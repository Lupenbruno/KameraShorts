#!/usr/bin/env python3
"""KameraShorts v5 — Mixer (v4-style: tek FFmpeg + FIFO + writer thread).

Tasarim:
- Tek FFmpeg sureli acik kalir; HIC kapanmaz.
- Input: named pipe (FIFO) — Python writer thread mpegts segment'leri pump'lar.
- Drawtext: textfile=path:reload=1 ile dinamik metin (sehir adi + hava).
- Sehir gecislerinde sadece drawtext dosyalari guncellenir, FFmpeg sessions
  asla kopmaz → MediaMTX publisher kopmaz → tee FFmpeg olmuyor →
  YouTube + Kick reconnect derdi yok.

Mimarisi (v4'tekine benzer):
  ┌─────────────────────────────────────────────────────┐
  │ FFmpeg (sureli acik)                                 │
  │   -i /tmp/kshorts/stream.pipe  (FIFO, mpegts)        │
  │   -i music/playlist.txt        (sonsuz loop)         │
  │   -vf scale,pad,fps,setpts,                          │
  │       drawtext=textfile=city.txt:reload=1,           │
  │       drawtext=textfile=weather.txt:reload=1         │
  │   -f flv rtmp://localhost:1935/live/stream           │
  └──────────────┬──────────────────────────────────────┘
                 ▲ besler
  ┌──────────────┴──────────────────────────────────────┐
  │ Writer thread (Python):                              │
  │   for city in [ankara, istanbul, corum, konya]:      │
  │     write_text("city.txt", "ANKARA")                 │
  │     write_text("weather.txt", "21 C")                │
  │     pump_city_segments(city, duration=180s)          │
  │       → FIFO'ya en yeni mpegts segment'leri yaz      │
  │   (sonsuz dongu)                                     │
  └──────────────────────────────────────────────────────┘
"""
import os
import re
import signal
import subprocess
import sys
import threading
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
    try:
        key = cfg.get("weather", {}).get("api_key", "")
        if not key:
            return ""
        from src.weather import get_weather as _gw
        w = _gw(city, api_key=key)
        if not w:
            return ""
        return "%s %s'C" % (w.get("condition", ""), w.get("temp", "?"))
    except Exception:
        return ""


