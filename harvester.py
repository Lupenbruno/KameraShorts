#!/usr/bin/env python3
"""KameraShorts — Ankara Shorts Üretici (saatlik, sadece Ankara).

Mimari: Stream'den BAĞIMSIZ
─────────────────────────
- live_streamer.py 4 şehri kesintisiz YouTube/Kick yayınına gönderir (stream).
- BU SERVİS sadece saatte 1 kez (:15) çalışır:
    1. EGO API'den canlı otobüs çek (~50 araç)
    2. Hareketli + taze plakalı 10 aday seç
    3. ClipRecorder ile HLS'ten 40s kayıt (relay TTL renewal ile)
    4. YOLO subprocess ile içerik kontrolü (lazy: RAM'i kirletmez)
    5. İlk geçen klibi: 1080×1920 dikey, drawtext, audio mix, YouTube upload
- Stream batch dosyalarına (/tmp/ks_v4/batch_*.ts) DOKUNMAZ.

İstanbul/Çorum/Konya: bu servis tarafından üretilmez. Sadece stream rotation'da
görünür. Otomatik YouTube upload sadece Ankara içindir.

Kullanım:
  python harvester.py --daemon                # daemon, saat :15'te çalışır
  python harvester.py --once                  # test: bir kez şimdi çalıştır
  python harvester.py --once --no-upload      # üret ama upload etme
"""
import argparse
import json
import logging
import signal
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import schedule
import yaml

from src.audio_mixer import AudioMixer
from src.notifier import TelegramNotifier
from src.weather import get_weather
from src.youtube_uploader import YouTubeUploader
from src.camera_registry import CameraRegistry
from src.clip_recorder import ClipRecorder


# ─── Sabitler ───────────────────────────────────────────────────────────────

AYLAR = ["Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran",
         "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık"]
GUNLER = ["Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma",
          "Cumartesi", "Pazar"]

STATS_FILE = Path("data/harvester_stats.json")
USED_PLATES_FILE = Path("data/harvester_ankara_plates.json")
DEDUP_HOURS = 24
FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def turkce_tarih(dt: datetime) -> str:
    return f"{dt.day} {AYLAR[dt.month-1]} {GUNLER[dt.weekday()]}"


# ═══════════════════════════════════════════════════════════════════════════
# ANKARA SHORTS PRODUCER
# ═══════════════════════════════════════════════════════════════════════════

