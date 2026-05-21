#!/usr/bin/env python3
"""KameraShorts — Çok Şehirli Shorts Üretici (saatlik, TÜM şehirler).

Mimari: Stream'den BAĞIMSIZ
─────────────────────────
- live_streamer.py şehirleri kesintisiz YouTube/Kick yayınına gönderir (stream).
- BU SERVİS sadece saatte 1 kez (:15) çalışır ve HER SLOT'ta bir şehir seçer:
    • Ankara  → EGO API canlı otobüs (relay + TTL renewal, ClipRecorder)
    • Diğer   → camera_pool.json havuzundan sabit şehir kamerası (resolver +
                 GenericRecorder.record_url, Referer header'lı)
  Şehir seçimi: anti-repeat (son slotlardaki şehri tekrar seçme).
- YOLO taraması ZORUNLU (fail-closed): kayıt sonrası analiz GEÇMEZSE klip atılır.
  YOLO modeli yüklenemezse de klip REDDEDİLİR (yayına taranmamış içerik çıkmaz).
- Şehir adı GİZLİ tutulur: başlık/overlay/TTS şehri söylemez ("Sizce burası
  neresi?" tahmin formatı tüm şehirler için). Stream batch'lerine DOKUNMAZ.

Kullanım:
  python harvester.py --daemon                # daemon, saat :15'te çalışır
  python harvester.py --once                  # test: bir kez şimdi çalıştır
  python harvester.py --once --no-upload      # üret ama upload etme
  python harvester.py --once --city İstanbul  # belirli şehri zorla (test)
"""
import argparse
import json
import logging
import random
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
from src.generic_recorder import GenericRecorder

import resolvers  # /opt/KameraShorts/resolvers.py — havuz kameralarını taze m3u8'e çözer


# ─── Sabitler ───────────────────────────────────────────────────────────────

AYLAR = ["Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran",
         "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık"]
GUNLER = ["Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma",
          "Cumartesi", "Pazar"]

STATS_FILE = Path("data/harvester_stats.json")
USED_PLATES_FILE = Path("data/harvester_ankara_plates.json")
USED_CAMS_FILE = Path("data/harvester_used_cams.json")     # havuz kamera dedup
USED_CITIES_FILE = Path("data/harvester_used_cities.json")  # slot-bazlı şehir anti-repeat
DEDUP_HOURS = 24
CAM_DEDUP_HOURS = 12          # aynı havuz kamerasını 12h tekrar gösterme
CITY_ANTIREPEAT = 2          # son 2 slotta kullanılan şehri tekrar seçme
POOL_MAX_CANDIDATES = 8      # havuz şehri için en fazla N kamera dene (sonra slot atla)
FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

POOL_PATH = "/opt/KameraShorts/camera_pool.json"
# Ankara mobil (ankara_ego) havuz dışı — EGO path'i ile ayrı üretilir.
POOL_EXCLUDE_TYPES = {"ankara_ego"}

# Havuz şehirleri için hava durumu koordinatları (weather.CITY_COORDS dışındakiler).
# get_weather(lat=, lon=) ile çözülür. Bulunmayan şehir → hava durumu atlanır (sorun değil).
POOL_CITY_COORDS = {
    "İstanbul": (41.0082, 28.9784), "Kayseri": (38.7312, 35.4787),
    "Kocaeli": (40.7654, 29.9408), "Balıkesir": (39.6484, 27.8826),
    "Çorum": (40.5506, 34.9556), "Konya": (37.8714, 32.4846),
    "Develi": (38.3906, 35.4906), "Reşadiye": (40.3914, 37.3447),
    "Ankara": (39.9334, 32.8597),
}