def _atomic_write(path: Path, text: str):
    """Drawtext reload=1 ile uyumlu atomic write (kismi okumayi onler)."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _build_vf(mixer_cfg, city_textfile, weather_textfile, font_path):
    """Tek seferlik vf — drawtext metinleri dinamik dosyalardan okunur."""
    width = mixer_cfg.get("width", 1280)
    height = mixer_cfg.get("height", 720)
    fps = mixer_cfg.get("fps", 20)

    parts = [
        "scale={}:{}:force_original_aspect_ratio=decrease".format(width, height),
        "pad={}:{}:(ow-iw)/2:(oh-ih)/2".format(width, height),
        "fps={}".format(fps),
        "setpts=N/{}/TB".format(fps),
        # Şehir adı — textfile reload=1 her frame'de yeniden okur
        "drawtext=fontfile={}:textfile={}:reload=1:"
        "x=30:y=h-90:fontsize=46:fontcolor=white:"
        "box=1:boxcolor=black@0.45:boxborderw=8:"
        "shadowx=2:shadowy=2:shadowcolor=black@0.8".format(
            font_path, city_textfile),
        # Hava durumu (varsa boş, yoksa yazısı gözükmez)
        "drawtext=fontfile={}:textfile={}:reload=1:"
        "x=30:y=h-46:fontsize=30:fontcolor=lightyellow:"
        "box=1:boxcolor=black@0.45:boxborderw=6:"
        "shadowx=1:shadowy=1:shadowcolor=black@0.8".format(
            font_path, weather_textfile),
    ]
    return ",".join(parts)


def _build_ffmpeg_cmd(fifo_path, vf, mixer_cfg, music_playlist, ffmpeg):
    vbr = mixer_cfg.get("video_bitrate_k", 2500)
    abr = mixer_cfg.get("audio_bitrate_k", 128)
    fps = mixer_cfg.get("fps", 20)
    output = mixer_cfg.get("output_rtmp", "rtmp://127.0.0.1:1935/live/stream")
    music_volume = mixer_cfg.get("music_volume", 0.4)

    if music_playlist and Path(music_playlist).exists():
        audio_input = ["-stream_loop", "-1", "-f", "concat", "-safe", "0",
                       "-i", music_playlist]
        audio_args = ["-map", "1:a:0",
                      "-af", "volume=" + str(music_volume),
                      "-c:a", "aac", "-b:a", str(abr) + "k"]
    else:
        audio_input = ["-f", "lavfi",
                       "-i", "anullsrc=channel_layout=stereo:sample_rate=44100"]
        audio_args = ["-map", "1:a:0",
                      "-c:a", "aac", "-b:a", str(abr) + "k"]

    # ÖNEMLI: FIFO input artik RAW H264 (annexb) — mpegts degil.
    # Sebep: farkli sehir mpegts segment'lerin PMT/PCR yapilari farkli,
    # FFmpeg mpegts demuxer sync kaybediyor → fps duser → tee oluyor.
    # Cozum: Python writer her segment'i ffmpeg ile h264 annexb'ye remux eder
    # ve FIFO'ya yazar. Ana FFmpeg sadece raw H264 okur, container drama yok.
    # Audio sadece music input'tan gelir (ingest audio kullanilmiyor zaten).
    return [
        ffmpeg, "-hide_banner",
        "-loglevel", "error",
        "-progress", "pipe:2",
        "-stats_period", "2",
        "-err_detect", "ignore_err",
        "-re",
        "-fflags", "+genpts+discardcorrupt+nobuffer",
        "-thread_queue_size", "1024",
        "-f", "h264",
        "-r", "20",        # giris framerate hint
        "-i", str(fifo_path),
    ] + audio_input + [
        "-vf", vf,
        "-map", "0:v",
    ] + audio_args + [
        "-c:v", "libx264", "-preset", "ultrafast",
        "-b:v", str(vbr) + "k",
        "-maxrate", str(vbr) + "k",
        "-bufsize", str(vbr * 2) + "k",
        "-g", str(fps * 2),
        "-f", "flv", output,
    ]


# ─── Writer thread ────────────────────────────────────────────────────────────

class FifoWriter(threading.Thread):
    """FIFO'ya mpegts segment'leri pump'lar.

    Akış:
      - Şehir sırasıyla sonsuz döngü
      - Her şehir için drawtext metnini güncelle (atomic)
      - O şehrin en yeni segment'lerini sırayla FIFO'ya yaz
      - block_duration saniye dolunca diğer şehre geç
      - Hiç yeni segment yoksa kısa bekle, yine de FIFO açık kalmalı
    """

    def __init__(self, fifo_path: Path, cfg: dict, city_txt: Path,
                 weather_txt: Path, shutdown):
        super().__init__(name="fifo-writer", daemon=True)
        self.fifo_path = fifo_path
        self.cfg = cfg
        self.city_txt = city_txt
        self.weather_txt = weather_txt
        self.shutdown = shutdown
        self.mixer_cfg = cfg.get("mixer", {})
        self.city_order = self.mixer_cfg.get(
            "city_order", ["ankara", "istanbul", "corum", "konya"])
        self.block_duration = self.mixer_cfg.get("city_duration_seconds", 180)
        self._fifo_fp = None
        self._current_city: str = ""

    @property
    def current_city(self) -> str:
        return self._current_city

    def run(self):
        log.info("FIFO writer basliyor (block=%ds)", self.block_duration)
        try:
            self._fifo_fp = open(self.fifo_path, "wb", buffering=0)
            log.info("FIFO acik: %s", self.fifo_path)
        except Exception as e:
            log.error("FIFO acilamadi: %s", e)
            return

        # KESINTISIZ MANTIK:
        # - Yeni segment varsa onlari yaz
        # - Yeni segment yoksa SON segment'i tekrar yaz (FIFO bos kalmasin)
        # - Sehir rotasyonu zaman bazli: her block_duration saniyede degisir
        # - Aktif sehirde 10s segment yoksa -> hemen diger sehre atla (skip)
        rotation_t = time.time()
        city_idx = self._pick_first_ready_city()
        self._switch_city(self.city_order[city_idx])
        seen_ids: set[str] = set()
        last_segment_path: Optional[Path] = None
        starve_count = 0
        no_segment_since: Optional[float] = None
        loop_count = 0

        while not self.shutdown.stopped.is_set():
            try:
                # Zamanli sehir rotasyonu
                if time.time() - rotation_t >= self.block_duration:
                    city_idx = (city_idx + 1) % len(self.city_order)
                    self._switch_city(self.city_order[city_idx])
                    seen_ids.clear()
                    last_segment_path = None
                    no_segment_since = None
                    rotation_t = time.time()

                # Bos sehir kacis: 10s segment yoksa diger sehre atla
                if no_segment_since and (time.time() - no_segment_since > 10):
                    next_idx = self._pick_next_ready_city(city_idx)
                    if next_idx != city_idx:
                        log.warning("[%s] 10s segment yok, %s'e atlaniyor",
                                    self._current_city,
                                    self.city_order[next_idx])
                        db.add_event(
                            "mixer", "city_skip",
                            "{} bos, {}'ya atlandi".format(
                                self._current_city,
                                self.city_order[next_idx]),
                            "warn",
                        )
                        city_idx = next_idx
                        self._switch_city(self.city_order[city_idx])
                        seen_ids.clear()
                        last_segment_path = None
                        no_segment_since = None
                        rotation_t = time.time()

                # En yeni segment'leri ara
                rows = db.latest_segments(self._current_city, limit=20)
                new_rows = []
                for r in reversed(rows):  # eskiden yeniye
                    if r["id"] in seen_ids:
                        continue
                    if not Path(r["path"]).exists():
                        continue
                    new_rows.append(r)

                if new_rows:
                    starve_count = 0
                    no_segment_since = None
                    for r in new_rows:
                        if self.shutdown.stopped.is_set():
                            return
                        seen_ids.add(r["id"])
                        try:
                            self._pump_segment(Path(r["path"]))
                            last_segment_path = Path(r["path"])
                        except FileNotFoundError:
                            pass
                elif last_segment_path and last_segment_path.exists():
                    # FALLBACK: yeni segment yok → son segment'i tekrar yaz
                    starve_count += 1
                    if no_segment_since is None:
                        no_segment_since = time.time()
                    if starve_count == 5:
                        db.add_event(
                            "mixer", "starve",
                            "[%s] yeni segment yok, son segment tekrarlaniyor"
                            % self._current_city,
                            "warn",
                        )
                        log.warning(
                            "[%s] segment akisi durdu, fallback aktif",
                            self._current_city)
                    try:
                        self._pump_segment(last_segment_path)
                    except FileNotFoundError:
                        last_segment_path = None
                        self.shutdown.wait(0.3)
                else:
                    # Henuz hic segment gelmedi
                    if no_segment_since is None:
                        no_segment_since = time.time()
                    if loop_count % 30 == 0:
                        log.info(
                            "[%s] segment bekleniyor (%ds)",
                            self._current_city,
                            int(time.time() - no_segment_since))
                    self.shutdown.wait(0.3)
                loop_count += 1
            except BrokenPipeError:
                log.error("FIFO broken — FFmpeg oldu")
                db.add_event("mixer", "fifo_broken",
                             "FIFO broken pipe — FFmpeg cikti", "error")
                self.shutdown.stopped.set()
                break
            except Exception as e:
                log.warning("writer hata: %s", e)
                self.shutdown.wait(2)

        try:
            self._fifo_fp.close()
        except Exception:
            pass
        log.info("FIFO writer kapaniyor")

    def _pick_first_ready_city(self) -> int:
        """Hangi sehir tamponda en az 5 segment varsa onunla basla."""
        for idx, c in enumerate(self.city_order):
            rows = db.latest_segments(c, limit=5)
            if rows and any(Path(r["path"]).exists() for r in rows):
                return idx
        return 0  # hicbiri yoksa ilki ile baslat, fallback olur

    def _pick_next_ready_city(self, current_idx: int) -> int:
        """Su anki sehirden sonraki, segment'i olan sehri sec.

        Eger hicbir sehirde segment yoksa current_idx'i koru."""
        n = len(self.city_order)
        for offset in range(1, n + 1):
            idx = (current_idx + offset) % n
            c = self.city_order[idx]
            rows = db.latest_segments(c, limit=5)
            if rows and any(Path(r["path"]).exists() for r in rows):
                return idx
        return current_idx

    def _switch_city(self, city: str):
        """Sehir gecisi: drawtext dosyalarini guncelle + DB'ye yaz."""
        display = CITY_DISPLAY.get(city, city.upper())
        weather = get_weather_text(city, self.cfg)
        _atomic_write(self.city_txt, display)
        _atomic_write(self.weather_txt, weather)
        self._current_city = city
        db.set_mixer_block_start(city, self.block_duration)
        db.add_event("mixer", "city_switch",
                     "{} yayinda (weather: {})".format(display, weather),
                     "info")
        log.info("[%s] yayinda — drawtext='%s'", city, display)

    def _pump_segment(self, seg_path: Path):
        """Segment'i raw H264 annexb olarak FIFO'ya yaz.

        mpegts container'in PMT/PCR drama'sini ortadan kaldirir.
        '-c copy -bsf:v h264_mp4toannexb -f h264' = sadece bitstream filter,
        encode YOK. Her segment ~50ms overhead (kabul edilebilir)."""
        if not seg_path.exists():
            raise FileNotFoundError(str(seg_path))
        ffmpeg = common.ffmpeg_path()
        cmd = [
            ffmpeg, "-hide_banner", "-loglevel", "error",
            "-fflags", "+genpts+discardcorrupt",
            "-i", str(seg_path),
            "-an",                                # audio yok
            "-c:v", "copy",
            "-bsf:v", "h264_mp4toannexb",
            "-f", "h264", "pipe:1",
        ]
        proc = subprocess.Popen(
            cmd, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            bufsize=0,
        )
        try:
            while not self.shutdown.stopped.is_set():
                chunk = proc.stdout.read(65536)
                if not chunk:
                    break
                self._fifo_fp.write(chunk)
        finally:
            try:
                proc.stdout.close()
            except Exception:
                pass
            try:
                proc.wait(timeout=2)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass


