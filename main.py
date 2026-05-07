"""KameraShorts — Ankara EGO otobüs kameraları → YouTube Shorts pipeline."""
import argparse
import logging
import schedule
import time
import yaml
from datetime import datetime
from pathlib import Path

AYLAR = ["Ocak","Şubat","Mart","Nisan","Mayıs","Haziran",
         "Temmuz","Ağustos","Eylül","Ekim","Kasım","Aralık"]
GUNLER = ["Pazartesi","Salı","Çarşamba","Perşembe","Cuma","Cumartesi","Pazar"]

def turkce_tarih(dt: datetime) -> str:
    gun = GUNLER[dt.weekday()]
    return f"{dt.day} {AYLAR[dt.month-1]} {gun}"

import json as _json
from src.camera_registry import CameraRegistry
from src.clip_recorder import ClipRecorder
from src.geocoder import Geocoder
from src.title_generator import TitleGenerator
from src.youtube_uploader import YouTubeUploader
from src.audio_mixer import AudioMixer
from src.notifier import TelegramNotifier
from src.camera_scorer import CameraScorer

USED_PLATES_FILE = Path("data/ankara_used_plates.json")

def _load_used_plates() -> set:
    """Bugün kullanılan plakaları yükle. Tarih değiştiyse sıfırla."""
    today = datetime.now().date().isoformat()
    try:
        if USED_PLATES_FILE.exists():
            data = _json.loads(USED_PLATES_FILE.read_text(encoding="utf-8"))
            if data.get("date") == today:
                return set(data.get("plates", []))
    except Exception:
        pass
    return set()

def _save_used_plates(plates: set):
    today = datetime.now().date().isoformat()
    USED_PLATES_FILE.parent.mkdir(parents=True, exist_ok=True)
    USED_PLATES_FILE.write_text(
        _json.dumps({"date": today, "plates": list(plates)}, ensure_ascii=False),
        encoding="utf-8"
    )


