#!/usr/bin/env python3
"""KameraShorts v5 — Shorts producer.

Saatlik tek-shot script (systemd timer cagirir).
DB'den uygun segment(ler) sec, YOLO subprocess ile dogrula,
40s shorts kirp + overlay + audio + YouTube upload.

YOLO subprocess: Bu process iczinde YOLO yuklemiyoruz.
Subprocess olarak v5/_yolo_check.py cagiriyoruz; o cikinca RAM serbest.

Kullanim:
    python -m v5.shorts --city ankara
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from v5 import common, db

log = common.setup_logging("shorts")

AYLAR = ["Ocak", "Subat", "Mart", "Nisan", "Mayis", "Haziran",
         "Temmuz", "Agustos", "Eylul", "Ekim", "Kasim", "Aralik"]
GUNLER = ["Pazartesi", "Sali", "Carsamba", "Persembe", "Cuma",
          "Cumartesi", "Pazar"]


def _esc(text: str) -> str:
    return text.replace("\\", "\\\\").replace("'", r"\'").replace(":", r"\:")


def yolo_check_subprocess(seg_path: str, ffmpeg: str, duration: int = 40) -> tuple[int, int]:
    """YOLO subprocess'te calistir. RAM cikista serbest kalir.
    Doner: (skor, esik). Skor >= esik = GECTI.
    """
    helper = Path(__file__).parent / "_yolo_check.py"
    try:
        result = subprocess.run(
            [sys.executable, str(helper),
             "--clip", seg_path, "--ffmpeg", ffmpeg,
             "--duration", str(duration)],
            capture_output=True, timeout=120, start_new_session=True,
        )
        out = result.stdout.decode("utf-8", errors="replace").strip()
        for line in out.splitlines():
            if line.startswith("RESULT:"):
                _, payload = line.split(":", 1)
                d = json.loads(payload)
                return int(d.get("score", 0)), int(d.get("min_score", 4))
        return 0, 4
    except Exception as e:
        log.warning("YOLO subprocess hata: %s", e)
        return 99, 4  # AI yoksa gecir


def produce_ankara_short(seg_row, weather: dict, cfg: dict, out_dir: Path) -> Optional[str]:
    """Tek segment'ten Ankara Shorts uret (1080x1920 dikey, drawtext, audio).
    Doner: hazirlanan mp4 yolu veya None.
    """
    seg_path = Path(seg_row["path"])
    if not seg_path.exists():
        log.warning("segment yok: %s", seg_path)
        return None

    shorts_cfg = cfg.get("shorts", {})
    clip_s = shorts_cfg.get("clip_seconds", 40)
    width, height = shorts_cfg.get("output_dim", [1080, 1920])
    preset = shorts_cfg.get("encoder_preset", "fast")
    crf = shorts_cfg.get("encoder_crf", 23)
    font = cfg.get("font_path", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")

    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M")
    out_path = out_dir / ("ankara_" + ts + ".mp4")

    # Dikey canvas + blurred bg + landscape content
    vf = (
        "split=2[v1][v2];"
        "[v1]scale=" + str(width) + ":" + str(height) +
        ":force_original_aspect_ratio=increase,"
        "crop=" + str(width) + ":" + str(height) + ",boxblur=20:1[bg];"
        "[v2]scale=" + str(width) + ":-2:force_original_aspect_ratio=decrease[fg];"
        "[bg][fg]overlay=(W-w)/2:(H-h)/2[base];"
        "[base]drawtext=fontfile=" + font + ":text='ANKARA':"
        "x=30:y=h-140:fontsize=52:fontcolor=white:"
        "box=1:boxcolor=black@0.55:boxborderw=10:shadowx=2:shadowy=2"
    )

    now = datetime.now()
    time_text = now.strftime(r"%H\:%M")
    if weather:
        weather_text = "%s %s'C" % (weather.get("condition", ""), weather.get("temp", "?"))
        weather_esc = _esc(weather_text)
        vf += (
            ",drawtext=fontfile=" + font + ":text='" + time_text + "  " + weather_esc + "':"
            "x=30:y=h-70:fontsize=36:fontcolor=lightyellow:"
            "box=1:boxcolor=black@0.5:boxborderw=8:shadowx=1:shadowy=1"
        )
    else:
        vf += (
            ",drawtext=fontfile=" + font + ":text='" + time_text + "':"
            "x=30:y=h-70:fontsize=36:fontcolor=lightyellow:"
            "box=1:boxcolor=black@0.5:boxborderw=8"
        )

    cmd = [
        common.ffmpeg_path(), "-y", "-hide_banner", "-loglevel", "warning",
        "-t", str(clip_s),
        "-i", str(seg_path),
        "-vf", vf,
        "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        str(out_path),
    ]
    result = subprocess.run(cmd, capture_output=True, timeout=180,
                            start_new_session=True)
    if result.returncode != 0:
        err = (result.stderr[-400:].decode("utf-8", errors="replace")
               if result.stderr else "")
        log.error("encode hata: %s", err)
        return None
    if not out_path.exists() or out_path.stat().st_size < 100_000:
        log.error("encode cikisi bos/kucuk")
        return None
    return str(out_path)


def get_weather(city: str, cfg: dict) -> Optional[dict]:
    try:
        from src.weather import get_weather as _gw
        return _gw(city, api_key=cfg.get("weather", {}).get("api_key", ""))
    except Exception as e:
        log.warning("hava durumu: %s", e)
        return None


def add_audio(clip: str, metadata: dict, weather: dict, cfg: dict) -> str:
    """src/audio_mixer.py'yi reuse et."""
    try:
        from src.audio_mixer import AudioMixer
        mixer = AudioMixer({"ffmpeg_path": common.ffmpeg_path()})
        return mixer.add_audio(clip, metadata, location="Ankara",
                               weather=weather, duration=40)
    except Exception as e:
        log.warning("audio mix atlandi: %s", e)
        return clip


