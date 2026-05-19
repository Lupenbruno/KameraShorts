#!/usr/bin/env python3
"""KameraShorts v5 — Mixer.

DB'den segment'leri okur, sehir rotasyonu ile tek RTMP'ye akitir.
Tek transcode pass (scale + pad + overlay + music).

Tasarim:
- city_order'da her sehir city_duration_seconds yayinda kalir
- Her sehir blogu icin ffmpeg yeniden baslar (segment listesi degisir)
- MediaMTX gecisleri absorb eder (1-2s gap downstream'i etkilemez)
- Music = -stream_loop -1 ile sonsuz loop
- Overlay = drawtext (sehir + hava) + scale 1280x720
- Hata: 5s backoff, sehri atla, sonrakine gec
"""
import argparse
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from v5 import common, db

log = common.setup_logging("mixer")

CITY_DISPLAY = {
    "ankara": "ANKARA", "istanbul": "ISTANBUL",
    "corum": "CORUM", "konya": "KONYA",
}


def _esc(text: str) -> str:
    return text.replace("\\", "\\\\").replace("'", r"\'").replace(":", r"\:")


def get_weather_text(city: str, cfg: dict) -> str:
    """OpenWeatherMap cache'li. Hata olursa bos."""
    try:
        key = cfg.get("weather", {}).get("api_key", "")
        if not key:
            return ""
        # src/weather.py modulu kullan
        from src.weather import get_weather as _gw
        w = _gw(city, api_key=key)
        if not w:
            return ""
        return "%s %s'C" % (w.get("condition", ""), w.get("temp", "?"))
    except Exception:
        return ""


def build_city_concat(city: str, duration_s: int, work_dir: Path) -> Optional[Path]:
    """O sehrin en yeni segment'lerinden concat list olustur (duration_s kadar)."""
    rows = db.latest_segments(city, limit=80)
    if not rows:
        log.warning("[%s] segment yok, atlaniyor", city)
        return None
    # En yeniden eskiye gelmis — chronological icin reverse et
    rows.reverse()
    # duration_s'i karsilayacak kadar segment al
    total_ms = 0
    chosen = []
    target_ms = duration_s * 1000
    # En yeni olanlardan basla (son N segment city_duration_seconds kadar)
    for r in reversed(rows):
        chosen.append(r)
        total_ms += r["duration_ms"]
        if total_ms >= target_ms:
            break
    chosen.reverse()  # tekrar chronological

    if total_ms < 30000:
        log.warning("[%s] yetersiz icerik (%dms)", city, total_ms)
        return None

    concat_file = work_dir / ("concat_" + city + ".txt")
    with open(concat_file, "w", encoding="utf-8") as f:
        for r in chosen:
            f.write("file '%s'\n" % r["path"])
    log.info("[%s] concat hazir: %d segment, %.1fs", city, len(chosen), total_ms / 1000)
    return concat_file