# Optimize edilmiş aktif saatler (207 video view analizi + YouTube Shorts algoritması):
# Günde 6 video — self-cannibalization + spam riski minimal, kalite maksimum.
#   09:00 sabah kuşağı (30 video kanıtlı, 250 ort view)
#   12:00 öğle peak (339 ort, en güvenilir yüksek)
#   14:00 ikindi peak (429 ort, en yüksek)
#   17:00 iş çıkışı (akşam Shorts izleme başlangıcı)
#   19:00 akşam yemek sonrası
#   21:00 gece Shorts prime (yatmadan önce scroll)
# SKIP: gece 22-08 (kameralar zemin/garaj, YOLO eler), 07/20 (kanıtlı ölü saatler),
#       10/11/13/15/16/18 (self-cannibalization önleme — peak'lere yoğunlaş)
ACTIVE_HOURS = {9, 12, 14, 17, 19, 21}

# Başlık rotasyonu: tek format YouTube "Repetitive Content" cezasına yol açar.
# 5 farklı şablon random seçilir. (Şehir adı KASITLI olarak belirtilmiyor.)
TITLE_TEMPLATES = [
    "{date_short} - Canlı Trafik Kamerası #Shorts",
    "Şu An Canlı: {weather_short} | Şehir Kamerası #Shorts",
    "Canlı Kamera Yayını — {date_short} #Shorts",
    "Şehir Yolları Canlı: {gun} {temp}°C #Shorts",
    "Canlı Sokak Kamerası | {gun_saat} — Trafik #Shorts",
]

# Hashtag listesi — algoritmik sınıflandırma için (şehir adı yok)
HASHTAGS = (
    "#CanlıTrafik #CanlıKamera #Şehir #Trafik #Sokak "
    "#Turkey #Türkiye #LiveCamera #Shorts #ŞehirKamerası #Trafikİzle"
)


def turkce_tarih(dt: datetime) -> str:
    return f"{dt.day} {AYLAR[dt.month-1]} {GUNLER[dt.weekday()]}"


# ═══════════════════════════════════════════════════════════════════════════
# ANKARA SHORTS PRODUCER
# ═══════════════════════════════════════════════════════════════════════════