def upload_youtube(clip: str, metadata: dict, cfg: dict) -> Optional[dict]:
    """src/youtube_uploader.py'yi reuse et."""
    try:
        from src.youtube_uploader import YouTubeUploader
        yt_cfg = cfg.get("youtube", {})
        adapter = {
            "youtube": {
                "client_secret_path": yt_cfg["client_secret_path"],
                "token_path": yt_cfg["token_path"],
                "playlist_id": yt_cfg.get("playlists", {}).get("ankara"),
                "daily_quota_limit": yt_cfg.get("daily_quota_per_city", 6),
            },
            "paths": {
                "queue_path": "/var/lib/kamerashorts/upload_queue.json",
                "log_path": "/var/log/kshorts-uploads.log",
            },
        }
        u = YouTubeUploader(adapter)
        if not u.check_quota():
            log.warning("YouTube kota dolu, kuyruga atildi")
            u.add_to_queue(clip, metadata)
            return None
        return u.upload(clip, metadata)
    except Exception as e:
        log.error("YouTube upload hata: %s", e)
        return None


def build_metadata(now: datetime, weather: Optional[dict]) -> dict:
    date_str = "%d/%d/%d %s" % (now.day, now.month, now.year, now.strftime("%H:%M"))
    title = date_str + " - Ankara Canli Trafik #Shorts"
    weather_line = ""
    if weather:
        weather_line = "\nHava: %s %s'C %s\n" % (
            weather.get("emoji", ""), weather.get("temp", ""),
            weather.get("condition", ""),
        )
    description = (
        "Ankara canli kamera goruntuleri.\n"
        "Tarih: " + str(now.day) + " " + AYLAR[now.month - 1] + " " +
        GUNLER[now.weekday()] + ", saat " + now.strftime("%H:%M") + "." +
        weather_line +
        "\nOtomatik uretim - KameraShorts v5."
    )
    return {
        "title": title[:100],
        "description": description,
        "tags": ["ankara", "ankara canli", "ego", "canli kamera",
                 "shorts", "trafik", "turkey"],
        "city": "Ankara",
        "category_id": "22",
        "tts_text": "Ankara, " + GUNLER[now.weekday()] +
                    ", saat " + now.strftime("%H:%M") + ".",
    }


def run_shorts(city: str, cfg: dict) -> int:
    """Tek slot icin tum akis. Doner: 0=success, 1=fail, 2=skip."""
    shorts_cfg = cfg.get("shorts", {})
    min_bright = shorts_cfg.get("min_brightness", 50)
    min_motion = shorts_cfg.get("min_motion", 5)
    max_tries = shorts_cfg.get("yolo_max_tries", 10)
    dedup_h = shorts_cfg.get("dedup_hours", 24)
    output_dir = Path("/var/lib/kamerashorts/shorts_out")

    log.info("[%s] shorts slot %s", city, datetime.now().strftime("%H:%M"))

    candidates = db.good_candidates(
        city, min_bright=min_bright, min_motion=min_motion,
        since_ts=int(time.time()) - 1800, limit=30,
    )
    if not candidates:
        log.warning("[%s] uygun segment yok, slot atlandi", city)
        return 2

    # Plate dedup (Ankara)
    if city == "ankara":
        used_plates = db.recent_plates(hours=dedup_h)
        log.info("[ankara] son %dh: %d plaka kullanilmis", dedup_h, len(used_plates))
        fresh = [c for c in candidates if c["plate"] and c["plate"] not in used_plates]
        if fresh:
            candidates = fresh
        else:
            log.info("[ankara] taze plaka yok, hepsi denenecek")

    weather = get_weather(city, cfg)
    if weather:
        log.info("[%s] hava: %s %s'C", city,
                 weather.get("condition", "?"), weather.get("temp", "?"))

    ffmpeg = common.ffmpeg_path()
    tried = 0
    for cand in candidates[:max_tries]:
        tried += 1
        plate = cand["plate"] or cand["id"]
        log.info("[%s] %d/%d deneme: %s (bright=%.0f motion=%.1f)",
                 city, tried, max_tries, plate,
                 cand["brightness"], cand["motion"])

        # YOLO kontrolu
        score, threshold = yolo_check_subprocess(cand["path"], ffmpeg, duration=40)
        log.info("[%s] YOLO: %d/%d", city, score, threshold)
        if score < threshold:
            continue

        # Encode
        clip = produce_ankara_short(cand, weather or {}, cfg, output_dir)
        if not clip:
            continue

        metadata = build_metadata(datetime.now(), weather)
        log.info("[%s] basligi: %s", city, metadata["title"])

        # Audio
        clip = add_audio(clip, metadata, weather, cfg)

        # Upload
        result = upload_youtube(clip, metadata, cfg)
        url = result.get("url", "") if result else ""

        # DB kayit
        db.mark_used(cand["id"], youtube_url=url)
        if city == "ankara" and cand["plate"]:
            db.record_plate(cand["plate"], youtube_url=url)
        if result:
            db.log_upload(result["video_id"], city, metadata["title"], url)
            log.info("[%s] BASARILI: %s", city, url)
        return 0

    log.warning("[%s] %d deneme YOLO'dan gecemedi", city, tried)
    return 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--city", default="ankara",
                        choices=["ankara", "istanbul", "corum", "konya"])
    args = parser.parse_args()
    cfg = common.load_config()
    rc = run_shorts(args.city, cfg)
    sys.exit(rc)


if __name__ == "__main__":
    main()