class AnkaraShortsProducer:
    """Saatlik Ankara Shorts üretici. Stream'den bağımsız."""

    def __init__(self, config_path: str = "config.yaml"):
        with open(config_path, encoding="utf-8") as f:
            self.config = yaml.safe_load(f)

        log_path = Path("logs/harvester.log")
        log_path.parent.mkdir(parents=True, exist_ok=True)
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(name)-12s] %(levelname)-7s %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
            handlers=[
                logging.FileHandler(log_path, encoding="utf-8"),
                logging.StreamHandler(sys.stdout),
            ],
        )
        self.log = logging.getLogger("ankara-shorts")

        self.ffmpeg = self.config.get("ffmpeg_path") or shutil.which("ffmpeg") or "ffmpeg"
        if self.ffmpeg and not Path(self.ffmpeg).exists() and self.ffmpeg != "ffmpeg":
            self.ffmpeg = shutil.which("ffmpeg") or "ffmpeg"

        self.owm_key = self.config.get("openweathermap_api_key", "")
        self.notifier = TelegramNotifier(self.config)
        self.mixer = AudioMixer(self.config)

        # Ankara için "eski usul" direct EGO HLS kayıt
        try:
            self.registry = CameraRegistry()
        except Exception as e:
            self.log.warning(f"CameraRegistry yüklenemedi: {e}")
            self.registry = None

        ankara_cfg = dict(self.config)
        ankara_cfg["schedule"] = dict(ankara_cfg.get("schedule", {}))
        ankara_cfg["schedule"]["clip_duration"] = 40   # Shorts: 40s
        ankara_cfg["paths"] = dict(ankara_cfg.get("paths", {}))
        ankara_cfg["paths"]["clips_dir"] = "data/clips"
        try:
            self.recorder = ClipRecorder(ankara_cfg)
        except Exception as e:
            self.log.warning(f"ClipRecorder yüklenemedi: {e}")
            self.recorder = None

        # Stats
        self.stats = {
            "ankara": {
                "attempts": 0, "success": 0, "failed": 0, "queued": 0,
                "last_run": None, "last_status": "—", "last_error": "",
                "last_youtube_url": "", "last_batch": "",
                "last_plate": "", "unique_plates_24h": 0,
            }
        }
        self._load_stats()

    # ── Stats persistence ────────────────────────────────────────────────

    def _load_stats(self):
        try:
            if STATS_FILE.exists():
                saved = json.loads(STATS_FILE.read_text(encoding="utf-8"))
                if "ankara" in saved:
                    self.stats["ankara"].update(saved["ankara"])
        except Exception as e:
            self.log.warning(f"Stats yüklenemedi: {e}")

    def _save_stats(self):
        try:
            STATS_FILE.parent.mkdir(parents=True, exist_ok=True)
            # Backwards-compat: harvester_stats.json hala 4 sehir formatinda
            full = {
                "ankara": self.stats["ankara"],
                "istanbul": {"attempts": 0, "success": 0, "failed": 0, "queued": 0},
                "corum":    {"attempts": 0, "success": 0, "failed": 0, "queued": 0},
                "konya":    {"attempts": 0, "success": 0, "failed": 0, "queued": 0},
            }
            STATS_FILE.write_text(
                json.dumps(full, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
        except Exception as e:
            self.log.warning(f"Stats kaydedilemedi: {e}")

    # ── Plaka dedup ─────────────────────────────────────────────────────

    def _recent_plates(self, hours: int = DEDUP_HOURS) -> set:
        if not USED_PLATES_FILE.exists():
            return set()
        try:
            data = json.loads(USED_PLATES_FILE.read_text(encoding="utf-8"))
            cutoff = datetime.now() - timedelta(hours=hours)
            return {p for p, ts in data.items()
                    if datetime.fromisoformat(ts) > cutoff}
        except Exception as e:
            self.log.warning(f"Plaka dosyası okuma hatası: {e}")
            return set()

    def _record_plate(self, plate: str):
        """Atomic write — son 48h plakaları + bu plaka."""
        if not plate:
            return
        data = {}
        if USED_PLATES_FILE.exists():
            try:
                data = json.loads(USED_PLATES_FILE.read_text(encoding="utf-8"))
            except Exception:
                data = {}
        data[plate] = datetime.now().isoformat()
        cutoff = datetime.now() - timedelta(hours=48)
        cleaned = {p: ts for p, ts in data.items()
                   if datetime.fromisoformat(ts) > cutoff}
        USED_PLATES_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = USED_PLATES_FILE.with_suffix(".json.tmp")
        try:
            tmp.write_text(
                json.dumps(cleaned, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(USED_PLATES_FILE)
        except Exception as e:
            self.log.warning(f"Plaka dosyası yazılamadı: {e}")
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass

    # ── YOLO subprocess wrapper ──────────────────────────────────────────

    def _analyze_clip(self, clip_path: str, duration: int = 40):
        """YOLO subprocess — RAM tasarrufu (model in-process load EDILMEZ).
        Doner: (score, threshold, thumb)."""
        try:
            r = subprocess.run(
                [sys.executable, "-m", "src.yolo_runner", "analyze",
                 "--clip", clip_path, "--ffmpeg", self.ffmpeg,
                 "--duration", str(duration)],
                capture_output=True, timeout=120, check=False,
            )
            out = r.stdout.decode("utf-8", errors="replace")
            for line in out.splitlines():
                if line.startswith("RESULT:"):
                    d = json.loads(line[7:])
                    return (int(d.get("score", 0)),
                            int(d.get("threshold", 4)),
                            d.get("thumb", ""))
        except Exception as e:
            self.log.warning(f"YOLO subprocess hata: {e}")
        return 99, 4, ""

    # ── Ankara üretim — Direct EGO HLS ────────────────────────────────────

    def produce_ankara(self, weather: Optional[dict]) -> Optional[tuple[str, str]]:
        """Stream'den BAĞIMSIZ. EGO API → 10 aday → YOLO → 40s shorts.

        Returns: (clip_path, plate) veya None
        """
        if not self.registry or not self.recorder:
            self.log.error("Registry/Recorder hazır değil")
            return None

        try:
            buses = self.registry.get_active_cameras(limit=30)
        except Exception as e:
            self.log.error(f"EGO API hata: {e}")
            return None
        if not buses:
            self.log.warning("Aktif araç yok")
            return None
        self.log.info(f"EGO'dan {len(buses)} aktif araç alındı")

        # Plaka dedup
        used = self._recent_plates()
        candidates = [b for b in buses
                      if b.get("license_plate", "?") not in used]
        if not candidates:
            self.log.warning(
                f"Son {DEDUP_HOURS}h tüm {len(buses)} plaka kullanılmış, "
                f"hepsi denenecek")
            candidates = buses

        # Hız sıralı
        def _speed(v):
            try:
                return float(v.get("speed", 0) or 0)
            except (TypeError, ValueError):
                return 0.0
        candidates.sort(key=lambda v: -_speed(v))

        # Top 10 — ilk YOLO geçen ile dur
        MAX_TRIES = 10
        now = datetime.now()
        for idx, bus in enumerate(candidates[:MAX_TRIES], 1):
            plate = bus.get("license_plate", "?")
            speed = _speed(bus)
            vtype = bus.get("vehicle_type", "?")
            self.log.info(
                f"{idx}/{MAX_TRIES} → '{plate}' "
                f"(tip: {vtype}, hız: {speed:.0f} km/h)")
            try:
                clip_path = self.recorder.record(bus, now)
            except Exception as e:
                self.log.error(f"{plate}: kayıt hata: {e}")
                continue
            if not clip_path:
                continue
            self.log.info(f"✓ {plate} klip hazır: {Path(clip_path).name}")
            return clip_path, plate

        self.log.warning(f"{MAX_TRIES} aracın hepsi başarısız")
        return None

    # ── Metadata ─────────────────────────────────────────────────────────

    def _build_metadata(self, now: datetime,
                        weather: Optional[dict]) -> dict:
        date_str = f"{now.day}/{now.month}/{now.year} {now.strftime('%H:%M')}"
        title = f"{date_str} - Ankara Canlı Trafik #Shorts"
        tags = ["ankara", "ankara canlı", "ego", "canlı kamera",
                "shorts", "trafik", "turkey"]
        weather_line = ""
        if weather:
            weather_line = (f"\n☁️ Hava: {weather['emoji']} "
                            f"{weather['temp']}°C {weather['condition']}\n")
        description = (
            f"Ankara canlı kamera görüntüleri.\n"
            f"📅 {turkce_tarih(now)}, saat {now.strftime('%H:%M')}.\n"
            f"{weather_line}"
            f"\n🎥 Otomatik üretim — KameraShorts Ankara Shorts."
        )
        tts_text = (
            f"Ankara. {turkce_tarih(now)}, saat {now.strftime('%H:%M')}.")
        if weather:
            tts_text += f" Hava {weather['condition']}, {weather['temp']} derece."
        return {
            "title": title[:100],
            "description": description,
            "tags": tags,
            "city": "Ankara",
            "tts_text": tts_text,
        }

    # ── Ana akış ────────────────────────────────────────────────────────

    def run_slot(self, do_upload: bool = True):
        """Tek slot — bir Shorts üret + upload."""
        now = datetime.now()
        self.log.info(f"╔══ Ankara Shorts slot başladı {now.strftime('%H:%M')} ══╗")

        s = self.stats["ankara"]
        s["attempts"] += 1
        s["last_run"] = now.isoformat()

        weather = get_weather("ankara", api_key=self.owm_key)
        if weather:
            self.log.info(
                f"Hava: {weather['emoji']} {weather['temp']}°C "
                f"{weather['condition']}")

        result = self.produce_ankara(weather)
        if not result:
            self.log.warning("Üretim başarısız → slot atlanıyor")
            s["last_status"] = "produce_fail"
            s["failed"] += 1
            self._save_stats()
            return

        clip, plate = result
        s["last_plate"] = plate
        s["last_batch"] = "direct_ego"

        # Metadata
        metadata = self._build_metadata(now, weather)
        self.log.info(f"Başlık: {metadata['title']}")

        # Audio mix
        try:
            clip = self.mixer.add_audio(
                clip, metadata, location="Ankara",
                weather=weather, duration=40,
            )
            self.log.info("Ses karıştırıldı")
        except Exception as e:
            self.log.warning(f"Ses ekleme hatası (orjinal video ile devam): {e}")

        # Upload
        upload_success = False
        if not do_upload:
            self.log.info("Upload atlandı (--no-upload)")
            s["last_status"] = "produced_only"
            s["success"] += 1
            upload_success = True
        else:
            try:
                uploader = YouTubeUploader(self.config)
                if uploader.check_quota():
                    result = uploader.upload(clip, metadata)
                    youtube_url = result.get("url", "")
                    self.log.info(f"✓ Yüklendi: {youtube_url}")
                    s["success"] += 1
                    s["last_status"] = "uploaded"
                    s["last_youtube_url"] = youtube_url
                    upload_success = True
                    try:
                        self.notifier.video_uploaded(
                            "ankara", metadata["title"], youtube_url, "Ankara")
                    except Exception:
                        pass
                else:
                    self.log.info("Kota dolu → kuyruğa")
                    uploader.add_to_queue(clip, metadata)
                    s["queued"] += 1
                    s["last_status"] = "queued"
                    upload_success = True
                    try:
                        self.notifier.quota_warning("Ankara")
                    except Exception:
                        pass
            except Exception as e:
                self.log.error(f"Upload hatası, kuyruğa: {e}")
                try:
                    uploader = YouTubeUploader(self.config)
                    uploader.add_to_queue(clip, metadata)
                    s["queued"] += 1
                    s["last_status"] = "queued"
                    upload_success = True
                except Exception:
                    s["last_status"] = "upload_fail"
                    s["failed"] += 1
                s["last_error"] = str(e)[:200]

        # Plaka kaydet (başarılı veya kuyrukta)
        if plate and upload_success:
            try:
                self._record_plate(plate)
                used = self._recent_plates()
                s["unique_plates_24h"] = len(used)
                self.log.info(
                    f"Plaka kaydedildi: {plate} "
                    f"(son 24h: {len(used)} farklı)")
            except Exception as e:
                self.log.warning(f"Plaka kaydı hatası: {e}")

        self._save_stats()
        self.log.info("╚══ slot bitti ══╝")

    # ── Daemon ───────────────────────────────────────────────────────────

    def run_daemon(self):
        hcfg = self.config.get("harvester", {})
        ankara_minute = hcfg.get("ankara_minute", 15)

        schedule.every().hour.at(f":{ankara_minute:02d}").do(self.run_slot)
        self.log.info(
            f"⏰ Zamanlayıcı: Ankara Shorts her saat :{ankara_minute:02d}")
        self.log.info(
            "ℹ Stream'den BAĞIMSIZ servis — direct EGO HLS, YOLO subprocess")
        self.log.info(
            "ℹ İstanbul/Çorum/Konya: YouTube upload YOK, sadece stream içeriği")

        try:
            self.notifier.send(
                "🎬 Ankara Shorts servisi başladı (stream'den bağımsız)")
        except Exception:
            pass

        self.log.info("✓ Daemon hazır. SIGTERM/SIGINT ile dur.")
        while True:
            try:
                schedule.run_pending()
            except Exception as e:
                self.log.error(f"Daemon döngü hatası: {e}")
            time.sleep(20)


# ───────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="KameraShorts — Ankara Shorts (saatlik, stream-bağımsız)")
    parser.add_argument("--daemon", action="store_true",
                        help="Daemon modu (saat :ankara_minute'te çalışır)")
    parser.add_argument("--once", action="store_true",
                        help="Test: bir kez şimdi çalıştır")
    parser.add_argument("--no-upload", action="store_true",
                        help="--once ile: YouTube'a yükleme")
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()

    h = AnkaraShortsProducer(config_path=args.config)

    if args.once:
        h.run_slot(do_upload=not args.no_upload)
    elif args.daemon:
        def _sig(s, f):
            h.log.info(f"Sinyal {s} alındı, kapanıyor")
            sys.exit(0)
        signal.signal(signal.SIGTERM, _sig)
        signal.signal(signal.SIGINT, _sig)
        h.run_daemon()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