def run_city_block(
    city: str, duration_s: int, concat_file: Path,
    mixer_cfg: dict, ffmpeg: str, weather_text: str,
) -> int:
    """Tek sehir bloku icin ffmpeg calistir. RC doner."""
    width = mixer_cfg.get("width", 1280)
    height = mixer_cfg.get("height", 720)
    fps = mixer_cfg.get("fps", 20)
    vbr = mixer_cfg.get("video_bitrate_k", 2500)
    abr = mixer_cfg.get("audio_bitrate_k", 128)
    output = mixer_cfg.get("output_rtmp", "rtmp://127.0.0.1:1935/live/stream-v5")
    music_playlist = mixer_cfg.get("music_playlist", "")
    music_volume = mixer_cfg.get("music_volume", 0.4)

    display = CITY_DISPLAY.get(city, city.upper())
    font = common.load_config().get("font_path",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")

    # Overlay: sehir adi (sol alt) + opsiyonel hava
    vf_parts = []
    vf_parts.append("scale=" + str(width) + ":" + str(height) +
                    ":force_original_aspect_ratio=decrease")
    vf_parts.append("pad=" + str(width) + ":" + str(height) +
                    ":(ow-iw)/2:(oh-ih)/2")
    vf_parts.append("fps=" + str(fps))
    vf_parts.append("setpts=N/" + str(fps) + "/TB")

    esc_d = _esc(display)
    vf_parts.append(
        "drawtext=fontfile=" + font + ":text='" + esc_d + "':"
        "x=30:y=h-90:fontsize=46:fontcolor=white:"
        "box=1:boxcolor=black@0.45:boxborderw=8:shadowx=2:shadowy=2"
    )
    if weather_text:
        esc_w = _esc(weather_text)
        vf_parts.append(
            "drawtext=fontfile=" + font + ":text='" + esc_w + "':"
            "x=30:y=h-46:fontsize=30:fontcolor=lightyellow:"
            "box=1:boxcolor=black@0.45:boxborderw=6:shadowx=1:shadowy=1"
        )
    vf = ",".join(vf_parts)

    # Audio: music varsa kullan, yoksa silent
    if music_playlist and Path(music_playlist).exists():
        audio_input = ["-stream_loop", "-1", "-f", "concat", "-safe", "0",
                       "-i", music_playlist]
        audio_map = ["-map", "0:v", "-map", "1:a:0",
                     "-af", "volume=" + str(music_volume),
                     "-c:a", "aac", "-b:a", str(abr) + "k"]
    else:
        audio_input = ["-f", "lavfi",
                       "-i", "anullsrc=channel_layout=stereo:sample_rate=44100"]
        audio_map = ["-map", "0:v", "-map", "1:a:0",
                     "-c:a", "aac", "-b:a", str(abr) + "k"]

    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "warning",
        "-re",  # 1.0x gercek zamanli rate
        "-fflags", "+genpts+discardcorrupt",
        "-f", "concat", "-safe", "0", "-i", str(concat_file),
    ] + audio_input + [
        "-vf", vf,
    ] + audio_map + [
        "-c:v", "libx264", "-preset", "ultrafast",
        "-b:v", str(vbr) + "k",
        "-maxrate", str(vbr) + "k",
        "-bufsize", str(vbr * 2) + "k",
        "-g", str(fps * 2),
        "-t", str(duration_s),  # bu kadar saniye sonra dur
        "-f", "flv",
        output,
    ]

    log.info("[mixer:%s] ffmpeg basliyor (%ds)", city, duration_s)
    t0 = time.time()
    try:
        proc = common.popen_proc(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        # ffmpeg stderr: speed satirlari + hata satirlari logla
        speed_re = re.compile(r"speed=\s*([\d.]+)x")
        last_log = time.time()
        stderr_tail = []  # son 20 satir hata icin
        for line in iter(proc.stderr.readline, b""):
            try:
                s = line.decode("utf-8", errors="replace").strip()
            except Exception:
                continue
            if not s:
                continue
            stderr_tail.append(s)
            if len(stderr_tail) > 20:
                stderr_tail.pop(0)
            m = speed_re.search(s)
            if m and (time.time() - last_log > 5):
                log.info("[mixer:%s] %s", city, s[:120])
                last_log = time.time()
            elif "error" in s.lower() or "fail" in s.lower():
                log.warning("[mixer:%s] %s", city, s[:200])
        proc.wait()
        rc = proc.returncode
        if rc != 0 and stderr_tail:
            log.error("[mixer:%s] STDERR TAIL:\n  %s", city,
                      "\n  ".join(stderr_tail[-10:]))
        log.info("[mixer:%s] ffmpeg cikti rc=%d, sure=%.0fs", city, rc, time.time() - t0)
        return rc
    except Exception as e:
        log.error("[mixer:%s] hata: %s", city, e)
        return -1


def run_mixer_loop(cfg: dict, shutdown):
    mixer_cfg = cfg.get("mixer", {})
    duration_s = mixer_cfg.get("city_duration_seconds", 240)
    city_order = mixer_cfg.get("city_order", ["ankara", "istanbul", "corum", "konya"])
    work_dir = common.WORK_DIR
    work_dir.mkdir(parents=True, exist_ok=True)
    ffmpeg = common.ffmpeg_path()

    common.start_heartbeat("mixer", interval=10)
    log.info("mixer basliyor: cities=%s duration=%ds output=%s",
             city_order, duration_s, mixer_cfg.get("output_rtmp"))

    idx = 0
    while not shutdown.stopped.is_set():
        city = city_order[idx % len(city_order)]
        idx += 1

        concat_file = build_city_concat(city, duration_s, work_dir)
        if not concat_file:
            shutdown.wait(5)
            continue

        wtext = get_weather_text(city, cfg)
        rc = run_city_block(city, duration_s, concat_file, mixer_cfg, ffmpeg, wtext)

        try:
            concat_file.unlink(missing_ok=True)
        except Exception:
            pass

        if rc != 0 and not shutdown.stopped.is_set():
            log.warning("[mixer:%s] hata sonrasi 5s backoff", city)
            shutdown.wait(5)

    log.info("mixer kapaniyor")


def main():
    cfg = common.load_config()
    shutdown = common.GracefulShutdown()
    try:
        run_mixer_loop(cfg, shutdown)
    except KeyboardInterrupt:
        pass
    log.info("Cikis.")


if __name__ == "__main__":
    main()
