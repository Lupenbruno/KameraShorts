"""Canli yayin baslangic scripti.

Kullanim:
  python live_stream.py          # baslat
  python live_stream.py --stop   # durdur
"""
import argparse
import logging
import sys
import yaml
from pathlib import Path

TITLE = "Turkiye Canli Kameralar | Ankara Istanbul Corum Konya | 7/24 CANLI"
DESCRIPTION = """Turkiye'nin dort sehrinden canli kamera yayini.
Ankara otobuslerinden, Istanbul turistik noktalarindan, Corum ve Konya sehir merkezlerinden 7/24 canli goruntuler.

Sehir degistirmek icin: ankara / istanbul / corum / konya yazin
SuperChat gondererek istediginiz sehri on plana alabilirsiniz!

Ankara | Istanbul | Corum | Konya | Canli Kamera | Turkey Live Camera"""

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    handlers=[
        logging.FileHandler("logs/live_stream.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("kamerashorts")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--stop",   action="store_true")
    args = parser.parse_args()

    with open(args.config, encoding="utf-8") as f:
        config = yaml.safe_load(f)

    if args.stop:
        _stop(config)
        return

    _start(config)


def _start(config: dict):
    from src.youtube_live import YouTubeLive
    from src.live_controller import LiveController

    yt_live = YouTubeLive(config)
    log.info("Broadcast olusturuluyor / devam ettiriliyor...")

    info = yt_live.create_or_resume(TITLE, DESCRIPTION)
    log.info(f"RTMP: {info['rtmp_url'][:60]}...")
    log.info(f"Broadcast ID: {info['broadcast_id']}")

    # PID kaydet (stop icin)
    Path("data/live_stream.pid").write_text(str(__import__("os").getpid()))

    # FFmpeg'in baglantisi kurulana kadar bekle, sonra live'a gec
    import time
    log.info("FFmpeg baglantisi bekleniyor (30s)...")

    controller = LiveController(config, info["rtmp_url"], info["chat_id"], yt_live)

    import threading
    def go_live_later():
        time.sleep(35)
        try:
            yt_live.go_live(info["broadcast_id"])
        except Exception as e:
            log.warning(f"go_live: {e}")

    threading.Thread(target=go_live_later, daemon=True).start()

    try:
        controller.run()
    except KeyboardInterrupt:
        log.info("Durduruldu.")
        controller._kill_ffmpeg()
        yt_live.end_broadcast(info["broadcast_id"])


def _stop(config: dict):
    pid_file = Path("data/live_stream.pid")
    if pid_file.exists():
        pid = int(pid_file.read_text().strip())
        import os, signal
        try:
            os.kill(pid, signal.SIGTERM)
            log.info(f"Canli yayin durduruldu (PID {pid})")
        except ProcessLookupError:
            log.warning("Process zaten durmus")
        pid_file.unlink()
    else:
        log.warning("PID dosyasi bulunamadi")


if __name__ == "__main__":
    main()
