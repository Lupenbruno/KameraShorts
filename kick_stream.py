"""Kick 7/24 canli yayin — YouTube beklenmeden direkt baslar.

Kullanim:
  python kick_stream.py          # baslat
  python kick_stream.py --stop   # durdur
"""
import argparse
import logging
import os
import signal
import yaml
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    handlers=[
        logging.FileHandler("logs/kick_stream.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("kamerashorts")


class _NoChat:
    """YouTube olmadan SuperChat yok — dummy sinif."""
    def poll_superchat(self, chat_id, page_token=None):
        return [], page_token


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--stop", action="store_true")
    args = parser.parse_args()

    with open(args.config, encoding="utf-8") as f:
        config = yaml.safe_load(f)

    if args.stop:
        _stop()
        return

    _start(config)


def _start(config: dict):
    from src.live_controller import LiveController

    kick_url = config.get("kick", {}).get("rtmp_url", "")
    if not kick_url:
        log.error("config.yaml'da kick.rtmp_url yok!")
        return

    log.info(f"Kick stream basliyor: {kick_url[:50]}...")
    Path("data/kick_stream.pid").write_text(str(os.getpid()))

    # Kick-only modda tee muxer olmasin — config'den kick kaldir
    kick_only_config = {k: v for k, v in config.items() if k != "kick"}

    controller = LiveController(
        config   = kick_only_config,
        rtmp_url = kick_url,
        chat_id  = "",
        yt_live  = _NoChat(),
    )

    try:
        controller.run()
    except KeyboardInterrupt:
        log.info("Durduruldu.")
        controller._kill_ffmpeg()


def _stop():
    pid_file = Path("data/kick_stream.pid")
    if pid_file.exists():
        pid = int(pid_file.read_text().strip())
        try:
            os.kill(pid, signal.SIGTERM)
            log.info(f"Kick stream durduruldu (PID {pid})")
        except ProcessLookupError:
            log.warning("Process zaten durmus")
        pid_file.unlink()
    else:
        log.warning("PID dosyasi bulunamadi")


if __name__ == "__main__":
    main()
