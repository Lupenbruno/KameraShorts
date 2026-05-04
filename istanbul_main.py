"""İstanbul turistik kameraları → YouTube pipeline (3 dakikalık landscape videolar)."""
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

from src.istanbul_registry import IstanbulRegistry
from src.istanbul_recorder import IstanbulRecorder
from src.istanbul_title_generator import IstanbulTitleGenerator
from src.youtube_uploader import YouTubeUploader
from src.audio_mixer import AudioMixer


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
    return logging.getLogger("istanbul_pipeline")


class IstanbulApp:
    def __init__(self, config_path: str = "config.yaml"):
        with open(config_path, encoding="utf-8") as f:
            self.config = yaml.safe_load(f)
        log_path = self.config["paths"].get("istanbul_log_path", "logs/istanbul_pipeline.log")
        self.log = setup_logging(log_path)
        self.registry = IstanbulRegistry()
        self.recorder = IstanbulRecorder(self.config)
        self.titler = IstanbulTitleGenerator()
        # İstanbul için ayrı token/credentials yolu
        istanbul_cfg = dict(self.config)
        istanbul_youtube = self.config.get("istanbul_youtube") or self.config["youtube"]
        istanbul_cfg["youtube"] = istanbul_youtube
        istanbul_cfg["paths"] = dict(self.config["paths"])
        istanbul_cfg["paths"]["log_path"] = self.config["paths"].get("istanbul_log_path", "logs/istanbul_pipeline.log")
        istanbul_cfg["paths"]["queue_path"] = self.config["paths"].get("istanbul_queue_path", "data/queue/istanbul_upload_queue.json")
        self.uploader = YouTubeUploader(istanbul_cfg)
        self.mixer = AudioMixer(self.config)

    def record_only(self, count: int = 1):
        """Sadece klip çeker ve meta.json kaydeder. Upload yapmaz."""
        import json as _json
        now = datetime.now()
        self.log.info(f"=== {now.strftime('%d/%m/%Y %H:%M')} — Istanbul kayit modunda basliyor ===")

        all_cams = self.registry.get_all_cameras()
        cameras = self.registry.get_random_cameras(count=min(len(all_cams), max(count * 4, 12)))
        self.log.info(f"{len(cameras)} aday kamera, {count} klip hedefleniyor")

        success, tried, filtered = 0, 0, 0
        for camera in cameras:
            if success >= count:
                break
            tried += 1
            cam_name = camera["name"]
            self.log.info(f"[{cam_name}] kayit basliyor...")

            if not self.registry.check_stream(camera):
                filtered += 1
                self.log.warning(f"[{cam_name}] stream erisimliyor, atlaniyor")
                continue

            clip_path = self.recorder.record(camera, now)
            if not clip_path:
                filtered += 1
                self.log.warning(f"[{cam_name}] clip alinamadi (donuk/hata), atlaniyor")
                continue

            metadata = self.titler.generate(camera, now)
            self.log.info(f"[{cam_name}] baslik: {metadata['title']}")

            tts_text = f"{camera['location']}. {turkce_tarih(now)}, saat {now.strftime('%H:%M')}."
            metadata["tts_text"] = tts_text
            clip_path = self.mixer.add_audio(clip_path, metadata, camera["location"])
            self.log.info(f"[{cam_name}] ses eklendi")

            meta = {k: v for k, v in metadata.items() if k != "tts_text"}
            meta.update({"city": "istanbul", "clip_path": clip_path,
                         "recorded_at": now.isoformat(), "uploaded": False, "youtube_url": None})
            meta_path = Path(clip_path).with_suffix(".meta.json")
            meta_path.write_text(_json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

            self.log.info(f"[{cam_name}] HAZIR: {Path(clip_path).name}")
            success += 1

        self.log.info(f"=== KAYIT TAMAM: {success} klip / {tried} denendi / {filtered} elendi ===")

    def run_once(self, count: int = 6, upload: bool = True):
        now = datetime.now()
        self.log.info(f"=== {now.strftime('%d/%m/%Y %H:%M')} — İstanbul pipeline başlıyor ===")

        # Tüm kameraları karıştır, count * 3 aday dene
        all_cams = self.registry.get_all_cameras()
        cameras = self.registry.get_random_cameras(count=min(len(all_cams), max(count * 4, 12)))
        self.log.info(f"{len(cameras)} aday kamera, {count} başarılı video hedefleniyor")

        success = 0
        for camera in cameras:
            if success >= count:
                break

            cam_name = camera["name"]
            self.log.info(f"[{cam_name}] kayıt başlıyor...")

            # Stream erişilebilir mi kontrol et
            if not self.registry.check_stream(camera):
                self.log.warning(f"[{cam_name}] stream erişilemiyor, atlanıyor")
                continue

            # Klip çek
            clip_path = self.recorder.record(camera, now)
            if not clip_path:
                self.log.warning(f"[{cam_name}] klip alınamadı, atlanıyor")
                continue

            # Metadata oluştur
            metadata = self.titler.generate(camera, now)
            self.log.info(f"[{cam_name}] başlık: {metadata['title']}")

            # TTS + ambient ses ekle
            tts_text = f"{camera['location']}. {turkce_tarih(now)}, saat {now.strftime('%H:%M')}."
            metadata["tts_text"] = tts_text
            clip_path = self.mixer.add_audio(clip_path, metadata, camera["location"])
            self.log.info(f"[{cam_name}] ses eklendi")

            # YouTube'a yükle
            if upload:
                if self.uploader.check_quota():
                    result = self.uploader.upload(clip_path, metadata)
                    self.log.info(f"[{cam_name}] yüklendi: {result['url']}")
                    success += 1
                else:
                    self.log.warning(f"[{cam_name}] günlük kota doldu, kuyruğa eklendi")
                    self.uploader.add_to_queue(clip_path, metadata)
            else:
                self.log.info(f"[{cam_name}] klip hazır (upload atlandı): {clip_path}")
                import subprocess as sp
                sp.Popen([clip_path], shell=True)
                success += 1

        self.log.info(f"=== Tamamlandı: {success} video başarıyla yüklendi ===")

    def run_daemon(self):
        times = self.config.get("istanbul", {}).get("times", ["06:00", "09:00", "12:00", "15:00", "18:00", "21:00"])
        count = self.config.get("istanbul", {}).get("videos_per_slot", 1)
        for t in times:
            schedule.every().day.at(t).do(self.run_once, count=count)
            self.log.info(f"Zamanlayıcı: her gün {t} → {count} video")

        self.log.info("İstanbul daemon modu başlatıldı. Ctrl+C ile dur.")
        while True:
            schedule.run_pending()
            time.sleep(30)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="İstanbul Kamera Pipeline")
    parser.add_argument("--now", action="store_true", help="Hemen çalıştır")
    parser.add_argument("--daemon", action="store_true", help="Günlük zamanlayıcı")
    parser.add_argument("--count", type=int, default=6, help="Kaç video")
    parser.add_argument("--no-upload", action="store_true", help="YouTube'a yükleme")
    parser.add_argument("--record-only", action="store_true", help="Sadece kaydet, upload yapma")
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()

    app = IstanbulApp(config_path=args.config)

    if getattr(args, 'record_only', False):
        app.record_only(count=args.count)
    elif args.now:
        app.run_once(count=args.count, upload=not args.no_upload)
    elif args.daemon:
        app.run_daemon()
    else:
        parser.print_help()
