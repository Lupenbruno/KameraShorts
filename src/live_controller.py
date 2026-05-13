"""Canli yayin kontrolcusu — mod yonetimi, ffmpeg, superchat."""
import json
import logging
import os
import random
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

log = logging.getLogger("kamerashorts")

_NW = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

CITY_ORDER  = ["ankara", "istanbul", "corum", "konya"]
CITY_NAMES  = {"ankara": "Ankara", "istanbul": "Istanbul", "corum": "Corum", "konya": "Konya"}
CITY_KEYWORDS = {
    "ankara":   ["ankara"],
    "istanbul": ["istanbul"],
    "corum":    ["corum", "corum"],
    "konya":    ["konya"],
}

# Kamera düşünce anında geçilecek yedek şehir
CITY_FALLBACK = {
    "ankara":   "istanbul",
    "istanbul": "konya",
    "corum":    "istanbul",
    "konya":    "istanbul",
}

SINGLE_DURATION = 180   # 3 dk tek kamera
SPLIT_DURATION  = 600   # 10 dk split ekran
POLL_INTERVAL   = 30    # superchat kontrol
CLIP_DURATION   = 195   # kaydedilen klip suresi (SINGLE + 15s tampon)
RAM_DIR         = "/dev/shm"  # Linux RAM diski


