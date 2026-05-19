#!/usr/bin/env python3
"""
KameraShorts Harvester — Stream batch'lerinden tüm şehirler için video üretir.

Mimari: "Stream Hasadı"
─────────────────────
Live streamer zaten 4 şehri kesintisiz çekiyor:
  /tmp/ks_v4/batch_NNNN.ts → [Ankara 240s][İstanbul 240s][Çorum 240s][Konya 240s]

Harvester bu hazır içerikten saatlik / zamanlanmış olarak video kesip YouTube'a
yükler. Stream'e DOKUNMAZ — sadece OKUR.

Şehir Bazlı Davranış:
  Ankara   → 1080×1920 dikey 40s  + YOLO 6-aday + libx264 encode + overlay+TTS
  İstanbul → 1280× 720 yatay 180s + YOLO yok    + -c copy        + TTS
  Çorum    → 1280× 720 yatay 180s + YOLO yok    + -c copy        + TTS
  Konya    → 1280× 720 yatay 180s + YOLO yok    + -c copy        + TTS

Kullanım:
  python harvester.py --daemon                # daemon, schedule'a göre çalışır
  python harvester.py --city ankara           # test: tek slot şimdi çalıştır
  python harvester.py --city istanbul --no-upload
"""
import argparse
import json
import logging
import random
import re
import shutil
import signal
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
# Ankara için "eski usul" direct EGO HLS kayıt
from src.camera_registry import CameraRegistry
from src.clip_recorder import ClipRecorder