class AnkaraShortsProducer:
    """Saatlik ÇOK ŞEHİRLİ Shorts üretici. Stream'den bağımsız.

    (Sınıf adı geçmiş uyumluluk için korundu — artık Ankara + havuz şehirleri.)
    Her slot bir şehir seçer: Ankara → EGO otobüs, diğerleri → camera_pool.json.
    """

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

        # Havuz şehirleri için genel kaydedici (Referer header + tls_verify 0,
        # 1080×1920 Shorts canvas, ZORUNLU kayıt-sonrası YOLO).
        try:
            self.pool_recorder = GenericRecorder(
                clips_dir="data/clips", duration=40, ffmpeg_path=self.ffmpeg)
        except Exception as e:
            self.log.warning(f"GenericRecorder yüklenemedi: {e}")
            self.pool_recorder = None

        # Stats — şehir bazlı bucket'lar. Dashboard ankara/istanbul/corum/konya
        # okur; diğer şehir anahtarları eklenebilir (dashboard bilmediğini yok sayar).
        self.stats = {}
        for _c in ("ankara", "istanbul", "corum", "konya"):
            self.stats[_c] = self._blank_stats()
        self.stats["ankara"].update({"last_plate": "", "unique_plates_24h": 0})
        self._load_stats()

    # ── Stats persistence (şehir bazlı) ──────────────────────────────────

    @staticmethod
    def _blank_stats() -> dict:
        return {"attempts": 0, "success": 0, "failed": 0, "queued": 0,
                "last_run": None, "last_status": "—", "last_error": "",
                "last_youtube_url": "", "last_batch": "", "last_camera": ""}

    @staticmethod
    def _city_key(city: str) -> str:
        """Şehir görünen adı → stats anahtarı: 'İstanbul'→'istanbul', 'Çorum'→'corum'."""
        tr = str.maketrans("çğıöşüÇĞİÖŞÜ", "cgiosucgiosU")
        return (city or "?").translate(tr).lower().replace(" ", "")

    def _bucket(self, city: str) -> dict:
        k = self._city_key(city)
        if k not in self.stats:
            self.stats[k] = self._blank_stats()
        return self.stats[k]

    def _load_stats(self):
        try:
            if STATS_FILE.exists():
                saved = json.loads(STATS_FILE.read_text(encoding="utf-8"))
                for k, v in saved.items():
                    if isinstance(v, dict):
                        self.stats.setdefault(k, self._blank_stats()).update(v)
        except Exception as e:
            self.log.warning(f"Stats yüklenemedi: {e}")

    def _save_stats(self):
        try:
            STATS_FILE.parent.mkdir(parents=True, exist_ok=True)
            # Dashboard ankara/istanbul/corum/konya bekler — hep mevcut olsun.
            for _c in ("ankara", "istanbul", "corum", "konya"):
                self.stats.setdefault(_c, self._blank_stats())
            STATS_FILE.write_text(
                json.dumps(self.stats, ensure_ascii=False, indent=2, default=str),
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
        Doner: (score, threshold, thumb).
        ZORUNLU YOLO: subprocess çalışmazsa/sonuç yoksa score=0 → klip REDDEDİLİR
        (fail-closed). Taranmamış içerik yayına çıkmaz."""
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
        return 0, 4, ""   # fail-closed: YOLO sonuç vermedi → reddet

    # ── Ankara üretim — Direct EGO HLS ────────────────────────────────────

    def produce_ankara(self, weather: Optional[dict]) -> Optional[tuple[str, str]]:
        """Stream'den BAĞIMSIZ. EGO API → 10 aday → YOLO → 40s shorts.

        Returns: (clip_path, plate) veya None
        """
        if not self.registry or not self.recorder:
            self.log.error("Registry/Recorder hazır değil")
            return None

        # Tum araclar (otobus/kamyon/sürgü her tip) — YOLO karar verir.
        # Arac tipinin onemi yok; gorsel ickilik kalitesi tek sec olcutu.
        try:
            buses = self.registry.get_active_cameras(limit=80)
        except Exception as e:
            self.log.error(f"EGO API hata: {e}")
            return None
        if not buses:
            self.log.warning("Aktif araç yok")
            return None

        # Hız sıralı: hareketli araçlar genelde daha ilginç görüntü verir
        def _speed(v):
            try:
                return float(v.get("speed", 0) or 0)
            except (TypeError, ValueError):
                return 0.0
        buses.sort(key=lambda v: -_speed(v))
        self.log.info(f"EGO'dan {len(buses)} aktif araç alındı (hız sıralı)")

        # Plaka dedup
        used = self._recent_plates()
        candidates = [b for b in buses
                      if b.get("license_plate", "?") not in used]
        if not candidates:
            self.log.warning(
                f"Son {DEDUP_HOURS}h tüm {len(buses)} plaka kullanılmış, "
                f"hepsi denenecek")
            candidates = buses

        # BULANA KADAR DENE — limit yok. İlk YOLO geçen aday ile dur.
        # Tüm 80 araç tüketilirse slot atlanır, sonraki saat tekrar tetiklenir.
        total = len(candidates)
        now = datetime.now()
        for idx, bus in enumerate(candidates, 1):
            plate = bus.get("license_plate", "?")
            try:
                speed = float(bus.get("speed", 0) or 0)
            except (TypeError, ValueError):
                speed = 0.0
            vtype = bus.get("vehicle_type", "?")
            self.log.info(
                f"{idx}/{total} → '{plate}' "
                f"(tip: {vtype}, hız: {speed:.0f} km/h)")
            try:
                clip_path = self.recorder.record(bus, now)
            except Exception as e:
                self.log.error(f"{plate}: kayıt hata: {e}")
                continue
            if not clip_path:
                continue
            self.log.info(
                f"✓ {plate} klip hazır ({idx}/{total} aday denendi): "
                f"{Path(clip_path).name}")
            return clip_path, plate

        self.log.warning(
            f"{total} aday tükendi, hiç biri YOLO/relay'den geçmedi — slot atlandı")
        return None

    # ── Havuz (çok şehirli) — camera_pool.json ───────────────────────────

    def _pool_cities(self) -> dict:
        """camera_pool.json'dan AKTİF sabit kameralar, şehir bazlı dict.
        Ankara mobil (ankara_ego) hariç — o ayrı EGO path ile üretilir."""
        try:
            data = json.loads(Path(POOL_PATH).read_text(encoding="utf-8"))
        except Exception as e:
            self.log.warning(f"camera_pool.json okunamadı: {e}")
            return {}
        out = {}
        for c in data.get("cameras", []):
            if c.get("status") != "active":
                continue
            if c.get("type") in POOL_EXCLUDE_TYPES:
                continue
            out.setdefault(c.get("city", "?"), []).append(c)
        return out

    def _recent_cams(self, hours: int = CAM_DEDUP_HOURS) -> set:
        if not USED_CAMS_FILE.exists():
            return set()
        try:
            data = json.loads(USED_CAMS_FILE.read_text(encoding="utf-8"))
            cutoff = datetime.now() - timedelta(hours=hours)
            return {k for k, ts in data.items()
                    if datetime.fromisoformat(ts) > cutoff}
        except Exception:
            return set()

    def _record_cam(self, cam_key: str):
        if not cam_key:
            return
        data = {}
        if USED_CAMS_FILE.exists():
            try:
                data = json.loads(USED_CAMS_FILE.read_text(encoding="utf-8"))
            except Exception:
                data = {}
        data[cam_key] = datetime.now().isoformat()
        cutoff = datetime.now() - timedelta(hours=CAM_DEDUP_HOURS * 2)
        data = {k: ts for k, ts in data.items()
                if datetime.fromisoformat(ts) > cutoff}
        USED_CAMS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = USED_CAMS_FILE.with_suffix(".json.tmp")
        try:
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            tmp.replace(USED_CAMS_FILE)
        except Exception:
            pass

    def _recent_cities(self, n: int = CITY_ANTIREPEAT) -> list:
        try:
            data = json.loads(USED_CITIES_FILE.read_text(encoding="utf-8"))
            return list(data.get("recent", []))[-n:]
        except Exception:
            return []

    def _record_city(self, city: str):
        recent = self._recent_cities(n=10) + [city]
        USED_CITIES_FILE.parent.mkdir(parents=True, exist_ok=True)
        try:
            USED_CITIES_FILE.write_text(
                json.dumps({"recent": recent[-10:]}, ensure_ascii=False),
                encoding="utf-8")
        except Exception:
            pass

    def _pick_city(self) -> Optional[str]:
        """Bu slot için şehir seç: Ankara (EGO hazırsa) + havuz şehirleri,
        son CITY_ANTIREPEAT slotta kullanılanları ele, kalanlardan random."""
        choices = list(self._pool_cities().keys())
        if self.registry and self.recorder:
            choices.append("Ankara")
        if not choices:
            return None
        recent = set(self._recent_cities())
        fresh = [c for c in choices if c not in recent]
        pool = fresh or choices   # hepsi yakın zamanda kullanıldıysa yine de seç
        return random.choice(pool)

    def _city_weather(self, city: str) -> Optional[dict]:
        """Şehir için hava durumu (best-effort). Koordinat yoksa/başarısızsa None."""
        if not self.owm_key:
            return None
        coords = POOL_CITY_COORDS.get(city)
        if not coords:
            return None
        try:
            return get_weather(lat=coords[0], lon=coords[1], api_key=self.owm_key)
        except Exception:
            return None

    def produce_pool_city(self, city: str, cams: list,
                          weather: Optional[dict]) -> Optional[tuple[str, str]]:
        """Havuz şehrinden: kameraları karıştır → resolve → kaydet → ZORUNLU YOLO.
        İlk geçen klibi döndür. Returns (clip_path, cam_key) veya None."""
        if not self.pool_recorder:
            self.log.error("GenericRecorder hazır değil")
            return None
        if not cams:
            self.log.warning(f"{city}: havuzda aktif kamera yok")
            return None

        cams = list(cams)
        random.shuffle(cams)
        used = self._recent_cams()
        ordered = ([c for c in cams if (c.get("name", "") + c.get("city", "")) not in used]
                   + [c for c in cams if (c.get("name", "") + c.get("city", "")) in used])
        ordered = ordered[:POOL_MAX_CANDIDATES]

        now = datetime.now()
        for idx, cam in enumerate(ordered, 1):
            name = cam.get("name", city)
            cam_key = cam.get("name", "") + cam.get("city", "")
            try:
                url = resolvers.resolve(cam)
            except Exception as e:
                self.log.warning(f"{city} {idx}: resolve hata: {e}")
                continue
            if not url:
                self.log.info(f"{city} {idx}/{len(ordered)} → resolve boş, atla")
                continue
            self.log.info(f"{city} {idx}/{len(ordered)} → {name}")
            try:
                clip = self.pool_recorder.record_url(
                    url, name, now, headers=cam.get("headers") or {})
            except Exception as e:
                self.log.error(f"{name}: kayıt hata: {e}")
                continue
            if clip:
                self.log.info(f"✓ {name} klip hazır ({idx}/{len(ordered)} denendi)")
                return clip, cam_key

        self.log.warning(
            f"{city}: {len(ordered)} aday denendi, YOLO'dan geçen yok — slot atlandı")
        return None

    # ── Metadata ─────────────────────────────────────────────────────────

    def _build_metadata(self, now: datetime,
                        weather: Optional[dict]) -> dict:
        date_short = f"{now.day}/{now.month}/{now.year} {now.strftime('%H:%M')}"
        gun = GUNLER[now.weekday()]
        gun_saat = f"{gun} {now.strftime('%H:%M')}"
        temp = weather["temp"] if weather else "?"
        weather_short = (f"{weather['condition']} {weather['temp']}°C"
                         if weather else "Canlı")

        # Random başlık (Repetitive Content cezasından kaçınma)
        template = random.choice(TITLE_TEMPLATES)
        title = template.format(
            date_short=date_short, gun=gun, gun_saat=gun_saat,
            temp=temp, weather_short=weather_short,
        )

        tags = ["canlı kamera", "canlı trafik", "shorts", "trafik",
                "turkey", "türkiye", "live camera", "şehir", "kamera",
                "sokak", "şehir kamerası", "canlı yayın"]

        weather_line = ""
        if weather:
            weather_line = (f"\n☁️ Hava: {weather['emoji']} "
                            f"{weather['temp']}°C {weather['condition']}\n")

        description = (
            f"Canlı şehir kamera görüntüleri — sokaktan canlı kareler.\n"
            f"📅 {turkce_tarih(now)}, saat {now.strftime('%H:%M')}.\n"
            f"{weather_line}"
            f"\n👇 Sizce burası neresi? Tahminini yorumlara yaz!\n"
            f"🔔 Her saat yeni bir canlı kamera için ABONE OL.\n"
            f"\n🎥 Otomatik üretim — KameraShorts.\n"
            f"\n" + HASHTAGS
        )

        # TTS: tarih + hava + 2 CTA (yorum + abone) — ŞEHİR ADI SÖYLENMİYOR
        tts_text = f"{turkce_tarih(now)}, saat {now.strftime('%H:%M')}."
        if weather:
            tts_text += f" Hava {weather['condition']}, {weather['temp']} derece."
        tts_text += " Sizce burası neresi? Tahmininizi yorumlara yazın."
        tts_text += " Beğendiyseniz abone olun, her saat yeni bir canlı kamera!"

        return {
            "title": title[:100],
            "description": description,
            "tags": tags,
            "city": "Türkiye",   # drawtext/overlay'de Ankara yerine generic
            "tts_text": tts_text,
            "category_id": "22",
        }

    # ── TikTok caption (Telegram'a video ile gönderilir, manuel upload için) ─

    def _build_tiktok_caption(self, now: datetime,
                              weather: Optional[dict]) -> str:
        """TikTok için hazır caption — kopyala-yapıştır. Şehir adı yok."""
        date_str = f"{now.day} {AYLAR[now.month-1]} {GUNLER[now.weekday()]}"
        weather_str = (f"{weather['condition']} {weather['temp']}°C"
                       if weather else "Canlı")
        return (
            f"Türkiye'den canlı kareler 🔴 Sizce burası neresi? 👇\n"
            f"{date_str} • {weather_str}\n\n"
            f"#türkiye #canlı #şehir #keşfet #fyp #trafik #sokak #foryou"
        )

    # ── CTA Overlays (ABONE OL + YORUM) — algoritma engagement booster ───

    def _add_cta_overlays(self, clip_path: str) -> str:
        """AudioMixer'dan sonra: 15-18s "ABONE OL" + 30-40s "YORUM YAZ" CTA.

        Türkçe karakterler GERÇEK haliyle (SİZCE BURASI NERESİ) — DejaVu Sans Bold
        Türkçe gliflerini render eder. ▶ ▼ geometrik şekilleri de desteklenir.
        Renkli kutu + büyük yazı + zaman-bazlı enable filter.
        """
        clip = Path(clip_path)
        if not clip.exists():
            return clip_path
        out = clip.parent / (clip.stem + "_cta.mp4")
        font = FONT_PATH

        # 15-18s arası ABONE OL (kırmızı arka plan, beyaz yazı, ekran ortasında alt 1/3)
        # 30-40s arası YORUM YAZ (sarı arka plan, siyah yazı, üst 1/4)
        vf = (
            f"drawtext=fontfile={font}:text='▶ ABONE OL':"
            f"x=(w-text_w)/2:y=h*0.7:fontsize=72:fontcolor=white:"
            f"box=1:boxcolor=red@0.9:boxborderw=24:"
            f"shadowx=3:shadowy=3:shadowcolor=black@0.8:"
            f"enable='between(t\\,15\\,18)',"
            f"drawtext=fontfile={font}:text='▼ SİZCE BURASI NERESİ?':"
            f"x=(w-text_w)/2:y=h*0.15:fontsize=54:fontcolor=black:"
            f"box=1:boxcolor=yellow@0.95:boxborderw=18:"
            f"shadowx=2:shadowy=2:shadowcolor=black@0.6:"
            f"enable='gt(t\\,30)',"
            f"drawtext=fontfile={font}:text='Yorumlara yaz ▼':"
            f"x=(w-text_w)/2:y=h*0.22:fontsize=38:fontcolor=white:"
            f"box=1:boxcolor=black@0.75:boxborderw=10:"
            f"enable='gt(t\\,30)'"
        )
        cmd = [
            self.ffmpeg, "-y", "-hide_banner", "-loglevel", "warning",
            "-i", str(clip),
            "-vf", vf,
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
            "-c:a", "copy",
            "-movflags", "+faststart",
            str(out),
        ]
        try:
            result = subprocess.run(
                cmd, capture_output=True, timeout=180, check=False,
            )
            if result.returncode != 0 or not out.exists() or out.stat().st_size < 100_000:
                err = (result.stderr[-300:].decode("utf-8", errors="replace")
                       if result.stderr else "")
                self.log.warning(f"CTA overlay hatası, orjinal ile devam: {err[:200]}")
                return str(clip)
            # Orjinali sil, CTA versiyonunu orjinal yola taşı
            clip.unlink(missing_ok=True)
            out.rename(clip)
            self.log.info("CTA overlay eklendi (ABONE OL + YORUM)")
            return str(clip)
        except subprocess.TimeoutExpired:
            self.log.warning("CTA overlay timeout, orjinal ile devam")
            return str(clip)
        except Exception as e:
            self.log.warning(f"CTA overlay exception: {e}")
            return str(clip)

    # ── Ana akış ────────────────────────────────────────────────────────

    def _upload_clip(self, clip: str, metadata: dict, city: str,
                     s: dict, do_upload: bool) -> bool:
        """YouTube upload (veya kota dolu → kuyruk). Telegram bildirimi OPERATÖRE
        (özel) gerçek şehir adıyla gider; PUBLIC metadata anonim kalır."""
        if not do_upload:
            self.log.info("Upload atlandı (--no-upload)")
            s["last_status"] = "produced_only"
            s["success"] += 1
            return True
        try:
            uploader = YouTubeUploader(self.config)
            if uploader.check_quota():
                result = uploader.upload(clip, metadata)
                youtube_url = result.get("url", "")
                self.log.info(f"✓ Yüklendi: {youtube_url}")
                s["success"] += 1
                s["last_status"] = "uploaded"
                s["last_youtube_url"] = youtube_url
                try:
                    self.notifier.video_uploaded(
                        city, metadata["title"], youtube_url, city)
                except Exception:
                    pass
                return True
            self.log.info("Kota dolu → kuyruğa")
            uploader.add_to_queue(clip, metadata)
            s["queued"] += 1
            s["last_status"] = "queued"
            try:
                self.notifier.quota_warning(city)
            except Exception:
                pass
            return True
        except Exception as e:
            self.log.error(f"Upload hatası, kuyruğa: {e}")
            s["last_error"] = str(e)[:200]
            try:
                uploader = YouTubeUploader(self.config)
                uploader.add_to_queue(clip, metadata)
                s["queued"] += 1
                s["last_status"] = "queued"
                return True
            except Exception:
                s["last_status"] = "upload_fail"
                s["failed"] += 1
                return False

    def run_slot(self, do_upload: bool = True, force_city: Optional[str] = None):
        """Tek slot — BİR şehir seç (Ankara EGO veya havuz şehri), Shorts üret + upload.
        Şehir adı PUBLIC içerikte gizli; içerik üretimi şehre göre değişir."""
        now = datetime.now()

        # 1. Şehir seç (anti-repeat). --city ile zorlanabilir.
        city = force_city or self._pick_city()
        if not city:
            self.log.warning(
                "Üretilecek şehir yok (havuz boş + Ankara hazır değil) → slot atlandı")
            return
        self.log.info(
            f"╔══ Shorts slot başladı {now.strftime('%H:%M')} — şehir: {city} ══╗")

        s = self._bucket(city)
        s["attempts"] += 1
        s["last_run"] = now.isoformat()

        # 2. Hava durumu (best-effort, şehir bazlı)
        weather = (get_weather("ankara", api_key=self.owm_key) if city == "Ankara"
                   else self._city_weather(city))
        if weather:
            self.log.info(
                f"Hava: {weather['emoji']} {weather['temp']}°C {weather['condition']}")

        # 3. Üret — Ankara → EGO otobüs, diğer → havuz kamerası
        dedup_kind = dedup_key = None
        if city == "Ankara":
            result = self.produce_ankara(weather)
            if result:
                _, dedup_key = result
                dedup_kind = "plate"
                s["last_plate"] = dedup_key
                s["last_batch"] = "direct_ego"
                s["last_camera"] = "EGO otobüs"
        else:
            cams = self._pool_cities().get(city, [])
            result = self.produce_pool_city(city, cams, weather)
            if result:
                _, dedup_key = result
                dedup_kind = "cam"
                s["last_batch"] = "pool"
                s["last_camera"] = dedup_key

        if not result:
            self.log.warning(f"{city}: üretim başarısız → slot atlanıyor")
            s["last_status"] = "produce_fail"
            s["failed"] += 1
            self._record_city(city)   # boş şehri anti-repeat'e ekle (sıradakini dene)
            self._save_stats()
            return
        clip = result[0]

        # 4. Metadata — ŞEHİR GİZLİ (generic "Türkiye", "Sizce burası neresi?")
        metadata = self._build_metadata(now, weather)
        self.log.info(f"Başlık: {metadata['title']}")

        # 5. Audio mix (TTS + ambient + sağ üst hava drawtext — Türkçe karakterli)
        try:
            clip = self.mixer.add_audio(
                clip, metadata, location="Türkiye", weather=weather, duration=40)
            self.log.info("Ses karıştırıldı (TTS dahil)")
        except Exception as e:
            self.log.warning(f"Ses ekleme hatası (orjinal video ile devam): {e}")

        # 6. CTA overlay (15-18s ABONE OL + 30s+ SİZCE BURASI NERESİ)
        try:
            clip = self._add_cta_overlays(clip)
        except Exception as e:
            self.log.warning(f"CTA overlay hatası (orjinal ile devam): {e}")

        # 7. TikTok için: video + caption Telegram'a (upload ÖNCESİ — uploader siler)
        try:
            tiktok_caption = self._build_tiktok_caption(now, weather)
            if self.notifier.send_video(clip, tiktok_caption):
                self.log.info("📲 TikTok videosu + caption Telegram'a gönderildi")
            else:
                self.log.info("Telegram video gönderilemedi (devam)")
        except Exception as e:
            self.log.warning(f"TikTok Telegram gönderim hatası: {e}")

        # 8. Upload
        upload_success = self._upload_clip(clip, metadata, city, s, do_upload)

        # 9. Dedup kaydı (başarılı/kuyrukta) + şehir anti-repeat
        if upload_success and dedup_key:
            if dedup_kind == "plate":
                try:
                    self._record_plate(dedup_key)
                    used = self._recent_plates()
                    s["unique_plates_24h"] = len(used)
                    self.log.info(
                        f"Plaka kaydedildi: {dedup_key} (son 24h: {len(used)} farklı)")
                except Exception as e:
                    self.log.warning(f"Plaka kaydı hatası: {e}")
            elif dedup_kind == "cam":
                self._record_cam(dedup_key)
                self.log.info(f"Kamera dedup kaydedildi ({CAM_DEDUP_HOURS}h)")
        self._record_city(city)
        self._save_stats()
        self.log.info("╚══ slot bitti ══╝")

    # ── Daemon ───────────────────────────────────────────────────────────

    def run_daemon(self):
        hcfg = self.config.get("harvester", {})
        ankara_minute = hcfg.get("ankara_minute", 15)

        def _run_if_active():
            """Saat filtresi: zayıf performanslı saatler atlanır."""
            h = datetime.now().hour
            if h not in ACTIVE_HOURS:
                self.log.info(
                    f"Saat {h:02d} aktif pencerede değil "
                    f"(SKIP: 00-05, 07, 20), slot atlandı")
                return
            self.run_slot()

        schedule.every().hour.at(f":{ankara_minute:02d}").do(_run_if_active)
        active_str = ", ".join(f"{h:02d}" for h in sorted(ACTIVE_HOURS))
        self.log.info(
            f"⏰ Zamanlayıcı: Her saat :{ankara_minute:02d} "
            f"(aktif saatler: {active_str})")
        try:
            pc = list(self._pool_cities().keys())
        except Exception:
            pc = []
        self.log.info(
            "ℹ Stream'den BAĞIMSIZ servis — ZORUNLU YOLO (fail-closed), şehir gizli")
        self.log.info(
            f"ℹ ÇOK ŞEHİRLİ: her slot 1 şehir — Ankara (EGO) + havuz {pc}")
        self.log.info(
            "ℹ Algoritma dostu: 5 random başlık, hashtag, görsel+işitsel CTA")

        try:
            self.notifier.send(
                "🎬 Çok şehirli Shorts servisi başladı (stream'den bağımsız)")
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
    parser.add_argument("--city", default=None,
                        help="--once ile: belirli şehri zorla (ör. İstanbul, Kayseri, Ankara)")
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()

    h = AnkaraShortsProducer(config_path=args.config)

    if args.once:
        h.run_slot(do_upload=not args.no_upload, force_city=args.city)
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