class LiveController:
    def __init__(self, config: dict, rtmp_url: str, chat_id: str, yt_live):
        self.config   = config
        self.rtmp_url = rtmp_url
        self.chat_id  = chat_id
        self.yt_live  = yt_live
        self.ffmpeg   = shutil.which("ffmpeg") or "ffmpeg"
        self.owm_key  = config.get("openweathermap_api_key", "")
        kick_url      = config.get("kick", {}).get("rtmp_url", "")
        # tee muxer: YouTube + Kick aynı anda
        if kick_url:
            self.output = f"[f=flv]{rtmp_url}|[f=flv]{kick_url}"
            self.tee    = True
            log.info("Dual stream: YouTube + Kick")
        else:
            self.output = rtmp_url
            self.tee    = False

        # Kamera listeleri
        cam_file = Path("data/live_cameras.json")
        self.cameras = json.loads(cam_file.read_text(encoding="utf-8")) if cam_file.exists() else {}
        # Her sehir icin kamera indexi (sirayla gec)
        self.cam_idx = {city: 0 for city in CITY_ORDER}

        self._proc: subprocess.Popen | None = None
        self._chat_token: str | None = None
        self._superchat_city: str | None = None
        self._superchat_until: float = 0

        # Canli kamera onbellegi: {city: [cam, cam, ...]}
        import threading
        self._live_cache: dict[str, list] = {c: [] for c in CITY_ORDER}
        self._cache_lock = threading.Lock()
        self._cache_thread = threading.Thread(target=self._refresh_cache_loop, daemon=True)
        self._cache_thread.start()

        # RAM klip tamponu: diger sehir yayindayken klip hazirla
        self._clip_ready: dict[str, str | None] = {c: None for c in CITY_ORDER}
        self._clip_lock = threading.Lock()
        self._clip_thread = threading.Thread(target=self._clip_record_loop, daemon=True)
        self._clip_thread.start()

    def _refresh_cache_loop(self):
        """Arka planda surekli kamera probe eder, onbellegi gunceller."""
        import itertools
        while True:
            for city in CITY_ORDER:
                cams = self.cameras.get(city, [])
                live = []
                # Her sehirden max 3 canli kamera bul
                for cam in cams:
                    if len(live) >= 3:
                        break
                    if self._probe(cam["stream_url"]):
                        live.append(cam)
                with self._cache_lock:
                    self._live_cache[city] = live
                log.debug(f"[onbellek] {city}: {len(live)} canli kamera")
            # 3 dakikada bir yenile
            time.sleep(180)

    def _clip_record_loop(self):
        """Arka planda her sehir icin RAM'e klip kaydeder (195s).
        Klip hazir olunca _clip_ready gunceller.
        Surekli dongu: eski klip oynatilinca yenisini kaydeder.
        """
        import itertools
        for city in itertools.cycle(CITY_ORDER):
            # Zaten gecerli klip varsa atla
            with self._clip_lock:
                existing = self._clip_ready.get(city)
            if existing and Path(existing).exists():
                time.sleep(5)
                continue

            cam = self._find_live_cam(city)
            if not cam:
                log.debug(f"[klip] {city}: canli kamera yok, atlaniyor")
                time.sleep(10)
                continue

            out_path = f"{RAM_DIR}/live_{city}.mp4"
            log.info(f"[klip] {city} — {cam['name']} kaydediliyor → {out_path}")

            weather  = self._get_weather(city)
            now_str  = datetime.now().strftime("%H\\:%M")
            city_disp = CITY_NAMES[city]
            w_str    = f"  {weather['condition']} {weather['temp']}C" if weather else ""
            overlay  = f"CANLI  |  {city_disp} - {cam['name']}  |  {now_str}{w_str}"
            overlay  = overlay.replace("'", "").replace(":", "\\:")
            font_arg = f"fontfile={FONT}:" if Path(FONT).exists() else ""
            vf = (
                f"scale=1280:720:force_original_aspect_ratio=increase,"
                f"crop=1280:720,"
                f"drawtext={font_arg}"
                f"text='{overlay}':"
                f"fontcolor=white:fontsize=28:x=16:y=16:"
                f"box=1:boxcolor=black@0.55:boxborderw=10"
            )

            cmd = [
                self.ffmpeg,
                "-tls_verify", "0",
                "-reconnect", "1", "-reconnect_streamed", "1",
                "-reconnect_delay_max", "3",
                "-i", cam["stream_url"],
                "-t", str(CLIP_DURATION),
                "-vf", vf,
                "-c:v", "libx264", "-preset", "ultrafast",
                "-b:v", "800k", "-maxrate", "900k", "-bufsize", "800k",
                "-g", "48",
                "-c:a", "aac", "-b:a", "64k",
                "-movflags", "+faststart",
                "-y", out_path,
            ]
            try:
                r = subprocess.run(cmd, capture_output=True, timeout=CLIP_DURATION + 30, **_NW)
                if r.returncode == 0 and Path(out_path).exists() and Path(out_path).stat().st_size > 100_000:
                    with self._clip_lock:
                        self._clip_ready[city] = out_path
                    log.info(f"[klip] {city} hazir: {Path(out_path).stat().st_size // 1024}KB")
                else:
                    log.warning(f"[klip] {city} kayit basarisiz (rc={r.returncode})")
            except subprocess.TimeoutExpired:
                log.warning(f"[klip] {city} timeout")
            except Exception as e:
                log.warning(f"[klip] {city} hata: {e}")

    def _next_cached_cam(self, city: str) -> dict | None:
        """Onbellekten aninda kamera ver — probe bekleme yok."""
        with self._cache_lock:
            cams = self._live_cache.get(city, [])
            if not cams:
                return None
            cam = cams[self.cam_idx[city] % len(cams)]
            self.cam_idx[city] = (self.cam_idx[city] + 1) % len(cams)
            return cam

    # ------------------------------------------------------------------
    def run(self):
        log.info("Canli yayin dongusu basliyor...")
        # Onbellek dolana kadar kisa bekleme (max 30s)
        log.info("Kamera onbellegi dolduruluyor (max 30s)...")
        for _ in range(30):
            with self._cache_lock:
                filled = sum(1 for c in CITY_ORDER if self._live_cache[c])
            if filled >= 2:
                break
            time.sleep(1)
        while True:
            try:
                self._run_cycle()
            except Exception as e:
                log.error(f"Canli yayin dongu hatasi: {e}")
                time.sleep(10)

    def _run_cycle(self):
        """1 tam dongu: 4 sehir x 3dk + 10dk split."""
        for city in CITY_ORDER:
            # 1. RAM'de hazir klip var mi? → encode yok, direkt oynat
            with self._clip_lock:
                clip = self._clip_ready.get(city)

            if clip and Path(clip).exists():
                log.info(f"[{city}] RAM klip oynatiliyor: {clip}")
                self._stream_clip(clip, city)
                # Kullanildi, silip yenisini hazirla
                with self._clip_lock:
                    self._clip_ready[city] = None
                try:
                    Path(clip).unlink(missing_ok=True)
                except Exception:
                    pass
            else:
                # 2. Klip yoksa canli stream (fallback)
                cam = self._next_cached_cam(city) or self._find_live_cam(city)
                if cam:
                    self._stream_single(cam, city, SINGLE_DURATION)
                else:
                    log.warning(f"[{city}] Ne klip ne canli kamera, atlaniyor")

        # Split ekran devre disi — cok sayida kamera eş zamanlı gerektirir
        # self._stream_split(SPLIT_DURATION)

    # ------------------------------------------------------------------
    def _stream_single(self, cam: dict, city: str, duration: int):
        """Tek kamera yayini. Superchat override kontrolu yapar."""
        log.info(f"[TEK] {CITY_NAMES[city]} — {cam['name']} ({duration}s)")
        weather = self._get_weather(city)
        self._start_ffmpeg_single(cam["stream_url"], city, cam["name"], weather)

        deadline      = time.time() + duration
        last_poll     = time.time()
        start_time    = time.time()
        dead_streak   = 0  # ard ardina oldu kamera sayisi

        while time.time() < deadline:
            # Superchat kontrol
            if time.time() - last_poll >= POLL_INTERVAL:
                override = self._poll_superchat()
                last_poll = time.time()
                if override and override != city:
                    log.info(f"SuperChat: {override} sehrine geciliyor")
                    override_cam = self._next_cam(override)
                    if override_cam:
                        self._stream_single(override_cam, override, SINGLE_DURATION)
                    return

            # FFmpeg oldu mu?
            if self._proc and self._proc.poll() is not None:
                alive_secs = time.time() - start_time
                fallback = CITY_FALLBACK.get(city)
                log.warning(f"[TEK] {city} dustu ({alive_secs:.0f}s) → {fallback} onbelleginden anlık gecis")
                # Once onbellekten aninda al — probe bekleme yok
                fallback_cam = self._next_cached_cam(fallback) if fallback else None
                # Onbellekte yoksa canli ara (son care)
                if not fallback_cam and fallback:
                    fallback_cam = self._find_live_cam(fallback)
                if fallback_cam:
                    self._start_ffmpeg_single(
                        fallback_cam["stream_url"], fallback,
                        fallback_cam["name"], self._get_weather(fallback)
                    )
                    start_time = time.time()
                else:
                    log.warning(f"[TEK] {fallback} yedegi de yok, sehir atlaniyor")
                    return

            time.sleep(5)

    def _stream_split(self, duration: int):
        """4 sehirden 1'er kamera ile bolunmus ekran yayini."""
        log.info(f"[SPLIT] 4 sehir bolunmus ekran ({duration}s)")
        cams = {city: self._next_cam(city) for city in CITY_ORDER}
        if any(v is None for v in cams.values()):
            log.warning("Split icin yeterli kamera yok, tek kamera moduna geciyor")
            return

        self._start_ffmpeg_split(cams)

        deadline  = time.time() + duration
        last_poll = time.time()

        while time.time() < deadline:
            if time.time() - last_poll >= POLL_INTERVAL:
                override = self._poll_superchat()
                last_poll = time.time()
                if override:
                    log.info(f"SuperChat: {override} sehrine geciliyor (split kesildi)")
                    override_cam = self._next_cam(override)
                    if override_cam:
                        self._stream_single(override_cam, override, SINGLE_DURATION)
                    return

            if self._proc and self._proc.poll() is not None:
                log.warning("[SPLIT] FFmpeg kapandi, yeniden baslatiliyor...")
                self._start_ffmpeg_split(cams)

            time.sleep(5)

    def _stream_clip(self, clip_path: str, city: str):
        """RAM'deki hazir klibi RTMP'ye gonder — encode yok, sadece copy."""
        out_args = ["-f", "tee", self.output] if self.tee else ["-f", "flv", self.output]
        cmd = [
            self.ffmpeg, "-re",
            "-i", clip_path,
            "-c", "copy",
            "-flush_packets", "1",
            *out_args,
        ]
        log.info(f"[{city}] Klip stream baslatiliyor (c:copy)")
        self._switch_to(cmd)

        deadline   = time.time() + SINGLE_DURATION + 10
        last_poll  = time.time()
        while time.time() < deadline:
            if time.time() - last_poll >= POLL_INTERVAL:
                override = self._poll_superchat()
                last_poll = time.time()
                if override and override != city:
                    log.info(f"SuperChat: {override} sehrine geciliyor")
                    self._kill_ffmpeg()
                    override_cam = self._next_cached_cam(override) or self._find_live_cam(override)
                    if override_cam:
                        self._stream_single(override_cam, override, SINGLE_DURATION)
                    return
            if self._proc and self._proc.poll() is not None:
                log.info(f"[{city}] Klip bitti")
                return
            time.sleep(3)

    # ------------------------------------------------------------------
    def _start_ffmpeg_single(self, stream_url: str, city: str, cam_name: str, weather: dict | None):
        now_str   = datetime.now().strftime("%H\\:%M")
        city_disp = CITY_NAMES[city]
        w_str     = f"  {weather['condition']} {weather['temp']}C" if weather else ""
        overlay   = f"CANLI  |  {city_disp} - {cam_name}  |  {now_str}{w_str}"
        overlay   = overlay.replace("'", "").replace(":", "\\:")

        font_arg = f"fontfile={FONT}:" if Path(FONT).exists() else ""

        vf = (
            f"scale=1280:720:force_original_aspect_ratio=increase,"
            f"crop=1280:720,"
            f"drawtext={font_arg}"
            f"text='{overlay}':"
            f"fontcolor=white:fontsize=28:"
            f"x=16:y=16:"
            f"box=1:boxcolor=black@0.55:boxborderw=10"
        )

        out_args = ["-f", "tee", self.output] if self.tee else ["-f", "flv", self.output]
        cmd = [
            self.ffmpeg, "-re",
            "-tls_verify", "0",
            "-reconnect", "1",
            "-reconnect_streamed", "1",
            "-reconnect_delay_max", "5",
            "-timeout", "10000000",
            "-i", stream_url,
            "-vf", vf,
            "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
            "-b:v", "800k", "-maxrate", "900k", "-bufsize", "800k",
            "-g", "48",
            "-c:a", "aac", "-b:a", "64k",
            "-avoid_negative_ts", "make_zero",
            "-flush_packets", "1",
            *out_args,
        ]
        log.info(f"FFmpeg baslatiyor (tek) — {city} {cam_name}")
        self._switch_to(cmd)

    def _start_ffmpeg_split(self, cams: dict):
        inputs = []
        for city in CITY_ORDER:
            cam = cams[city]
            inputs += ["-re", "-tls_verify", "0", "-i", cam["stream_url"]]

        # xstack: 4 kamera 2x2 grid → 1920x1080
        # Her kamera 960x540
        # Split: 4x 640x360 → 1280x720
        filter_complex = (
            "[0:v]scale=640:360[a];"
            "[1:v]scale=640:360[b];"
            "[2:v]scale=640:360[c];"
            "[3:v]scale=640:360[d];"
            "[a][b][c][d]xstack=inputs=4:layout=0_0|640_0|0_360|640_360[v]"
        )

        out_args = ["-f", "tee", self.output] if self.tee else ["-f", "flv", self.output]
        cmd = [
            self.ffmpeg,
            *inputs,
            "-filter_complex", filter_complex,
            "-map", "[v]",
            "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
            "-b:v", "1200k", "-maxrate", "1400k", "-bufsize", "1200k",
            "-g", "48",
            "-an",
            "-flush_packets", "1",
            *out_args,
        ]
        log.info("FFmpeg baslatiyor (split)")
        self._switch_to(cmd)

    def _switch_to(self, cmd: list):
        """Yeni FFmpeg'i once baslat, RTMP baglantisi kurulunca eskiyi kes.
        Bu sayede kameralar arasi geciste yayın kapanmaz.
        """
        new_proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **_NW)
        # Yeni process RTMP'ye baglansin (2s yeterli)
        time.sleep(2)
        # Eski process'i kes
        old_proc = self._proc
        self._proc = new_proc
        if old_proc and old_proc.poll() is None:
            old_proc.terminate()
            try:
                old_proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                old_proc.kill()
        log.info(f"Gecis tamamlandi: yeni PID {new_proc.pid}")

    def _kill_ffmpeg(self):
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self._proc = None

    # ------------------------------------------------------------------
    def _probe(self, url: str) -> bool:
        """2 saniyelik ffprobe — kamera canli mi degil mi."""
        try:
            r = subprocess.run(
                ["ffprobe", "-v", "quiet", "-tls_verify", "0",
                 "-i", url, "-show_entries", "stream=codec_type",
                 "-of", "csv=p=0"],
                capture_output=True, timeout=4
            )
            return r.returncode == 0 and b"video" in r.stdout
        except Exception:
            return False

    def _find_live_cam(self, city: str) -> dict | None:
        """Sehirdeki kameralar arasinda canli olan ilkini bul (max 10 dene)."""
        cams = self.cameras.get(city, [])
        if not cams:
            return None
        total = len(cams)
        for _ in range(min(10, total)):
            cam = self._next_cam(city)
            if self._probe(cam["stream_url"]):
                log.info(f"[{city}] Canli kamera: {cam['name']}")
                return cam
            log.debug(f"[{city}] Offline: {cam['name']}")
        return None

    def _next_cam(self, city: str) -> dict | None:
        cams = self.cameras.get(city, [])
        if not cams:
            return None
        cam = cams[self.cam_idx[city] % len(cams)]
        self.cam_idx[city] = (self.cam_idx[city] + 1) % len(cams)
        return cam

    def _get_weather(self, city: str) -> dict | None:
        try:
            from src.weather import get_weather
            return get_weather(city_key=city, api_key=self.owm_key)
        except Exception:
            return None

    def _poll_superchat(self) -> str | None:
        """Superchat/mesaj ara, sehir keyword bulursa dondur."""
        messages, self._chat_token = self.yt_live.poll_superchat(
            self.chat_id, self._chat_token
        )
        for msg in messages:
            text = msg["text"].lower()
            for city, keywords in CITY_KEYWORDS.items():
                if any(kw in text for kw in keywords):
                    log.info(f"SuperChat/mesaj: '{msg['text']}' → {city} (yazan: {msg['author']})")
                    return city
        return None