def analyze_clip(clip_path: str, ffmpeg_path: str,
                 duration: int = 40) -> tuple[int, int, str]:
    """YOLO subprocess wrapper — Lazy load, RAM tasarrufu.

    Harvester daemon YOLO modelini RAM'de tutmaz (~700 MB tasarruf).
    Subprocess: ~8s overhead, ~400 MB peak (sonra serbest)."""
    try:
        r = subprocess.run(
            [sys.executable, "-m", "src.yolo_runner", "analyze",
             "--clip", clip_path, "--ffmpeg", ffmpeg_path,
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
        logging.getLogger("harvester").warning(f"YOLO subprocess hata: {e}")
    # Hata durumunda gec (model yoksa gibi davran)
    return 99, 4, ""

# ─── Sabitler ───────────────────────────────────────────────────────────────

WORK_DIR = Path("/tmp/ks_v4")
CITY_DURATION = 240  # stream'de her şehir 240s
# Yayın sırası (eski stable): Ankara → İstanbul → Çorum → Konya (quad yok)
# (CITY_INDEX sadece log-parse başarısız olursa fallback; harvester gerçek
#  konumları log_parse ile dinamik olarak bulur.)
CITY_INDEX = {"ankara": 0, "istanbul": 1, "corum": 2, "konya": 3}
CITY_DISPLAY = {"ankara": "Ankara", "istanbul": "İstanbul", "corum": "Çorum", "konya": "Konya"}

AYLAR = ["Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran",
         "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık"]
GUNLER = ["Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma",
          "Cumartesi", "Pazar"]

STATS_FILE = Path("data/harvester_stats.json")
USED_PLATES_FILE = Path("data/harvester_ankara_plates.json")
STREAM_LOG = Path("/var/log/kamerashorts-live.log")
DEDUP_HOURS = 24  # son N saatte aynı plaka tekrar etmesin
FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def turkce_tarih(dt: datetime) -> str:
    return f"{dt.day} {AYLAR[dt.month-1]} {GUNLER[dt.weekday()]}"


def _esc_drawtext(text: str) -> str:
    """FFmpeg drawtext için özel karakter escape."""
    return text.replace("\\", "\\\\").replace("'", "\\'").replace(":", "\\:")


# ═══════════════════════════════════════════════════════════════════════════
# HARVESTER
# ═══════════════════════════════════════════════════════════════════════════

class Harvester:
    def __init__(self, config_path: str = "config.yaml"):
        self.config_path = config_path
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
        self.log = logging.getLogger("harvester")

        self.ffmpeg = self.config.get("ffmpeg_path") or shutil.which("ffmpeg") or "ffmpeg"
        if self.ffmpeg and not Path(self.ffmpeg).exists() and self.ffmpeg != "ffmpeg":
            self.ffmpeg = shutil.which("ffmpeg") or "ffmpeg"

        self.owm_key = self.config.get("openweathermap_api_key", "")
        self.notifier = TelegramNotifier(self.config)
        self.mixer = AudioMixer(self.config)

        # ── Ankara için "eski usul" direct EGO HLS kayıt ─────────────────
        # Stream batch'lerinden Ankara çekmek yerine, EGO API'den direkt
        # canlı otobüs seçip 40s kayıt. Çoklu aday + hız sıralaması ile
        # boş depo/duvar görüntüsü engellenir.
        try:
            self.ankara_registry = CameraRegistry()
        except Exception as e:
            self.log.warning(f"Ankara CameraRegistry yüklenemedi: {e}")
            self.ankara_registry = None
        ankara_cfg = dict(self.config)
        ankara_cfg["schedule"] = dict(ankara_cfg.get("schedule", {}))
        ankara_cfg["schedule"]["clip_duration"] = 40   # Shorts: 40s
        ankara_cfg["paths"] = dict(ankara_cfg.get("paths", {}))
        ankara_cfg["paths"]["clips_dir"] = "data/clips"
        try:
            self.ankara_recorder = ClipRecorder(ankara_cfg)
        except Exception as e:
            self.log.warning(f"Ankara ClipRecorder yüklenemedi: {e}")
            self.ankara_recorder = None

        # Per-city stats (dashboard için)
        self.stats = {
            city: {
                "attempts": 0, "success": 0, "failed": 0, "queued": 0,
                "last_run": None, "last_status": "—", "last_error": "",
                "last_youtube_url": "", "last_batch": "",
                "last_plate": "",                # Ankara için son kullanılan plaka
                "unique_plates_24h": 0,          # Son 24h çeşitlilik (Ankara)
            }
            for city in CITY_INDEX
        }
        self._load_stats()

        # Stale hardlink temizliği (önceki run'dan kalmış _harv_*.ts)
        try:
            now = time.time()
            for stale in WORK_DIR.glob("_harv_*.ts"):
                try:
                    if now - stale.stat().st_mtime > 1800:  # 30dk
                        stale.unlink(missing_ok=True)
                        self.log.info(f"Eski hardlink silindi: {stale.name}")
                except Exception:
                    pass
        except Exception:
            pass

    # ── Stats persistence ────────────────────────────────────────────────

    def _load_stats(self):
        try:
            if STATS_FILE.exists():
                saved = json.loads(STATS_FILE.read_text(encoding="utf-8"))
                for city in CITY_INDEX:
                    if city in saved:
                        self.stats[city].update(saved[city])
        except Exception as e:
            self.log.warning(f"Stats yüklenemedi: {e}")

    def _save_stats(self):
        try:
            STATS_FILE.parent.mkdir(parents=True, exist_ok=True)
            STATS_FILE.write_text(
                json.dumps(self.stats, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
        except Exception as e:
            self.log.warning(f"Stats kaydedilemedi: {e}")

    # ── Batch finder ─────────────────────────────────────────────────────

    def _list_available_batches(self) -> list[Path]:
        """En yeniden eskiye sıralı batch dosyaları (stream'in okuduğu en yeni HARİÇ)."""
        if not WORK_DIR.exists():
            return []
        batches = sorted(
            WORK_DIR.glob("batch_*.ts"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if not batches:
            return []
        # En yeniyi at, writer şu an okuyor olabilir
        return batches[1:] if len(batches) > 1 else batches

    def find_source_batch(self) -> Optional[Path]:
        """Diğer şehirler için: en son tamamlanmış batch."""
        batches = self._list_available_batches()
        return batches[0] if batches else None

    # ── Ankara plaka dedup (Stream Log Hasadı) ──────────────────────────

    def _get_recent_ankara_plates(self, hours: int = DEDUP_HOURS) -> set:
        """Son N saatte kullanılmış Ankara plakaları."""
        if not USED_PLATES_FILE.exists():
            return set()
        try:
            data = json.loads(USED_PLATES_FILE.read_text(encoding="utf-8"))
            cutoff = datetime.now() - timedelta(hours=hours)
            used = set()
            for plate, ts_str in data.items():
                try:
                    if datetime.fromisoformat(ts_str) > cutoff:
                        used.add(plate)
                except Exception:
                    continue
            return used
        except Exception as e:
            self.log.warning(f"Plaka dosyası okuma hatası: {e}")
            return set()

    def _record_ankara_plate(self, plate: str):
        """Plakayı kullanılmış olarak işaretle + 48h+ eskileri temizle.

        Atomic write (temp file + rename) — concurrent erişimde dosya bozulmaz.
        """
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
        cleaned = {}
        for p, ts in data.items():
            try:
                if datetime.fromisoformat(ts) > cutoff:
                    cleaned[p] = ts
            except Exception:
                continue
        USED_PLATES_FILE.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write: temp dosyaya yaz, sonra rename (POSIX atomik)
        tmp_path = USED_PLATES_FILE.with_suffix(".json.tmp")
        try:
            tmp_path.write_text(
                json.dumps(cleaned, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp_path.replace(USED_PLATES_FILE)
        except Exception as e:
            self.log.warning(f"Plaka dosyası yazılamadı: {e}")
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass

    def _get_batch_section(self, batch_path: Path) -> Optional[str]:
        """Stream log'undan bu batch'in 'başlıyor → HAZIR' aralığını al."""
        if not STREAM_LOG.exists():
            return None
        batch_name = batch_path.name
        try:
            result = subprocess.run(
                ["grep", "-B", "800", "-m", "1",
                 f"═══ Batch .* HAZIR: {batch_name}",
                 str(STREAM_LOG)],
                capture_output=True, timeout=10, check=False,
            )
            section = result.stdout.decode("utf-8", errors="replace")
        except Exception:
            return None
        if not section:
            return None
        # Aynı batch için en son "başlıyor" anchor'ından kes
        start_matches = list(re.finditer(r"═══\s+Batch\s+\d+\s+başlıyor", section))
        if start_matches:
            section = section[start_matches[-1].start():]
        return section

    def _get_batch_city_positions(self, batch_path: Path) -> dict:
        """
        Stream log'undan bu batch'teki şehir konumlarını çıkar.

        Stream her şehrin başarılı transcode'unu logluyor:
          [transcode:Ankara - 06 EJA 018] OK — 47s
          [transcode:Istanbul - Sultanahmet 2] OK — 434s
          [transcode:Corum] OK — 62s
          [transcode:Konya] OK — 86s

        Eksik şehir = log'da OK satırı yok = batch'te de yok.
        Bulunan şehirler config order'da binary concat ediliyor:
          ankara → istanbul → corum → konya

        Döner:
          {"ankara": {"start": 0, "duration": 240, "detail": "06 EJA 018"},
           "istanbul": {"start": 240, "duration": 240, "detail": "Sultanahmet 2"},
           ...}
          Eksik şehir dict'te YOK.
        """
        section = self._get_batch_section(batch_path)
        if not section:
            return {}

        # Config order (önemli: bu sırayla binary concat oluyor)
        city_patterns = [
            ("ankara",   r"\[transcode:Ankara\s*-\s*([^\]]+?)\]\s+OK\b"),
            ("istanbul", r"\[transcode:Istanbul\s*-\s*([^\]]+?)\]\s+OK\b"),
            ("corum",    r"\[transcode:Corum\]\s+OK\b"),
            ("konya",    r"\[transcode:Konya\]\s+OK\b"),
        ]

        detected: list[tuple[str, str]] = []
        for city_key, pattern in city_patterns:
            m = re.search(pattern, section)
            if not m:
                continue
            # Capture group varsa al (Ankara, Istanbul); yoksa boş (Corum, Konya)
            try:
                detail = m.group(1).strip().replace("_", " ")
            except (IndexError, AttributeError):
                detail = ""
            detected.append((city_key, detail))

        # Offset hesapla — bulunan şehirler config order'da sıralı
        positions = {}
        for idx, (city_key, detail) in enumerate(detected):
            positions[city_key] = {
                "start": idx * CITY_DURATION,
                "duration": CITY_DURATION,
                "detail": detail,
            }
        return positions

    def _snapshot_batch(self, batch_path: Path) -> Optional[Path]:
        """
        Race-condition koruması: batch'i hardlink ile snapshot al.
        Stream'in cleanup'ı batch_*.ts pattern'i siler ama _harv_*.ts'i atlar.
        Hardlink anında, 0 disk maliyeti (aynı inode).
        """
        try:
            import os as _os
            snap = WORK_DIR / f"_harv_{batch_path.stem}_{int(time.time())}.ts"
            try:
                snap.unlink()
            except Exception:
                pass
            _os.link(str(batch_path), str(snap))
            return snap
        except Exception as e:
            self.log.warning(f"Snapshot oluşturulamadı, original kullanılacak: {e}")
            return batch_path

    def find_batch_with_city(
        self, city: str, dedup_plate: bool = False,
    ) -> tuple[Optional[Path], Optional[dict]]:
        """
        Verilen şehir için uygun batch + konum bilgisi bul.

        Algoritma:
          1. En yeniden eskiye batch'leri tara (writer'ın okuduğu en yeni hariç)
          2. Her batch için log-parse: bu batch'te hangi şehirler hangi offsette?
          3. İstenen şehir batch'te YOKSA → atla, sonraki batch'i dene
          4. Ankara için ayrıca plaka dedup (son 24h tekrarsa atla)
          5. Tüm batch'ler yetersizse → (None, None) döner (slot atlanmalı)

        Döner: (batch_path, {"start": int, "duration": int, "detail": str})
        """
        candidates = self._list_available_batches()
        if not candidates:
            self.log.warning(f"[{city}] Hiç batch yok")
            return None, None

        used = (self._get_recent_ankara_plates() if dedup_plate and city == "ankara"
                else set())
        if dedup_plate and city == "ankara":
            self.log.info(f"[ankara] Son {DEDUP_HOURS}h'de {len(used)} plaka kullanılmış")

        for batch in candidates:
            positions = self._get_batch_city_positions(batch)
            if not positions:
                self.log.info(f"[{city}] {batch.name}: log'da hiç şehir tespit edilemedi, atla")
                continue
            if city not in positions:
                cities_in = ", ".join(positions.keys())
                self.log.info(
                    f"[{city}] {batch.name}: bu şehir batch'te YOK "
                    f"(içerdiği şehirler: {cities_in}), atla"
                )
                continue

            pos = positions[city]

            # Ankara için plaka dedup
            if dedup_plate and city == "ankara":
                plate = pos.get("detail", "")
                if plate and plate in used:
                    self.log.info(f"[ankara] {batch.name}: plaka {plate} → tekrar, atla")
                    continue
                self.log.info(
                    f"[ankara] {batch.name}: ✓ taze plaka '{plate}' "
                    f"(offset {pos['start']}s)"
                )
            else:
                detail = pos.get("detail", "")
                detail_str = f", '{detail}'" if detail else ""
                self.log.info(
                    f"[{city}] {batch.name}: ✓ konum {pos['start']}-"
                    f"{pos['start']+pos['duration']}s{detail_str}"
                )
            return batch, pos

        # Hiç uygun batch yok — slot atlamalı
        self.log.warning(
            f"[{city}] Son {len(candidates)} batch'in hiçbiri bu şehir için uygun değil, "
            f"slot atlanacak"
        )
        return None, None

    def city_offset(self, city: str) -> int:
        """[Deprecated] Sabit offset — sadece backward-compat. find_batch_with_city kullan."""
        return CITY_INDEX[city] * CITY_DURATION

    # ── Ankara: dikey Shorts + YOLO multi-candidate ──────────────────────

    def produce_ankara(self, batch: Path, base_offset: int,
                       weather: Optional[dict]) -> Optional[str]:
        """6 aday penceresi dene, ilk YOLO geçeni kullan.

        base_offset: find_batch_with_city tarafından tespit edilen Ankara'nın
        batch içindeki gerçek konumu (sabit 0 varsayımı YERİNE).
        """
        candidates = list(range(0, 240, 40))  # 0,40,80,120,160,200
        random.shuffle(candidates)  # Saatte hep aynı aday seçilmesin

        ts = datetime.now().strftime("%Y%m%d_%H%M")
        clips_dir = Path(self.config["paths"]["clips_dir"])
        clips_dir.mkdir(parents=True, exist_ok=True)

        for c_start in candidates:
            abs_start = base_offset + c_start
            tmp_clip = clips_dir / f"_ankara_yolo_{ts}_{c_start}.mp4"

            # Hızlı düşük kalite encode — sadece YOLO için
            cmd_probe = [
                self.ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                "-ss", str(abs_start), "-t", "40",
                "-i", str(batch),
                "-vf", "crop=ih*9/16:ih,scale=1080:1920",
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30",
                "-an", str(tmp_clip),
            ]
            try:
                subprocess.run(cmd_probe, capture_output=True, timeout=60, check=False)
            except subprocess.TimeoutExpired:
                self.log.warning(f"[ankara] Aday {abs_start}s probe timeout, atlanıyor")
                tmp_clip.unlink(missing_ok=True)
                continue

            if not tmp_clip.exists() or tmp_clip.stat().st_size < 50_000:
                self.log.warning(f"[ankara] Aday {abs_start}s probe boş, atlanıyor")
                tmp_clip.unlink(missing_ok=True)
                continue

            try:
                score, dyn_min, _ = analyze_clip(str(tmp_clip), self.ffmpeg, 40)
                self.log.info(f"[ankara] Aday {abs_start}s: YOLO {score}p (eşik {dyn_min})")
                if score >= dyn_min:
                    tmp_clip.unlink(missing_ok=True)
                    return self._produce_ankara_final(batch, abs_start, ts, weather)
            except Exception as e:
                self.log.warning(f"[ankara] Aday {abs_start}s YOLO hatası: {e}")
            tmp_clip.unlink(missing_ok=True)

        self.log.warning(f"[ankara] 6 adayın hepsi elendi → slot atlanıyor")
        return None

    def _produce_ankara_final(self, batch: Path, abs_start: int, ts: str,
                              weather: Optional[dict]) -> Optional[str]:
        """YOLO geçen aday için son kalite + drawtext overlay."""
        clips_dir = Path(self.config["paths"]["clips_dir"])
        out = clips_dir / f"ankara_{ts}.mp4"

        now = datetime.now()
        time_text = now.strftime("%H\\:%M")  # drawtext escape: :
        city_text = "ANKARA"
        if weather:
            weather_text = f"{weather['temp']}°C {weather['condition']}"
            weather_esc = _esc_drawtext(weather_text)
        else:
            weather_esc = ""

        vf = (
            "crop=ih*9/16:ih,scale=1080:1920,"
            f"drawtext=fontfile={FONT_PATH}:text='{city_text}':"
            f"x=30:y=h-140:fontsize=52:fontcolor=white:"
            f"box=1:boxcolor=black@0.55:boxborderw=10:shadowx=2:shadowy=2"
        )
        if weather_esc:
            vf += (
                f",drawtext=fontfile={FONT_PATH}:text='{time_text}  {weather_esc}':"
                f"x=30:y=h-70:fontsize=36:fontcolor=lightyellow:"
                f"box=1:boxcolor=black@0.5:boxborderw=8:shadowx=1:shadowy=1"
            )
        else:
            vf += (
                f",drawtext=fontfile={FONT_PATH}:text='{time_text}':"
                f"x=30:y=h-70:fontsize=36:fontcolor=lightyellow:"
                f"box=1:boxcolor=black@0.5:boxborderw=8"
            )

        cmd = [
            self.ffmpeg, "-y", "-hide_banner", "-loglevel", "warning",
            "-ss", str(abs_start), "-t", "40",
            "-i", str(batch),
            "-vf", vf,
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart",
            str(out),
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=180, check=False)
            if result.returncode != 0:
                err = result.stderr[-300:].decode("utf-8", errors="replace") if result.stderr else ""
                self.log.error(f"[ankara] Final encode hatası: {err}")
                return None
            if not out.exists() or out.stat().st_size < 100_000:
                self.log.error(f"[ankara] Final dosya boş/küçük")
                return None
            return str(out)
        except subprocess.TimeoutExpired:
            self.log.error(f"[ankara] Final encode timeout")
            return None

    # ── Ankara DIRECT MODE (eski usul, EGO API'den) ───────────────────────

    def produce_ankara_direct(self, weather: Optional[dict]) -> Optional[tuple[str, str]]:
        """
        Stream'den BAĞIMSIZ: EGO API'den canlı otobüs seç, direkt HLS kayıt.

        Avantajlar (stream Hasadı'na göre):
          - ~50 aktif otobüsten en hareketli olanı seçilir (hız > 0 öncelikli)
          - Saatte 1 değil saatte birçok deneme (5 farklı otobüs)
          - Stream'in random seçimine bağımlı değil → "depo otobüsü" sorunu azalır
          - Relay TTL renewal thread (yeni eklendi) → 40s timeout fix

        Döner: (final_clip_path, plate) veya None
        """
        if not self.ankara_registry or not self.ankara_recorder:
            self.log.error("[ankara-direct] CameraRegistry/ClipRecorder hazır değil")
            return None

        # 1) Aktif araçları çek (limit 30 — Solo otobüs şansı artırılır)
        try:
            buses = self.ankara_registry.get_active_cameras(limit=30)
        except Exception as e:
            self.log.error(f"[ankara-direct] EGO API hata: {e}")
            return None

        if not buses:
            self.log.warning("[ankara-direct] Aktif araç yok")
            return None
        self.log.info(f"[ankara-direct] EGO'dan {len(buses)} aktif araç alındı")

        # 2) Plaka dedup (son 24h kullanılmış olanları ele)
        used = self._get_recent_ankara_plates()
        candidates = [b for b in buses
                      if b.get("license_plate", "?") not in used]
        if not candidates:
            self.log.warning(
                f"[ankara-direct] Son 24h tüm {len(buses)} plaka kullanılmış, "
                f"fallback hepsi"
            )
            candidates = buses

        # 3) Hareket halindeki otobüsleri öncele (speed > 0 = ilginç içerik)
        def _speed(v):
            try:
                return float(v.get("speed", 0) or 0)
            except (TypeError, ValueError):
                return 0.0
        candidates.sort(key=lambda v: -_speed(v))

        # 4) Top 10 aracı sırayla dene — ilk YOLO geçen ile dur
        # (Vehicle type filtresi YOK — YOLO skoru tek karar mekanizması.
        #  10 deneme = ~%97 başarı ihtimali, kalan %3 slot atlanır.)
        MAX_TRIES = 10
        now = datetime.now()
        for idx, bus in enumerate(candidates[:MAX_TRIES], 1):
            plate = bus.get("license_plate", "?")
            speed = _speed(bus)
            vtype = bus.get("vehicle_type", "?")
            self.log.info(
                f"[ankara-direct] {idx}/{MAX_TRIES} deneme → '{plate}' "
                f"(tip: {vtype}, hız: {speed:.0f} km/h)"
            )

            try:
                clip_path = self.ankara_recorder.record(bus, now)
            except Exception as e:
                self.log.error(f"[ankara-direct] {plate}: kayıt hata: {e}")
                continue

            if not clip_path:
                continue  # YOLO elendi veya kayıt başarısız → sonraki

            self.log.info(f"[ankara-direct] ✓ {plate} klip hazır: {Path(clip_path).name}")
            return clip_path, plate

        self.log.warning(f"[ankara-direct] {MAX_TRIES} aracın hepsi başarısız, slot atlanacak")
        return None

    # ── Diğer şehirler: yatay, c:copy ─────────────────────────────────────

    def produce_landscape(self, city: str, batch: Path,
                          base_offset: int) -> Optional[str]:
        """İstanbul/Çorum/Konya: 180s yatay, -c copy ile saniyeler içinde kes.

        base_offset: find_batch_with_city tarafından tespit edilen bu şehrin
        batch içindeki gerçek konumu (kayma varsa otomatik düzeltilir).
        """
        # İstanbul top-level config (cities dict altında değil), diğerleri cities altında
        if city == "istanbul":
            ist_cfg = self.config.get("istanbul", {})
            dur = ist_cfg.get("clip_duration", 180)
            clips_dir = Path(self.config["paths"].get("istanbul_clips_dir", "data/istanbul_clips"))
        else:
            city_cfg = self.config.get("cities", {}).get(city)
            if not city_cfg:
                self.log.error(f"[{city}] config'de tanımlı değil")
                return None
            dur = city_cfg.get("clip_duration", 180)
            clips_dir = Path(city_cfg["clips_dir"])
        if dur > CITY_DURATION:
            dur = CITY_DURATION
        # Şehir 240s, biz 180s alırız → 60s kayma payı, rastgele başla
        max_shift = CITY_DURATION - dur
        shift = random.randint(0, max_shift) if max_shift > 0 else 0
        abs_start = base_offset + shift

        clips_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M")
        out = clips_dir / f"{city}_{ts}.mp4"

        # -c copy: encode YOK, sadece konteynerden kes
        cmd = [
            self.ffmpeg, "-y", "-hide_banner", "-loglevel", "warning",
            "-ss", str(abs_start), "-t", str(dur),
            "-i", str(batch),
            "-c", "copy",
            "-avoid_negative_ts", "make_zero",
            "-movflags", "+faststart",
            str(out),
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=60, check=False)
            if result.returncode != 0:
                err = result.stderr[-300:].decode("utf-8", errors="replace") if result.stderr else ""
                self.log.warning(f"[{city}] copy hatası, yeniden encode'a düşülüyor: {err[:100]}")
                # Fallback: yeniden encode (mpegts→mp4 dönüşümünde codec uyumsuzluğu olabilir)
                cmd_re = [
                    self.ffmpeg, "-y", "-hide_banner", "-loglevel", "warning",
                    "-ss", str(abs_start), "-t", str(dur),
                    "-i", str(batch),
                    "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
                    "-c:a", "aac",
                    "-movflags", "+faststart",
                    str(out),
                ]
                r2 = subprocess.run(cmd_re, capture_output=True, timeout=180, check=False)
                if r2.returncode != 0:
                    return None
            if not out.exists() or out.stat().st_size < 100_000:
                return None
            return str(out)
        except subprocess.TimeoutExpired:
            self.log.error(f"[{city}] kesim timeout")
            return None

    # ── Metadata ─────────────────────────────────────────────────────────

    def _build_metadata(self, city: str, now: datetime,
                        weather: Optional[dict]) -> dict:
        display = CITY_DISPLAY[city]
        date_str = f"{now.day}/{now.month}/{now.year} {now.strftime('%H:%M')}"
        if city == "ankara":
            title = f"{date_str} - Ankara Canlı Trafik #Shorts"
            tags = ["ankara", "ankara canlı", "ego", "canlı kamera", "shorts", "trafik", "turkey"]
        else:
            title = f"{date_str} - {display} Canlı Kamera"
            tags_base = self.config.get("cities", {}).get(city, {}).get("youtube", {}).get(
                "tags", [display, "canlı kamera", "turkey"])
            tags = list(tags_base)

        weather_line = ""
        if weather:
            weather_line = f"\n☁️ Hava: {weather['emoji']} {weather['temp']}°C {weather['condition']}\n"

        description = (
            f"{display} canlı kamera görüntüleri.\n"
            f"📅 {turkce_tarih(now)}, saat {now.strftime('%H:%M')}.\n"
            f"{weather_line}"
            f"\n🎥 Otomatik üretim — KameraShorts Harvester."
        )

        tts_text = (
            f"{display}. {turkce_tarih(now)}, saat {now.strftime('%H:%M')}."
        )
        if weather:
            tts_text += f" Hava {weather['condition']}, {weather['temp']} derece."

        return {
            "title": title[:100],          # YouTube 100 char limit
            "description": description,
            "tags": tags,
            "city": display,
            "tts_text": tts_text,
        }

    # ── Upload ───────────────────────────────────────────────────────────

    def _get_uploader(self, city: str) -> YouTubeUploader:
        """Şehir bazlı YouTubeUploader (her şehrin kendi config/quota'sı)."""
        cfg = dict(self.config)
        if city == "ankara":
            # Ankara için top-level youtube config
            pass  # cfg["youtube"] zaten doğru
        elif city == "istanbul":
            iy = self.config.get("istanbul_youtube") or self.config.get("youtube")
            cfg["youtube"] = iy
            cfg["paths"] = dict(cfg.get("paths", {}))
            cfg["paths"]["queue_path"] = self.config["paths"].get(
                "istanbul_queue_path", "data/queue/istanbul_upload_queue.json")
        else:
            city_cfg = self.config["cities"].get(city, {})
            cfg["youtube"] = city_cfg.get("youtube") or self.config["youtube"]
            cfg["paths"] = dict(cfg.get("paths", {}))
            cfg["paths"]["queue_path"] = city_cfg.get(
                "queue_path", f"data/queue/{city}_upload_queue.json")
        return YouTubeUploader(cfg)

    # ── Ana akış ─────────────────────────────────────────────────────────

    def produce(self, city: str, do_upload: bool = True):
        """Tek slot için: batch bul → kes → ses ekle → yükle."""
        display = CITY_DISPLAY[city]
        self.log.info(f"╔══ [{display}] slot başladı {datetime.now().strftime('%H:%M')} ══╗")

        s = self.stats[city]
        s["attempts"] += 1
        s["last_run"] = datetime.now().isoformat()

        # Hava durumu — slot başında çek (her iki yol da kullanır)
        weather = get_weather(city, api_key=self.owm_key)
        if weather:
            self.log.info(
                f"[{city}] Hava: {weather['emoji']} {weather['temp']}°C "
                f"{weather['condition']}"
            )

        # ═══ ANKARA: ESKİ USUL (direct EGO HLS, stream'e bağımlı değil) ═══
        # Stream'in rastgele seçtiği otobüs yerine, EGO API'den hareket halinde
        # otobüs seçilir. 5 farklı otobüs denenir, ilk YOLO geçen kullanılır.
        ankara_plate: Optional[str] = None
        batch = None
        base_offset = 0
        ankara_clip_direct: Optional[str] = None

        if city == "ankara":
            self.log.info(f"[ankara] Eski usul (direct EGO HLS) modu")
            result = self.produce_ankara_direct(weather)
            if not result:
                self.log.warning(f"[ankara] Direct mode başarısız → slot atlanıyor")
                s["last_status"] = "produce_fail"
                s["failed"] += 1
                self._save_stats()
                return
            ankara_clip_direct, ankara_plate = result
            s["last_plate"] = ankara_plate
            s["last_batch"] = "direct_ego"
            # Ankara için batch/offset/snapshot KULLANILMIYOR
        else:
            # ═══ DİĞER ŞEHİRLER: STREAM HASADI ═══
            # Batch + KONUM bul — log-parse ile gerçek offset (kayma varsa düzeltir)
            batch, city_pos = self.find_batch_with_city(city, dedup_plate=False)
            if not batch or not city_pos:
                self.log.warning(f"[{city}] Uygun batch bulunamadı → slot atlanıyor")
                s["last_status"] = "no_batch"
                s["failed"] += 1
                self._save_stats()
                return

            base_offset = city_pos["start"]
            s["last_batch"] = batch.name
            self.log.info(
                f"[{city}] Kaynak batch: {batch.name} "
                f"({batch.stat().st_size//1024//1024}MB), offset={base_offset}s"
            )

            # Race-condition koruması: hardlink snapshot
            original_batch = batch
            batch = self._snapshot_batch(original_batch)
            if batch != original_batch:
                self.log.info(f"[{city}] Hardlink snapshot: {batch.name}")

        # (weather artık üstte çekildi)

        # Üret — Ankara için direct sonucu kullan, diğerleri için stream-harvest
        if city == "ankara":
            clip = ankara_clip_direct   # produce_ankara_direct'ten geldi
        else:
            clip = self.produce_landscape(city, batch, base_offset)

        if not clip:
            self.log.warning(f"[{city}] Üretim başarısız")
            s["last_status"] = "produce_fail"
            s["failed"] += 1
            self._save_stats()
            return

        self.log.info(f"[{city}] Klip hazır: {Path(clip).name}")

        # Metadata
        metadata = self._build_metadata(city, datetime.now(), weather)
        self.log.info(f"[{city}] Başlık: {metadata['title']}")

        # Audio mix
        try:
            if city == "ankara":
                mix_dur = 40
            elif city == "istanbul":
                mix_dur = self.config.get("istanbul", {}).get("clip_duration", 180)
            else:
                mix_dur = self.config.get("cities", {}).get(city, {}).get("clip_duration", 180)
            clip = self.mixer.add_audio(
                clip, metadata,
                location=display,
                weather=weather,
                duration=mix_dur,
            )
            self.log.info(f"[{city}] Ses karıştırıldı")
        except Exception as e:
            self.log.warning(f"[{city}] Ses ekleme hatası (orjinal video ile devam): {e}")

        # Upload
        upload_success = False
        if not do_upload:
            self.log.info(f"[{city}] Upload atlandı (--no-upload)")
            s["last_status"] = "produced_only"
            s["success"] += 1
            upload_success = True       # plakayı kaydet (üretildi = kullanıldı)
        else:
            try:
                uploader = self._get_uploader(city)
                if uploader.check_quota():
                    result = uploader.upload(clip, metadata)
                    youtube_url = result.get("url", "")
                    self.log.info(f"[{city}] ✓ Yüklendi: {youtube_url}")
                    s["success"] += 1
                    s["last_status"] = "uploaded"
                    s["last_youtube_url"] = youtube_url
                    upload_success = True
                    try:
                        self.notifier.video_uploaded(city, metadata["title"], youtube_url, display)
                    except Exception:
                        pass
                else:
                    self.log.info(f"[{city}] Kota dolu → kuyruğa")
                    uploader.add_to_queue(clip, metadata)
                    s["queued"] += 1
                    s["last_status"] = "queued"
                    upload_success = True   # kuyruğa girdi = bu plaka kullanıldı sayılır
                    try:
                        self.notifier.quota_warning(display)
                    except Exception:
                        pass
            except Exception as e:
                self.log.error(f"[{city}] Upload hatası, kuyruğa: {e}")
                try:
                    uploader = self._get_uploader(city)
                    uploader.add_to_queue(clip, metadata)
                    s["queued"] += 1
                    s["last_status"] = "queued"
                    upload_success = True
                except Exception:
                    s["last_status"] = "upload_fail"
                    s["failed"] += 1
                s["last_error"] = str(e)[:200]

        # Ankara plaka kaydet (başarılı veya kuyruğa girmiş ise)
        if city == "ankara" and ankara_plate and upload_success:
            try:
                self._record_ankara_plate(ankara_plate)
                used = self._get_recent_ankara_plates()
                s["unique_plates_24h"] = len(used)
                self.log.info(f"[ankara] Plaka kaydedildi: {ankara_plate} (son 24h: {len(used)} farklı)")
            except Exception as e:
                self.log.warning(f"[ankara] Plaka kaydı hatası: {e}")

        # Snapshot temizliği — sadece stream-harvest yapan şehirlerde
        # (Ankara direct mode'da batch/snapshot yok)
        if city != "ankara":
            try:
                if batch is not None and original_batch is not None and batch != original_batch:
                    batch.unlink(missing_ok=True)
            except (NameError, Exception):
                pass

        self._save_stats()
        self.log.info(f"╚══ [{display}] slot bitti ══╝")

    # ── Daemon ───────────────────────────────────────────────────────────

    def run_daemon(self):
        hcfg = self.config.get("harvester", {})

        # ═══ YENİ MOD: SADECE ANKARA ═══
        # Diğer şehirler (İstanbul/Çorum/Konya) artık otomatik upload edilmiyor.
        # Sadece live stream'de görünmeye devam ediyorlar (rotasyon + quad).
        # Manuel upload için: python harvester.py --city <city>  hâlâ çalışır.

        ankara_minute = hcfg.get("ankara_minute", 15)
        schedule.every().hour.at(f":{ankara_minute:02d}").do(self.produce, city="ankara")
        self.log.info(f"⏰ Zamanlayıcı: ankara → her saat :{ankara_minute:02d}")
        self.log.info("ℹ İstanbul/Çorum/Konya otomatik upload DEVRE DIŞI (sadece stream'de)")

        try:
            self.notifier.send(
                "🎬 Harvester başlatıldı — SADECE Ankara Shorts modu.\n"
                "Stream rotasyonu: 4 şehir devam, ancak Shorts üretimi sadece Ankara için."
            )
        except Exception:
            pass

        self.log.info("✓ Harvester daemon hazır. Ctrl+C ile dur.")

        while True:
            try:
                schedule.run_pending()
            except Exception as e:
                self.log.error(f"Daemon döngü hatası (devam): {e}")
            time.sleep(20)


# ───────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="KameraShorts Harvester")
    parser.add_argument("--daemon", action="store_true",
                        help="Daemon modu (schedule'a göre çalışır)")
    parser.add_argument("--city", choices=list(CITY_INDEX.keys()),
                        help="Test: bu şehir için tek slot hemen çalıştır")
    parser.add_argument("--no-upload", action="store_true",
                        help="--city ile birlikte: YouTube'a yükleme, sadece üret")
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()

    h = Harvester(config_path=args.config)

    if args.city:
        h.produce(city=args.city, do_upload=not args.no_upload)
    elif args.daemon:
        # SIGTERM/SIGINT clean exit
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