def setup_logging(log_path: str) -> logging.Logger:
    Path(log_path).parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(message)s",
        handlers=[
            logging.FileHandler(log_path, encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )
    return logging.getLogger("kamerashorts")


class KameraShortsApp:
    def __init__(self, config_path: str = "config.yaml"):
        with open(config_path, encoding="utf-8") as f:
            self.config = yaml.safe_load(f)
        self.log = setup_logging(self.config["paths"]["log_path"])
        self.registry = CameraRegistry()
        self.recorder = ClipRecorder(self.config)
        self.geocoder = Geocoder()
        self.titler = TitleGenerator(self.config)
        self.uploader = YouTubeUploader(self.config)
        self.mixer = AudioMixer(self.config)
        self.notifier = TelegramNotifier(self.config)
        self.scorer = CameraScorer(ffmpeg_path=self.config.get("ffmpeg_path", "ffmpeg"))

    def record_only(self, count: int = 1):
        """Sadece klip çeker ve meta.json kaydeder. Upload yapmaz."""
        import json as _json
        now = datetime.now()
        self.log.info(f"=== {now.strftime('%d/%m/%Y %H:%M')} — kayit modunda basliyor ===")

        candidates = self.registry.get_active_cameras(limit=count * 10)
        self.log.info(f"{len(candidates)} aday kamera, {count} klip hedefleniyor")

        success, tried, filtered = 0, 0, 0
        for vehicle in candidates:
            if success >= count:
                break
            tried += 1
            plate = vehicle.get("license_plate", "?")
            self.log.info(f"[{plate}] kayit basliyor...")

            clip_path = self.recorder.record(vehicle, now)
            if not clip_path:
                filtered += 1
                self.log.warning(f"[{plate}] clip alinamadi (donuk/bulanik/offline), atlaniyor")
                continue

            lat = vehicle.get("latitude", 0)
            lon = vehicle.get("longitude", 0)
            location = self.geocoder.get_location_name(lat, lon)
            self.log.info(f"[{plate}] konum: {location}")

            metadata = self.titler.generate(vehicle, location, now)
            self.log.info(f"[{plate}] baslik: {metadata['title']}")

            tts_text = f"{location}. {turkce_tarih(now)}, saat {now.strftime('%H:%M')}."
            metadata["tts_text"] = tts_text
            clip_path = self.mixer.add_audio(clip_path, metadata, location)
            self.log.info(f"[{plate}] ses eklendi")

            meta = {k: v for k, v in metadata.items() if k != "tts_text"}
            meta.update({"city": "ankara", "clip_path": clip_path,
                         "recorded_at": now.isoformat(), "uploaded": False, "youtube_url": None})
            meta_path = Path(clip_path).with_suffix(".meta.json")
            meta_path.write_text(_json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

            self.log.info(f"[{plate}] HAZIR: {Path(clip_path).name}")
            success += 1

        self.log.info(f"=== KAYIT TAMAM: {success} klip / {tried} denendi / {filtered} elendi ===")

    def run_once(self, count: int = 4, upload: bool = True):
        now = datetime.now()
        self.log.info(f"=== {now.strftime('%d/%m/%Y %H:%M')} — pipeline başlıyor ===")

        used_plates = _load_used_plates()
        candidates = self.registry.get_active_cameras(limit=count * 20)
        # Bugün kullanılmış plakaları filtrele
        candidates = [v for v in candidates
                      if v.get("license_plate", "?") not in used_plates]
        self.log.info(f"{len(candidates)} aday kamera (bugün kullanılmamış), {count} hedefleniyor")
        # Skor sistemine göre en kalitelileri öne al
        self.log.info("Kamera kalitesi analiz ediliyor...")
        candidates = self.scorer.pick_best(candidates, now=now, top_n=count * 2)

        success = 0
        vehicles_tried = 0
        for vehicle in candidates:
            if success >= count:
                break
            vehicles_tried += 1
            plate = vehicle.get("license_plate", "?")
            self.log.info(f"[{plate}] kayıt başlıyor...")

            # Clip çek
            clip_path = self.recorder.record(vehicle, now)
            if not clip_path:
                self.log.warning(f"[{plate}] clip alınamadı (offline/hata), atlanıyor")
                continue

            # Konumu bul
            lat = vehicle.get("latitude", 0)
            lon = vehicle.get("longitude", 0)
            location = self.geocoder.get_location_name(lat, lon)
            self.log.info(f"[{plate}] konum: {location}")

            # Metadata oluştur
            metadata = self.titler.generate(vehicle, location, now)
            self.log.info(f"[{plate}] başlık: {metadata['title']}")

            # Ambient + TTS ses ekle
            tts_text = f"{location}. {turkce_tarih(now)}, saat {now.strftime('%H:%M')}."
            metadata["tts_text"] = tts_text
            clip_path = self.mixer.add_audio(clip_path, metadata, location)
            self.log.info(f"[{plate}] ses eklendi")

            # YouTube'a yükle
            if upload:
                if self.uploader.check_quota():
                    try:
                        result = self.uploader.upload(clip_path, metadata)
                        self.log.info(f"[{plate}] yüklendi: {result['url']}")
                        self.notifier.video_uploaded(plate, metadata["title"], result["url"], "ankara")
                        used_plates.add(plate)
                        _save_used_plates(used_plates)
                        success += 1
                    except Exception as e:
                        self.log.error(f"[{plate}] YouTube yukleme hatasi: {e}, kuyruğa eklendi")
                        self.uploader.add_to_queue(clip_path, metadata)
                else:
                    self.log.warning(f"[{plate}] günlük kota doldu, kuyruğa eklendi")
                    self.notifier.quota_warning("ankara")
                    self.uploader.add_to_queue(clip_path, metadata)
            else:
                self.log.info(f"[{plate}] clip hazır (upload atlandı): {clip_path}")
                import subprocess as sp, sys
                sp.Popen([clip_path], shell=True)
                used_plates.add(plate)
                _save_used_plates(used_plates)
                success += 1

        self.log.info(f"=== Tamamlandı: {success}/{vehicles_tried} denendi, {success} başarılı ===")

    def run_daemon(self):
        per_slot = self.config["schedule"].get("videos_per_slot", 4)
        for t in self.config["schedule"]["times"]:
            schedule.every().day.at(t).do(self.run_once, count=per_slot)
            self.log.info(f"Zamanlayıcı: her gün {t} → {per_slot} video")

        self.notifier.system_started("Ankara")
        self.log.info("Daemon modu başlatıldı. Ctrl+C ile dur.")
        while True:
            schedule.run_pending()
            time.sleep(30)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="KameraShorts Pipeline")
    parser.add_argument("--now", action="store_true", help="Hemen çalıştır")
    parser.add_argument("--daemon", action="store_true", help="Günlük zamanlayıcı")
    parser.add_argument("--count", type=int, default=6, help="Kaç video")
    parser.add_argument("--no-upload", action="store_true", help="YouTube'a yükleme")
    parser.add_argument("--upload-queue", action="store_true", help="Kuyruktakileri yükle")
    parser.add_argument("--record-only", action="store_true", help="Sadece kaydet, upload yapma")
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()

    app = KameraShortsApp(config_path=args.config)

    if args.upload_queue:
        app.uploader.upload_queue()
    elif getattr(args, 'record_only', False):
        app.record_only(count=args.count)
    elif args.now:
        app.run_once(count=args.count, upload=not args.no_upload)
    elif args.daemon:
        app.run_daemon()
    else:
        parser.print_help()
