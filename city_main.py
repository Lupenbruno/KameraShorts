"""Generic Türk şehir kamera pipeline — config.yaml'dan city key alır.

Kullanım:
  python city_main.py --city corum --daemon
  python city_main.py --city konya --now --count 3
  python city_main.py --city corum --record-only --count 1
"""
import argparse
import json as _json
import logging
import schedule
import time
import yaml
from datetime import datetime
from pathlib import Path

from src.generic_registry import GenericRegistry
from src.generic_recorder import GenericRecorder
from src.city_title_generator import CityTitleGenerator
from src.youtube_uploader import YouTubeUploader
from src.audio_mixer import AudioMixer
from src.notifier import TelegramNotifier
from src.weather import get_weather

AYLAR = ["Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran",
         "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık"]
GUNLER = ["Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar"]


def turkce_tarih(dt: datetime) -> str:
    return f"{dt.day} {AYLAR[dt.month - 1]} {GUNLER[dt.weekday()]}"


class CityApp:
    def __init__(self, city_key: str, config_path: str = "config.yaml"):
        with open(config_path, encoding="utf-8") as f:
            self.config = yaml.safe_load(f)

        cities = self.config.get("cities", {})
        if city_key not in cities:
            raise ValueError(
                f"'{city_key}' config.yaml > cities altında tanımlı değil. "
                f"Mevcut şehirler: {list(cities.keys())}"
            )

        self.city_key = city_key
        self.city_cfg = cities[city_key]
        self.city_name = self.city_cfg["name"]

        # Logging
        log_path = self.city_cfg["log_path"]
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)-8s %(message)s",
            handlers=[
                logging.FileHandler(log_path, encoding="utf-8"),
                logging.StreamHandler(),
            ],
        )
        self.log = logging.getLogger(f"{city_key}_pipeline")

        # Registry
        cameras = self.city_cfg["cameras"]
        index_file = f"data/{city_key}_cam_index.json"
        self.registry = GenericRegistry(cameras, index_file)

        # Recorder (landscape 1280x720 varsayılan, dikey için vertical: true)
        self.recorder = GenericRecorder(
            clips_dir=self.city_cfg["clips_dir"],
            duration=self.city_cfg.get("clip_duration", 180),
            ffmpeg_path=self.config.get("ffmpeg_path"),
            vertical=self.city_cfg.get("vertical", False),
        )

        # Title generator
        yt_cfg = self.city_cfg.get("youtube", {})
        self.titler = CityTitleGenerator(
            city_name=self.city_name,
            tags_base=yt_cfg.get("tags", [self.city_name, "canlı kamera", "turkey"]),
            city_key=self.city_key,
        )

        # Uploader
        uploader_cfg = dict(self.config)
        uploader_cfg["youtube"] = self.city_cfg["youtube"]
        uploader_cfg["paths"] = {
            "log_path": self.city_cfg["log_path"],
            "queue_path": self.city_cfg["queue_path"],
        }
        self.uploader = YouTubeUploader(uploader_cfg)

        # Audio mixer & Telegram notifier
        self.mixer = AudioMixer(self.config)
        self.notifier = TelegramNotifier(self.config)

        # Hava durumu API anahtarı
        self.owm_key = self.config.get("openweathermap_api_key", "")

    # ------------------------------------------------------------------
    def run_once(self, count: int = 1, upload: bool = True):
        now = datetime.now()
        self.log.info(
            f"=== {now.strftime('%d/%m/%Y %H:%M')} — {self.city_name} pipeline başlıyor ==="
        )

        cameras = self.registry.get_next_cameras(count=count * 4)
        self.log.info(
            f"{len(cameras)} aday kamera, {count} başarılı video hedefleniyor"
        )

        # Hava durumu — slot başında bir kere çek (10 dk cache'den gelir)
        weather = get_weather(city_key=self.city_key, api_key=self.owm_key)
        if weather:
            self.log.info(
                f"Hava durumu: {weather['emoji']} {weather['temp']}°C {weather['condition']}"
            )

        success = 0
        for camera in cameras:
            if success >= count:
                break

            cam_name = camera["name"]
            self.log.info(f"[{cam_name}] kayıt başlıyor...")

            if not self.registry.check_stream(camera):
                self.log.warning(f"[{cam_name}] stream erişilemiyor, atlanıyor")
                continue

            clip_path = self.recorder.record(camera, now)
            if not clip_path:
                self.log.warning(f"[{cam_name}] klip alınamadı, atlanıyor")
                continue

            metadata = self.titler.generate(camera, now, weather=weather)
            metadata["city"] = self.city_name
            self.log.info(f"[{cam_name}] başlık: {metadata['title']}")

            tts_text = (
                f"{camera['location']}. "
                f"{turkce_tarih(now)}, saat {now.strftime('%H:%M')}."
            )
            metadata["tts_text"] = tts_text
            clip_path = self.mixer.add_audio(clip_path, metadata, camera["location"], weather=weather)
            self.log.info(f"[{cam_name}] ses eklendi")

            if upload:
                if self.uploader.check_quota():
                    try:
                        result = self.uploader.upload(clip_path, metadata)
                        self.log.info(f"[{cam_name}] yüklendi: {result['url']}")
                        self.notifier.video_uploaded(
                            cam_name, metadata["title"], result["url"], self.city_name
                        )
                        success += 1
                    except Exception as e:
                        self.log.error(f"[{cam_name}] YouTube yukleme hatasi: {e}, kuyruğa eklendi")
                        self.uploader.add_to_queue(clip_path, metadata)
                else:
                    self.log.warning(
                        f"[{cam_name}] günlük kota doldu, kuyruğa eklendi"
                    )
                    self.notifier.quota_warning(self.city_name)
                    self.uploader.add_to_queue(clip_path, metadata)
            else:
                self.log.info(
                    f"[{cam_name}] klip hazır (upload atlandı): {clip_path}"
                )
                success += 1

        self.log.info(f"=== Tamamlandı: {success} video ===")

    # ------------------------------------------------------------------
    def record_only(self, count: int = 1):
        """Sadece kaydet + ses ekle, YouTube'a yükleme."""
        now = datetime.now()
        self.log.info(
            f"=== {now.strftime('%d/%m/%Y %H:%M')} — {self.city_name} kayıt modu ==="
        )

        cameras = self.registry.get_next_cameras(count=count * 4)
        success = 0
        weather = get_weather(city_key=self.city_key, api_key=self.owm_key)

        for camera in cameras:
            if success >= count:
                break

            cam_name = camera["name"]
            self.log.info(f"[{cam_name}] kayıt başlıyor...")

            if not self.registry.check_stream(camera):
                self.log.warning(f"[{cam_name}] stream erişilemiyor, atlanıyor")
                continue

            clip_path = self.recorder.record(camera, now)
            if not clip_path:
                self.log.warning(f"[{cam_name}] klip alınamadı, atlanıyor")
                continue

            metadata = self.titler.generate(camera, now, weather=weather)
            tts_text = (
                f"{camera['location']}. "
                f"{turkce_tarih(now)}, saat {now.strftime('%H:%M')}."
            )
            metadata["tts_text"] = tts_text
            clip_path = self.mixer.add_audio(clip_path, metadata, camera["location"], weather=weather)

            meta = {k: v for k, v in metadata.items() if k != "tts_text"}
            meta.update({
                "city": self.city_key,
                "clip_path": clip_path,
                "recorded_at": now.isoformat(),
                "uploaded": False,
                "youtube_url": None,
            })
            meta_path = Path(clip_path).with_suffix(".meta.json")
            meta_path.write_text(
                _json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
            )

            self.log.info(f"[{cam_name}] HAZIR: {Path(clip_path).name}")
            success += 1

        self.log.info(f"=== KAYIT TAMAM: {success} klip ===")

    # ------------------------------------------------------------------
    def run_daemon(self):
        times = self.city_cfg.get(
            "times", ["06:00", "09:00", "12:00", "15:00", "18:00", "21:00"]
        )
        count = self.city_cfg.get("videos_per_slot", 1)
        for t in times:
            schedule.every().day.at(t).do(self.run_once, count=count)
            self.log.info(f"Zamanlayıcı: her gün {t} → {count} video")

        self.notifier.system_started(self.city_name)
        self.log.info(f"{self.city_name} daemon modu başlatıldı. Ctrl+C ile dur.")
        while True:
            try:
                schedule.run_pending()
            except Exception as e:
                self.log.error(f"Daemon döngü hatası (devam ediliyor): {e}")
            time.sleep(30)


# ----------------------------------------------------------------------
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generic Türk Şehir Kamera Pipeline")
    parser.add_argument(
        "--city", required=True,
        help="config.yaml > cities altındaki şehir anahtarı (örn: corum, konya)"
    )
    parser.add_argument("--now", action="store_true", help="Hemen çalıştır")
    parser.add_argument("--daemon", action="store_true", help="Günlük zamanlayıcı")
    parser.add_argument("--count", type=int, default=1, help="Kaç video (varsayılan: 1)")
    parser.add_argument("--no-upload", action="store_true", help="YouTube'a yükleme")
    parser.add_argument("--record-only", action="store_true",
                        help="Sadece kaydet, upload yapma")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--upload-queue", action="store_true", help="Kuyruktakileri yukle")
    args = parser.parse_args()

    app = CityApp(city_key=args.city, config_path=args.config)

    if getattr(args, 'upload_queue', False):
        app.uploader.upload_queue()
    elif args.record_only:
        app.record_only(count=args.count)
    elif args.now:
        app.run_once(count=args.count, upload=not args.no_upload)
    elif args.daemon:
        app.run_daemon()
    else:
        parser.print_help()