# ─── FFmpeg supervisor ────────────────────────────────────────────────────────

class FFmpegSupervisor(threading.Thread):
    """FFmpeg'i sureli acik tutar. Progress satirlarini DB'ye yazar.

    Crash olursa otomatik yeniden baslat (10s backoff, max 60s)."""

    def __init__(self, cmd, writer_ref: FifoWriter, shutdown):
        super().__init__(name="ffmpeg-supervisor", daemon=True)
        self.cmd = cmd
        self.writer_ref = writer_ref
        self.shutdown = shutdown
        self.proc: Optional[subprocess.Popen] = None

    def run(self):
        backoff = 5
        while not self.shutdown.stopped.is_set():
            log.info("FFmpeg basliyor (supervisor)...")
            t0 = time.time()
            self.proc = common.popen_proc(
                self.cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
            log.info("FFmpeg PID=%d", self.proc.pid)
            db.add_event("mixer", "ffmpeg_start",
                         "FFmpeg basladi (PID {})".format(self.proc.pid), "info")
            self._read_progress()
            self.proc.wait()
            rc = self.proc.returncode
            elapsed = time.time() - t0
            log.warning("FFmpeg cikti rc=%d sure=%.0fs", rc, elapsed)
            db.add_event("mixer", "ffmpeg_exit",
                         "FFmpeg cikti rc={} ({:.0f}s) → restart".format(rc, elapsed),
                         "error" if rc != 0 else "warn")
            if self.shutdown.stopped.is_set():
                break
            log.info("FFmpeg %ds sonra yeniden baslayacak", backoff)
            self.shutdown.wait(backoff)
            backoff = min(backoff * 2, 60)
            # Başarılı uzun çalışma sonrası backoff'u sıfırla
            if elapsed > 300:
                backoff = 5

    def _read_progress(self):
        """ffmpeg -progress key=value akışı oku, DB'ye yaz."""
        progress_acc = {}
        last_log = time.time()
        stderr_tail = []
        for line in iter(self.proc.stderr.readline, b""):
            try:
                s = line.decode("utf-8", errors="replace").strip()
            except Exception:
                continue
            if not s:
                continue
            if "=" in s and not s.startswith("["):
                k, _, v = s.partition("=")
                progress_acc[k.strip()] = v.strip()
                if k.strip() == "progress":
                    try:
                        speed_s = progress_acc.get("speed", "").rstrip("x")
                        bitrate_s = progress_acc.get("bitrate", "").replace(
                            "kbits/s", "").strip()
                        speed = float(speed_s) if speed_s and speed_s != "N/A" else 0.0
                        fps_v = float(progress_acc.get("fps", "0") or 0)
                        frame = int(progress_acc.get("frame", "0") or 0)
                        bitrate = int(float(bitrate_s)) if bitrate_s and bitrate_s != "N/A" else 0
                        db.update_mixer_progress(
                            speed=speed, fps=fps_v, frame=frame,
                            bitrate_k=bitrate,
                            active_city=self.writer_ref.current_city or None,
                        )
                        if time.time() - last_log > 30:
                            log.info("[%s] speed=%.2fx fps=%.0f frame=%d",
                                     self.writer_ref.current_city, speed,
                                     fps_v, frame)
                            last_log = time.time()
                    except Exception as e:
                        log.debug("progress parse: %s", e)
                    progress_acc.clear()
            else:
                stderr_tail.append(s)
                if len(stderr_tail) > 30:
                    stderr_tail.pop(0)
                if any(kw in s.lower() for kw in
                       ("error", "fail", "invalid", "broken")):
                    log.warning("[ffmpeg] %s", s[:200])

    def stop(self):
        if self.proc and self.proc.poll() is None:
            common.kill_proc_group(self.proc, timeout=5)


# ─── Ana ──────────────────────────────────────────────────────────────────────

def run_mixer(cfg: dict, shutdown):
    work_dir = common.WORK_DIR
    work_dir.mkdir(parents=True, exist_ok=True)
    ffmpeg = common.ffmpeg_path()
    mixer_cfg = cfg.get("mixer", {})

    fifo_path = work_dir / "stream.pipe"
    city_txt = work_dir / "city.txt"
    weather_txt = work_dir / "weather.txt"
    font = cfg.get("font_path",
                   "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")

    # İlk içerik (boş yerine "Yukleniyor")
    if not city_txt.exists():
        city_txt.write_text("YAYIN HAZIR", encoding="utf-8")
    if not weather_txt.exists():
        weather_txt.write_text("", encoding="utf-8")

    # FIFO oluştur
    if fifo_path.exists():
        try:
            fifo_path.unlink()
        except Exception:
            pass
    os.mkfifo(str(fifo_path))
    log.info("FIFO olusturuldu: %s", fifo_path)

    # Heartbeat
    common.start_heartbeat("mixer", interval=10)

    # 1) Writer thread başlat (FIFO'ya yazar, FFmpeg input okuyacak)
    writer = FifoWriter(fifo_path, cfg, city_txt, weather_txt, shutdown)
    writer.start()

    # 2) FFmpeg komutunu oluştur
    music = mixer_cfg.get("music_playlist", "")
    vf = _build_vf(mixer_cfg, city_txt, weather_txt, font)
    cmd = _build_ffmpeg_cmd(fifo_path, vf, mixer_cfg, music, ffmpeg)

    # 3) FFmpeg supervisor
    supervisor = FFmpegSupervisor(cmd, writer, shutdown)
    supervisor.start()

    log.info("Mixer aktif (v4-style FIFO seamless mode)")
    db.add_event("mixer", "startup",
                 "v4-style FIFO mod basladi", "info")

    # Ana thread sadece shutdown bekler
    while not shutdown.stopped.is_set():
        shutdown.wait(1)

    log.info("Mixer kapaniyor...")
    supervisor.stop()
    try:
        fifo_path.unlink()
    except Exception:
        pass


def main():
    cfg = common.load_config()
    shutdown = common.GracefulShutdown()
    try:
        run_mixer(cfg, shutdown)
    except KeyboardInterrupt:
        pass
    log.info("Cikis.")


if __name__ == "__main__":
    main()
