#!/usr/bin/env python3
"""
KameraShorts Live Streamer v4
==============================
Mimari: Pre-build Batch → Named Pipe → FFmpeg → YouTube + Kick

Akış:
  1. BatchBuilder: tüm şehirler PARALEL indirilir (CITY_DURATION saniye/şehir)
  2. CityTranscoder: her şehir ortak formata (H264 mpegts) transcode edilir
  3. Batch: şehir dosyaları sırayla binary concat → batch_N.ts (disk)
  4. PipeWriter: batch dosyalarını named pipe'a sırayla yazar
  5. FFmpeg -re: named pipe'tan okur → setpts=N/fps/TB → YouTube + Kick

Avantajlar vs v3:
  - Kamera hızı ile stream hızı tamamen ayrışır (disk'ten okuma = her zaman 1.0x)
  - Relay timeout → sadece o şehrin içeriği eksik, stream devam eder
  - Filler/PTS hack gereksiz (pre-transcode her şeyi normalize eder)
  - Named pipe = tek FFmpeg süreci, RTMP bağlantısı hiç kesilmez
  - Build süresi (~10 dk) << Stream süresi (~40 dk) → hep önde
"""

import logging
import os
import random
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from queue import Queue, Empty
from typing import Optional

import requests
import urllib3
import yaml

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ─── Logging ────────────────────────────────────────────────────────────────

def _setup_logging(log_path: Optional[str]):
    fmt = logging.Formatter(
        "%(asctime)s [%(name)-16s] %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    # urllib3 / requests gürültüsünü kapat
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)
    logging.getLogger("charset_normalizer").setLevel(logging.WARNING)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root.addHandler(sh)
    if log_path:
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_path, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)


log = logging.getLogger("live")
_NW = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}

# ─── Subprocess Tracker ─────────────────────────────────────────────────────
# Tüm spawn edilen ffmpeg'ler kendi process group'unda (start_new_session=True)
# çalışır. Bu tracker, kapanmada `os.killpg` ile süreç grubunu temiz öldürmek
# için onları takip eder — systemd'nin 90s timeout + SIGKILL bombardımanı önlenir.
_proc_tracker: "set[subprocess.Popen]" = set()
_proc_tracker_lock = threading.Lock()


def _track_proc(p: subprocess.Popen) -> subprocess.Popen:
    with _proc_tracker_lock:
        _proc_tracker.add(p)
    return p


def _untrack_proc(p: subprocess.Popen):
    with _proc_tracker_lock:
        _proc_tracker.discard(p)


def _kill_tracked(sig: int = signal.SIGTERM):
    """Tüm takipli proseslere süreç-grup sinyali gönderir."""
    with _proc_tracker_lock:
        procs = [p for p in _proc_tracker if p.poll() is None]
    for p in procs:
        try:
            os.killpg(os.getpgid(p.pid), sig)
        except (ProcessLookupError, PermissionError):
            pass


def _kill_proc_group(p: subprocess.Popen, timeout: float = 5.0) -> bool:
    """Tek bir process group'a SIGTERM gönder, timeout sonra SIGKILL. Döner: temiz öldü mü."""
    if p.poll() is not None:
        return True
    try:
        os.killpg(os.getpgid(p.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return True
    try:
        p.wait(timeout=timeout)
        return True
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(p.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            p.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        return False


# ─── Sabitler ───────────────────────────────────────────────────────────────

ISTANBUL_BASE = "https://livestream.ibb.gov.tr/cam_turistik/{slug}.stream/playlist.m3u8"
ISTANBUL_CAMERAS = [
    # Doğrulanmış çalışan kameralar (IBB turistik kamera listesi)
    {"name": "Sultanahmet 1",  "slug": "b_sultanahmet"},
    {"name": "Sultanahmet 2",  "slug": "b_sultanahmet2"},
    {"name": "Salacak",        "slug": "b_salacak"},
    {"name": "Kapalı Çarşı",  "slug": "b_kapalicarsi"},
    {"name": "Kadıköy",        "slug": "b_kadikoy"},
    {"name": "Taksim Meydanı", "slug": "b_taksim_meydan"},
    {"name": "Üsküdar",        "slug": "b_uskudar"},
    {"name": "Kız Kulesi",     "slug": "new_Kızkulesi"},
    {"name": "Eyüp Sultan",    "slug": "b_eyupsultan"},
    {"name": "Anadolu Hisarı", "slug": "b_anadoluhisari"},
    {"name": "Dragos",         "slug": "b_dragos"},
    {"name": "Hidiv Kasrı",    "slug": "b_hidivkasri"},
    {"name": "Küçükçekmece",   "slug": "b_kucukcekmece"},
    {"name": "Metrohan",       "slug": "b_metrohan"},
    {"name": "Mısır Çarşısı",  "slug": "b_misircarsisi"},
    {"name": "Saraçhane",      "slug": "b_sarachane"},
    {"name": "Ulus Parkı",     "slug": "b_ulusparki"},
    {"name": "Pierre Lotti",   "slug": "b_pierreloti"},
    {"name": "Beyazıt Kulesi 1", "slug": "b_beyazitkule"},
    {"name": "Beyazıt Kulesi 2", "slug": "b_beyazitkule2new"},
    {"name": "Beyazıt Meydanı",  "slug": "b_beyazitmeydani"},
    {"name": "Büyük Çamlıca",    "slug": "b_buyukcamlıca"},
    {"name": "Miniatürk",        "slug": "b_miniatürk"},
]

ANKARA_STATUS_URL = "https://seyret.ankara.bel.tr/status.json"
RELAY_START_URL   = "https://seyret.ankara.bel.tr/api/relay/start/{dvr}?provider={provider}"

# ─── Overlay / Hava Durumu ───────────────────────────────────────────────────

FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

# config city key → ekranda gösterilecek şehir adı
CITY_DISPLAY_MAP: dict[str, str] = {
    "Ankara":   "ANKARA",
    "Istanbul": "İSTANBUL",
    "Corum":    "ÇORUM",
    "Konya":    "KONYA",
}

# config city key → OpenWeatherMap sorgu stringi
CITY_OWM_MAP: dict[str, str] = {
    "Ankara":   "Ankara,TR",
    "Istanbul": "Istanbul,TR",
    "Corum":    "Corum,TR",
    "Konya":    "Konya,TR",
}

_weather_cache: dict[str, tuple[float, str]] = {}  # key → (timestamp, text)
_WEATHER_TTL = 1800  # 30 dakika


def _esc_drawtext(text: str) -> str:
    """FFmpeg drawtext text= değeri için özel karakterleri escape eder."""
    return (
        text
        .replace("\\", "\\\\")
        .replace("'",  "\\'")
        .replace(":",  "\\:")
    )


def fetch_weather(city_key: str, api_key: str) -> str:
    """
    OpenWeatherMap'ten hava durumu çeker.
    Sonucu 30 dakika önbelleğe alır.
    Hata veya key yoksa "" döner.
    """
    if not api_key:
        return ""

    now = time.time()
    cached = _weather_cache.get(city_key)
    if cached and now - cached[0] < _WEATHER_TTL:
        return cached[1]

    owm_city = CITY_OWM_MAP.get(city_key, city_key)
    try:
        url = (
            "https://api.openweathermap.org/data/2.5/weather"
            f"?q={owm_city}&appid={api_key}&units=metric&lang=tr"
        )
        r = requests.get(url, timeout=8)
        if r.status_code == 200:
            data = r.json()
            temp = round(data["main"]["temp"])
            desc = data["weather"][0]["description"].capitalize()
            text = f"{desc}  {temp}°C"
            log.info(f"[weather:{city_key}] {text}")
        else:
            log.warning(f"[weather:{city_key}] OWM HTTP {r.status_code}")
            text = ""
    except Exception as e:
        log.warning(f"[weather:{city_key}] Hata: {e}")
        text = ""

    _weather_cache[city_key] = (now, text)
    return text

# ─── Config ─────────────────────────────────────────────────────────────────

def load_config(path: str = "config.yaml") -> dict:
    with open(path, encoding="utf-8") as f:
        full = yaml.safe_load(f)
    ls = full.get("live_stream")
    if not ls:
        raise ValueError("config.yaml'da 'live_stream:' bölümü bulunamadı")
    if not ls.get("kick_rtmp_url") and full.get("kick", {}).get("rtmp_url"):
        ls["kick_rtmp_url"] = full["kick"]["rtmp_url"]
    # OWM API key (live_stream altında yoksa üst düzeyde ara)
    if not ls.get("owm_api_key"):
        ls["owm_api_key"] = full.get("openweathermap_api_key", "")
    return ls

# ─── HTTP Session ────────────────────────────────────────────────────────────

def _make_session(headers: dict = None, ssl_verify: bool = True) -> requests.Session:
    s = requests.Session()
    # Connection pool: HLS playlist + segment indirme tek kaynağa onlarca istek
    # gönderir. Default pool_maxsize=10 → her segment yeni TCP. 50 ile yeniden
    # kullanım maksimuma çıkar.
    adapter = requests.adapters.HTTPAdapter(
        pool_connections=20, pool_maxsize=50, pool_block=False,
    )
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    s.headers["User-Agent"] = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
    if headers:
        s.headers.update(headers)
    s.verify = ssl_verify
    return s

# ─── Kamera Çözümleme ───────────────────────────────────────────────────────

def resolve_camera(cam_cfg: dict) -> Optional[dict]:
    ctype = cam_cfg.get("type", "direct")
    hdrs  = cam_cfg.get("headers", {})
    ssl   = cam_cfg.get("ssl_verify", True)

    if ctype == "direct":
        return {
            "name":       cam_cfg.get("city", "Kamera"),
            "stream_url": cam_cfg["stream_url"],
            "session":    _make_session(hdrs, ssl),
        }

    if ctype == "direct_random":
        streams = cam_cfg.get("streams", [])
        if not streams:
            return None
        return {
            "name":       cam_cfg.get("city", "Kamera"),
            "stream_url": random.choice(streams),
            "session":    _make_session(hdrs, ssl),
        }

    if ctype == "istanbul_api":
        cam = random.choice(ISTANBUL_CAMERAS)
        return {
            "name":       f"Istanbul - {cam['name']}",
            "stream_url": ISTANBUL_BASE.format(slug=cam["slug"]),
            "session":    _make_session(hdrs, ssl_verify=False),
        }

    if ctype == "ankara_api":
        try:
            s = _make_session(hdrs, ssl_verify=False)
            data = s.get(ANKARA_STATUS_URL, timeout=15).json()
            BUS_TYPES = {"Solo", "ELK", "Koruklu", "Koruklu ELK", "Minibus", "Midibus"}
            active = [
                v for v in data
                if v.get("stream_url") and v.get("dvr_serial_number")
                and v.get("is_visible") and v.get("vehicle_type") in BUS_TYPES
            ]
            if not active:
                active = [
                    v for v in data
                    if v.get("stream_url") and v.get("dvr_serial_number") and v.get("is_visible")
                ]
            if not active:
                log.warning("[resolve] Ankara'da aktif kamera bulunamadı")
                return None
            v = random.choice(active)
            plate = v.get("license_plate", v.get("dvr_serial_number", "?"))
            return {
                "name":       f"Ankara - {plate}",
                "stream_url": v["stream_url"],
                "session":    s,
                "dvr":        v["dvr_serial_number"],
                "provider":   v.get("source", "ego"),
            }
        except Exception as e:
            log.error(f"[resolve] Ankara API hatası: {e}")
            return None

    log.error(f"[resolve] Bilinmeyen kamera tipi: {ctype}")
    return None


def _follow_master_playlist(url: str, session: requests.Session) -> str:
    """2 katmanlı HLS (IBB gibi): master → chunklist."""
    try:
        r = session.get(url, timeout=8)
        if r.status_code != 200 or "#EXT-X-STREAM-INF" not in r.text:
            return url
        lines = r.text.strip().split("\n")
        for i, line in enumerate(lines):
            if line.startswith("#EXT-X-STREAM-INF") and i + 1 < len(lines):
                sub = lines[i + 1].strip()
                if sub and not sub.startswith("#"):
                    return sub if sub.startswith("http") else url.rsplit("/", 1)[0] + "/" + sub
    except Exception:
        pass
    return url

# ─── Relay Yöneticisi ───────────────────────────────────────────────────────

class RelayManager:
    """Ankara EGO relay başlatır ve TTL boyunca yeniler."""
    _RENEW_AT = 33  # saniye (TTL=40)

    def __init__(self, cam: dict):
        self._session  = cam["session"]
        self._dvr      = cam["dvr"]
        self._provider = cam.get("provider", "ego")
        self._stream   = cam["stream_url"]
        self._stop     = threading.Event()

    def start_and_wait(self, timeout: int = 40) -> bool:
        if self._check_live():
            log.info(f"[relay] {self._dvr} zaten canlı")
            threading.Thread(target=self._renew_loop, daemon=True).start()
            return True
        try:
            url = RELAY_START_URL.format(dvr=self._dvr, provider=self._provider)
            self._session.post(url, timeout=10)
        except Exception as e:
            log.warning(f"[relay] POST hatası: {e}")

        deadline = time.time() + timeout
        waited   = 0
        while time.time() < deadline:
            if self._check_live():
                log.info(f"[relay] {self._dvr} hazır ({waited}s)")
                threading.Thread(target=self._renew_loop, daemon=True).start()
                return True
            time.sleep(3)
            waited += 3

        log.warning(f"[relay] {self._dvr} {timeout}s içinde yanıt vermedi")
        return False

    def stop(self):
        self._stop.set()

    def _check_live(self) -> bool:
        try:
            r = self._session.get(self._stream, timeout=5)
            return r.status_code == 200 and "#EXTM3U" in r.text
        except Exception:
            return False

    def _renew_loop(self):
        while not self._stop.wait(self._RENEW_AT):
            try:
                url = RELAY_START_URL.format(dvr=self._dvr, provider=self._provider)
                self._session.post(url, timeout=10)
                log.debug(f"[relay] {self._dvr} yenilendi")
            except Exception as e:
                log.warning(f"[relay] Yenileme hatası: {e}")

# ─── Şehir Segment Toplayıcı ─────────────────────────────────────────────────

class CityCollector:
    """
    Bir şehrin HLS kamerasından target_duration saniyelik video içeriği indirir.
    Paralel çalıştırılmak üzere tasarlanmıştır — thread-safe.

    Kamera başarısızsa aynı şehirden yeni kamera seçerek yeniden dener.
    Toplam zaman bütçesi: target × 1.5 (birden fazla deneme arasında paylaşılır).

    Döner: (city_key, city_name, segments)
    """

    MIN_DURATION    = 30.0   # Bu kadar bile toplanamadıysa başarısız say
    MAX_ATTEMPTS    = 3      # Bir şehir için maksimum kamera denemesi
    SEG_FAIL_LIMIT  = 10     # Art arda bu kadar segment hatası → kamerayı bırak

    def __init__(
        self,
        cam_cfg: dict,
        target_duration: float,
        work_dir: Path,
        stop_event: threading.Event,
    ):
        self._cfg    = cam_cfg
        self._target = target_duration
        self._dir    = work_dir
        self._stop   = stop_event

    def collect(self) -> tuple[str, str, list[Path]]:
        """Döner: (city_key, city_name, segments)"""
        city_id  = self._cfg.get("city", self._cfg.get("type", "?"))
        deadline = time.time() + self._target * 1.5   # Toplam bütçe (tüm denemeler için)

        for attempt in range(self.MAX_ATTEMPTS):
            if self._stop.is_set():
                break

            remaining = deadline - time.time()
            if remaining < self.MIN_DURATION:
                log.warning(f"[collector:{city_id}] Yeterli süre kalmadı ({remaining:.0f}s)")
                break

            if attempt > 0:
                log.info(f"[collector:{city_id}] Yeni kamera deneniyor ({attempt+1}/{self.MAX_ATTEMPTS}), kalan {remaining:.0f}s")

            relay: Optional[RelayManager] = None
            cam = resolve_camera(self._cfg)
            if cam is None:
                log.warning(f"[collector:{city_id}] resolve başarısız")
                continue

            city_name = cam["name"]

            # Ankara relay başlat
            if "dvr" in cam:
                relay = RelayManager(cam)
                if not relay.start_and_wait(timeout=min(40, int(remaining - 10))):
                    log.warning(f"[collector:{city_id}] Relay başlatılamadı")
                    continue

            segments, total_dur = self._try_one(cam, deadline)

            if relay:
                relay.stop()

            if total_dur >= self.MIN_DURATION:
                log.info(f"[collector:{city_name}] Bitti: {len(segments)} seg, {total_dur:.0f}/{self._target:.0f}s")
                return city_id, city_name, segments

            # Bu kamera yetersiz — segmentleri temizle, başka dene
            log.warning(
                f"[collector:{city_name}] Yetersiz ({total_dur:.0f}s < {self.MIN_DURATION}s)"
                + (f", {self.MAX_ATTEMPTS - attempt - 1} deneme kaldı" if attempt + 1 < self.MAX_ATTEMPTS else "")
            )
            for p in segments:
                try: p.unlink()
                except Exception: pass

        log.warning(f"[collector:{city_id}] Tüm denemeler tükendi, şehir atlanıyor")
        return city_id, city_id, []

    def _try_one(self, cam: dict, deadline: float) -> tuple[list[Path], float]:
        """
        Tek bir kameradan mümkün olduğunca segment toplar.
        deadline'a veya hedef süreye ulaşınca durur.
        Art arda SEG_FAIL_LIMIT segment hatası olursa erken çıkar.

        Döner: (segments, total_dur)
        """
        city_name   = cam["name"]
        stream_url  = _follow_master_playlist(cam["stream_url"], cam["session"])
        base        = stream_url.rsplit("/", 1)[0] + "/"
        sess        = cam["session"]

        segments:    list[Path] = []
        total_dur    = 0.0
        seen:        set[str]   = set()
        pl_fail      = 0   # Playlist 404/403 streak
        seg_fail     = 0   # Segment indirme hatası streak
        last_prog_t  = time.time()

        log.info(f"[collector:{city_name}] Başladı → hedef {self._target:.0f}s, son {deadline - time.time():.0f}s")

        while not self._stop.is_set() and total_dur < self._target:
            if time.time() >= deadline:
                log.warning(f"[collector:{city_name}] Süre bütçesi bitti ({total_dur:.0f}s toplandı)")
                break

            try:
                r = sess.get(stream_url, timeout=8)

                if r.status_code in (404, 403, 410):
                    pl_fail += 1
                    if pl_fail >= 3:
                        new_url = _follow_master_playlist(cam["stream_url"], sess)
                        if new_url != stream_url:
                            log.info(f"[collector:{city_name}] URL güncellendi → {new_url[-50:]}")
                            stream_url = new_url
                            base       = stream_url.rsplit("/", 1)[0] + "/"
                        else:
                            log.warning(f"[collector:{city_name}] Chunklist 404 streak={pl_fail}, master aynı URL")
                        pl_fail = 0
                    self._stop.wait(2)
                    continue

                if r.status_code != 200:
                    self._stop.wait(1)
                    continue

                pl_fail = 0

                # HLS parse (EXT-X-PROGRAM-DATE-TIME gibi ara etiketleri atla)
                entries: list[tuple[str, float]] = []
                lines = r.text.strip().split("\n")
                i = 0
                while i < len(lines):
                    line = lines[i].strip()
                    if line.startswith("#EXTINF:"):
                        try:
                            dur = float(line.split(":")[1].rstrip(","))
                        except Exception:
                            dur = 2.0
                        j = i + 1
                        while j < len(lines):
                            sl = lines[j].strip()
                            if not sl or sl.startswith("#"):
                                j += 1
                                continue
                            url = sl if sl.startswith("http") else base + sl
                            entries.append((url, dur))
                            i = j
                            break
                    i += 1

                if not entries:
                    self._stop.wait(1)
                    continue

                new_entries = [(u, d) for u, d in entries if u not in seen]

                for url, dur in new_entries:
                    if self._stop.is_set() or total_dur >= self._target or time.time() >= deadline:
                        break
                    seen.add(url)
                    try:
                        sr = sess.get(url, timeout=12, stream=True)
                        if sr.status_code != 200:
                            log.debug(f"[collector:{city_name}] Segment HTTP {sr.status_code}: {url[-50:]}") if sr.status_code == 403 else log.warning(f"[collector:{city_name}] Segment HTTP {sr.status_code}: {url[-50:]}")
                            seg_fail += 1
                            if seg_fail >= self.SEG_FAIL_LIMIT:
                                log.warning(f"[collector:{city_name}] {self.SEG_FAIL_LIMIT} ardışık segment hatası → kamera değiştirilecek")
                                return segments, total_dur
                            continue
                        seg_fail = 0
                        fd, tmp = tempfile.mkstemp(suffix=".ts", dir=str(self._dir))
                        with os.fdopen(fd, "wb") as f:
                            for chunk in sr.iter_content(65536):
                                f.write(chunk)
                        segments.append(Path(tmp))
                        total_dur += dur
                    except Exception as e:
                        log.warning(f"[collector:{city_name}] Segment hatası: {e}")
                        seg_fail += 1
                        if seg_fail >= self.SEG_FAIL_LIMIT:
                            log.warning(f"[collector:{city_name}] {self.SEG_FAIL_LIMIT} ardışık segment hatası → kamera değiştirilecek")
                            return segments, total_dur

                if total_dur < self._target and not new_entries:
                    self._stop.wait(1.5)

                now = time.time()
                if now - last_prog_t >= 30:
                    pct = min(total_dur / self._target * 100, 100)
                    log.info(f"[collector:{city_name}] İlerleme: {total_dur:.0f}/{self._target:.0f}s ({pct:.0f}%)")
                    last_prog_t = now

            except Exception as e:
                log.warning(f"[collector:{city_name}] Playlist hatası: {e}")
                self._stop.wait(2)

        return segments, total_dur

# ─── Şehir Transcode ─────────────────────────────────────────────────────────

def transcode_city(
    city_name:    str,
    segments:     list[Path],
    output:       Path,
    ffmpeg:       str,
    w: int, h: int, fps: int, vbr: int,
    display_name: str = "",
    weather_text: str = "",
) -> bool:
    """
    Ham segmentleri ortak H264/mpegts formatına transcode eder.
    setpts=N/fps/TB → monotonic timestamp, her kamera farklı PTS olsa bile sorunsuz.
    Ses YOK — streaming FFmpeg sessiz audio ekler.
    display_name + weather_text verilirse drawtext overlay eklenir.
    """
    list_file = output.with_suffix(".txt")
    try:
        with open(list_file, "w", encoding="utf-8") as f:
            for seg in segments:
                f.write(f"file '{seg}'\n")

        # Overlay: şehir adı (sol alt) + hava durumu (hemen altında)
        overlay_filters = ""
        if display_name or weather_text:
            parts = []
            if display_name:
                esc = _esc_drawtext(display_name)
                parts.append(
                    f"drawtext=fontfile={FONT_PATH}:text='{esc}':"
                    f"x=30:y=h-90:fontsize=46:fontcolor=white:"
                    f"box=1:boxcolor=black@0.45:boxborderw=8:"
                    f"shadowx=2:shadowy=2:shadowcolor=black@0.8"
                )
            if weather_text:
                esc_w = _esc_drawtext(weather_text)
                parts.append(
                    f"drawtext=fontfile={FONT_PATH}:text='{esc_w}':"
                    f"x=30:y=h-46:fontsize=30:fontcolor=lightyellow:"
                    f"box=1:boxcolor=black@0.45:boxborderw=6:"
                    f"shadowx=1:shadowy=1:shadowcolor=black@0.8"
                )
            overlay_filters = "," + ",".join(parts)

        fc = (
            f"[0:v]fps={fps},"
            f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,"
            f"setpts=N/{fps}/TB"
            f"{overlay_filters}[vout]"
        )

        cmd = [
            # nice -n 18: Python süreci os.nice(-10) → child nice -10+18=+8
            # → streaming(-15) > Python(-10) > normal(0) > transcode(+8)
            # Streamer'a CPU önceliği bırakmak için +5 yerine +8.
            "nice", "-n", "18",
            ffmpeg, "-y", "-hide_banner", "-loglevel", "warning",
            "-f", "concat", "-safe", "0", "-i", str(list_file),
            "-filter_complex", fc,
            "-map", "[vout]",
            "-an",                                   # ses yok
            "-c:v", "libx264", "-preset", "ultrafast",
            "-threads", "2",                         # 2 core max → stream'e 1 core garanti
            "-b:v", f"{vbr}k", "-maxrate", f"{vbr}k", "-bufsize", f"{vbr*2}k",
            "-g", str(fps * 2),
            "-f", "mpegts",
            "-muxrate", str(vbr * 1000),   # CBR mux → doğru dosya boyutu → rate limit çalışır
            str(output),
        ]

        log.info(f"[transcode:{city_name}] Başladı ({len(segments)} seg → {output.name})")
        t0 = time.time()
        # Popen + tracker → kapanmada killpg ile temiz öldürme.
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True, **_NW,
        )
        _track_proc(proc)
        try:
            _, stderr_b = proc.communicate(timeout=600)
        finally:
            _untrack_proc(proc)
        rc = proc.returncode
        dt = time.time() - t0

        if rc != 0:
            err = (stderr_b[-800:].decode("utf-8", errors="replace")
                   if stderr_b else "")
            log.error(f"[transcode:{city_name}] HATA (rc={rc}):\n{err}")
            return False

        size_mb = output.stat().st_size / 1e6
        log.info(f"[transcode:{city_name}] OK — {dt:.0f}s, {size_mb:.1f}MB")
        return True

    except subprocess.TimeoutExpired:
        # Process group öldür — alt thread'leri de temizler.
        try:
            _kill_proc_group(proc, timeout=2)  # type: ignore[has-type]
            _untrack_proc(proc)                # type: ignore[has-type]
        except Exception:
            pass
        log.error(f"[transcode:{city_name}] Zaman aşımı (600s)")
        return False
    except Exception as e:
        log.error(f"[transcode:{city_name}] İstisna: {e}")
        return False
    finally:
        try: list_file.unlink()
        except Exception: pass

# ─── Quad-Split Composite ────────────────────────────────────────────────────

def generate_quad(
    batch_dir: Path,
    city_keys_ordered: list[str],     # ["Istanbul", "Konya", "Corum", "Ankara"]
    city_ts_paths: list[Path],         # aynı sırada ts dosyaları
    ffmpeg: str,
    fps: int, vbr: int,
    owm_key: str,
    target_seconds: int = 240,
) -> Optional[Path]:
    """
    4 şehir .ts dosyasını 2×2 quad-split (1280×720) composite eder.

    Her hücre: 640×360, sol-üst'te ŞEHİR ADI + güncel hava durumu overlay.
    Yerleşim:
      ┌──────────┬──────────┐
      │ city[0]  │ city[1]  │
      ├──────────┼──────────┤
      │ city[2]  │ city[3]  │
      └──────────┴──────────┘

    Süre: target_seconds (default 240s). En kısa input bitince durur.
    """
    if len(city_keys_ordered) != 4 or len(city_ts_paths) != 4:
        log.info("[quad] 4 şehir yok, quad atlanıyor")
        return None

    # 4 hücre için filter chain (her biri scale + overlay)
    chains = []
    for i, city_key in enumerate(city_keys_ordered):
        display = CITY_DISPLAY_MAP.get(city_key, city_key.upper())
        weather_text = fetch_weather(city_key, owm_key)
        esc_d = _esc_drawtext(display)
        chain = (
            f"[{i}:v]scale=640:360,setsar=1,format=yuv420p,"
            f"drawtext=fontfile={FONT_PATH}:text='{esc_d}':"
            f"x=15:y=12:fontsize=30:fontcolor=white:"
            f"box=1:boxcolor=black@0.6:boxborderw=6:"
            f"shadowx=2:shadowy=2:shadowcolor=black@0.8"
        )
        if weather_text:
            esc_w = _esc_drawtext(weather_text)
            chain += (
                f",drawtext=fontfile={FONT_PATH}:text='{esc_w}':"
                f"x=15:y=52:fontsize=22:fontcolor=lightyellow:"
                f"box=1:boxcolor=black@0.55:boxborderw=5:"
                f"shadowx=1:shadowy=1:shadowcolor=black@0.7"
            )
        chain += f"[v{i}]"
        chains.append(chain)

    # xstack: 2×2 grid layout
    fc = (
        ";".join(chains)
        + ";[v0][v1][v2][v3]xstack=inputs=4:layout=0_0|w0_0|0_h0|w0_h0[out]"
    )

    quad_path = batch_dir / "QUAD.ts"
    cmd = [
        "nice", "-n", "18",
        ffmpeg, "-y", "-hide_banner", "-loglevel", "warning",
        "-i", str(city_ts_paths[0]),
        "-i", str(city_ts_paths[1]),
        "-i", str(city_ts_paths[2]),
        "-i", str(city_ts_paths[3]),
        "-filter_complex", fc,
        "-map", "[out]", "-an",
        "-t", str(target_seconds),
        "-c:v", "libx264", "-preset", "ultrafast",
        "-threads", "2",                          # 2 core max — stream'i etkilemesin
        "-b:v", f"{vbr}k", "-maxrate", f"{vbr}k", "-bufsize", f"{vbr*2}k",
        "-g", str(fps * 2),
        "-f", "mpegts",
        "-muxrate", str(vbr * 1000),
        str(quad_path),
    ]

    log.info(
        f"[quad] Composite başladı — 4 şehir (640×360) → 1280×720 ({target_seconds}s)"
    )
    t0 = time.time()
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=True, **_NW,
    )
    _track_proc(proc)
    try:
        _, stderr_b = proc.communicate(timeout=300)
    except subprocess.TimeoutExpired:
        try:
            _kill_proc_group(proc, timeout=2)
            _untrack_proc(proc)
        except Exception:
            pass
        log.error("[quad] Zaman aşımı (300s)")
        return None
    finally:
        _untrack_proc(proc)

    if proc.returncode != 0:
        err = (stderr_b[-500:].decode("utf-8", errors="replace")
               if stderr_b else "")
        log.error(f"[quad] HATA (rc={proc.returncode}):\n{err}")
        return None
    if not quad_path.exists() or quad_path.stat().st_size < 1_000_000:
        log.error("[quad] Çıktı dosyası boş veya çok küçük")
        return None

    dt = time.time() - t0
    size_mb = quad_path.stat().st_size / 1e6
    log.info(f"[quad] ✓ OK — {dt:.0f}s, {size_mb:.1f}MB")
    return quad_path


# ─── Batch Builder ───────────────────────────────────────────────────────────

class BatchBuilder:
    """
    Tüm şehirleri paralel indirir → transcode → binary concat → batch_N.ts.

    Binary concat neden çalışır:
      - Tüm şehirler aynı codec/çözünürlük/fps'e transcode edildi
      - MPEG-TS discontinuity'ler streaming FFmpeg'deki setpts=N/fps/TB tarafından
        tamamen görmezden gelinir (frame counter bazlı timestamp)
    """

    def __init__(self, cfg: dict, work_dir: Path, stop_event: threading.Event):
        self._cfg      = cfg
        self._work_dir = work_dir
        self._stop     = stop_event
        self._ffmpeg   = cfg.get("ffmpeg", "/usr/bin/ffmpeg")
        self._w        = cfg.get("width", 1080)
        self._h        = cfg.get("height", 1920)
        self._fps      = cfg.get("fps", 25)
        self._vbr      = cfg.get("video_bitrate", cfg.get("bitrate", 2500))
        self._dur      = cfg.get("city_duration", 600)   # saniye/şehir
        self._cameras  = cfg.get("cameras", [])

    def build(self, batch_id: int) -> Optional[Path]:
        """
        Bir batch oluşturur. Stop event set edilirse None döner.
        Blocking — tüm şehirler paralel indirilir, sonra transcode edilir.
        """
        batch_dir = self._work_dir / f"b{batch_id:04d}"
        batch_dir.mkdir(parents=True, exist_ok=True)
        t0 = time.time()

        n = len(self._cameras)
        log.info(
            f"[builder] ═══ Batch {batch_id} başlıyor "
            f"({n} şehir × {self._dur}s ≈ toplam {n * self._dur / 60:.0f} dk) ═══"
        )

        # ── Paralel indirme ──────────────────────────────────────────────────
        # Her şehir için bağımsız stop event (global stop gelince hepsini durdur)
        city_stops: list[threading.Event] = []

        def _make_stop_watcher(ev: threading.Event) -> threading.Event:
            def _watch():
                while not self._stop.is_set():
                    time.sleep(0.3)
                ev.set()
            threading.Thread(target=_watch, daemon=True).start()
            return ev

        def collect_one(cam_cfg: dict) -> tuple[str, str, list[Path]]:
            ev = threading.Event()
            city_stops.append(ev)
            _make_stop_watcher(ev)
            collector = CityCollector(cam_cfg, self._dur, batch_dir, ev)
            return collector.collect()

        city_results: list[tuple[str, str, list[Path]]] = []
        with ThreadPoolExecutor(max_workers=max(n, 1)) as pool:
            futures = {pool.submit(collect_one, cam): cam for cam in self._cameras}
            for future in as_completed(futures):
                if self._stop.is_set():
                    # Tüm city stop'ları aktive et
                    for ev in city_stops:
                        ev.set()
                    return None
                try:
                    city_results.append(future.result())
                except Exception as e:
                    cam = futures[future]
                    log.error(f"[builder] {cam.get('city', '?')} thread hatası: {e}")

        if self._stop.is_set():
            return None

        dt_dl = time.time() - t0
        log.info(f"[builder] Batch {batch_id} indirme tamamlandı ({dt_dl:.0f}s)")

        # ── Transcode (paralel) ──────────────────────────────────────────────
        # Her şehir bağımsız — ThreadPoolExecutor ile paralel çalıştır.
        # Sıralı transcode: Istanbul 182s + Çorum 180s + Konya 140s + Ankara 82s = 584s.
        # Paralel transcode: max(182, 180, 140, 82) = 182s → 3x daha hızlı.
        # Çıktı sırası kamera sırasına göre korunur (pool.map değil ordered list).

        if self._stop.is_set():
            return None

        owm_key = self._cfg.get("owm_api_key", "")

        # Transcode işleri: sadece segment'i olan şehirler
        transcode_jobs: list[tuple[int, str, str, list[Path], Path, str, str]] = []
        for idx, (city_key, city_name, segments) in enumerate(city_results):
            if not segments:
                log.warning(f"[builder] {city_name} atlandı (segment yok)")
                continue
            safe_name    = re.sub(r"[^\w]", "_", city_name)
            out_ts       = batch_dir / f"{safe_name}.ts"
            display_name = CITY_DISPLAY_MAP.get(city_key, city_key.upper())
            weather      = fetch_weather(city_key, owm_key)
            transcode_jobs.append((idx, city_key, city_name, segments, out_ts, display_name, weather))

        ffmpeg = self._ffmpeg
        w, h, fps, vbr = self._w, self._h, self._fps, self._vbr

        def _transcode_one(job):
            idx, city_key, city_name, segments, out_ts, display_name, weather = job
            ok = transcode_city(
                city_name, segments, out_ts, ffmpeg, w, h, fps, vbr,
                display_name=display_name,
                weather_text=weather,
            )
            # Ham segmentleri her halükarda temizle
            for seg in segments:
                try: seg.unlink()
                except Exception: pass
            return idx, out_ts if ok else None

        log.info(f"[builder] Batch {batch_id} sıralı transcode başlıyor ({len(transcode_jobs)} şehir)...")
        t_tc = time.time()
        ordered_results: dict[int, Optional[Path]] = {}
        # max_workers=1: streamer FFmpeg (-15 nice) zaten 1 CPU çekirdek tüketir.
        # 3 vCPU'da 2 paralel transcode + streamer = load ~11. Sıralı transcode
        # batch süresini ~2× artırır ama streamer'da hiç speed düşüşü olmaz.
        with ThreadPoolExecutor(max_workers=1) as tc_pool:
            for idx_out, ts_path in tc_pool.map(_transcode_one, transcode_jobs):
                ordered_results[idx_out] = ts_path

        dt_tc = time.time() - t_tc
        log.info(f"[builder] Paralel transcode tamamlandı ({dt_tc:.0f}s)")

        if self._stop.is_set():
            return None

        # Kamera sırasına göre concat listesi oluştur
        city_ts_files: list[Path] = []
        city_keys_used: list[str] = []   # quad için aynı sırada
        for idx, city_key, city_name, _, out_ts, _, _ in sorted(transcode_jobs, key=lambda j: j[0]):
            ts = ordered_results.get(idx)
            if ts is not None:
                city_ts_files.append(ts)
                city_keys_used.append(city_key)
            else:
                log.warning(f"[builder] {city_name} transcode başarısız, batch'ten çıkarıldı")

        if not city_ts_files:
            log.error(f"[builder] Batch {batch_id}: hiç şehir başarılı olmadı")
            shutil.rmtree(batch_dir, ignore_errors=True)
            return None

        # QUAD KALDIRILDI — eski stable formata dönüş (4 şehir, quad yok).
        # OOM ve memory pressure azaltıldı. Quad generate_quad() fonksiyonu
        # dosyada duruyor — gelecekte tekrar istenirse buraya çağrı eklenir.

        if self._stop.is_set():
            return None

        # ── ffmpeg concat demuxer + remux → batch_N.ts ───────────────────────
        # Eskiden binary concat (shutil.copyfileobj). Hızlı ama her city .ts'i
        # kendi PCR clock'uyla başladığı için sınırlarda PTS jump → streamer
        # FFmpeg'in fps/setpts filtresi geri-giden PTS'i reddedip donuyor →
        # watchdog 60s sonra kill ediyor (gözlemlenen sorun).
        #
        # Çözüm: concat demuxer + -fflags +genpts. PTS'leri yeniden üretip
        # tek monotonic akış yapar. -c copy → encode yok, sadece remux, ~5-10s.
        batch_path = self._work_dir / f"batch_{batch_id:04d}.ts"
        concat_list = batch_path.with_suffix(".concat.txt")
        log.info(f"[builder] Batch {batch_id} birleştiriliyor ({len(city_ts_files)} şehir, ffmpeg concat+remux)...")
        try:
            with open(concat_list, "w", encoding="utf-8") as f:
                for ts in city_ts_files:
                    f.write(f"file '{ts}'\n")

            concat_cmd = [
                "nice", "-n", "18",
                self._ffmpeg, "-y", "-hide_banner", "-loglevel", "warning",
                "-f", "concat", "-safe", "0", "-i", str(concat_list),
                "-c", "copy",
                "-fflags", "+genpts",
                "-avoid_negative_ts", "make_zero",
                "-muxrate", str(self._vbr * 1000),
                "-f", "mpegts",
                str(batch_path),
            ]
            t_concat = time.time()
            proc = subprocess.Popen(
                concat_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                start_new_session=True, **_NW,
            )
            _track_proc(proc)
            try:
                _, stderr_b = proc.communicate(timeout=180)
            finally:
                _untrack_proc(proc)
            if proc.returncode != 0:
                err = (stderr_b[-500:].decode("utf-8", errors="replace")
                       if stderr_b else "")
                log.error(f"[builder] Concat-remux hatası (rc={proc.returncode}):\n{err}")
                shutil.rmtree(batch_dir, ignore_errors=True)
                return None
            log.info(f"[builder] Concat-remux tamamlandı ({time.time()-t_concat:.0f}s)")
        except subprocess.TimeoutExpired:
            try:
                _kill_proc_group(proc, timeout=2)  # type: ignore[has-type]
                _untrack_proc(proc)                # type: ignore[has-type]
            except Exception:
                pass
            log.error(f"[builder] Concat-remux zaman aşımı (180s)")
            shutil.rmtree(batch_dir, ignore_errors=True)
            return None
        except Exception as e:
            log.error(f"[builder] Concat istisnası: {e}")
            shutil.rmtree(batch_dir, ignore_errors=True)
            return None
        finally:
            try: concat_list.unlink()
            except Exception: pass

        # City TS'leri temizle
        shutil.rmtree(batch_dir, ignore_errors=True)

        estimated_duration = float(len(city_ts_files) * self._dur)
        size_mb  = batch_path.stat().st_size / 1e6
        dt_total = time.time() - t0
        log.info(
            f"[builder] ═══ Batch {batch_id} HAZIR: {batch_path.name} "
            f"({size_mb:.0f}MB, ~{estimated_duration:.0f}s içerik, toplam {dt_total:.0f}s) ═══"
        )
        return batch_path, estimated_duration

# ─── Gerçek Zamanlı Yardımcılar ─────────────────────────────────────────────

import ctypes, ctypes.util as _ctypes_util

_libc = ctypes.CDLL(_ctypes_util.find_library("c"), use_errno=True)

_SCHED_FIFO      = 1
_CLOCK_MONOTONIC = 1

class _SchedParam(ctypes.Structure):
    _fields_ = [("sched_priority", ctypes.c_int)]

class _Timespec(ctypes.Structure):
    _fields_ = [("tv_sec", ctypes.c_long), ("tv_nsec", ctypes.c_long)]

def _set_realtime(priority: int = 50) -> bool:
    """Thread'i SCHED_FIFO önceliğine al — sleep hassasiyeti için."""
    try:
        param = _SchedParam(priority)
        return _libc.sched_setscheduler(0, _SCHED_FIFO, ctypes.byref(param)) == 0
    except Exception:
        return False

def _nanosleep(seconds: float):
    """clock_nanosleep ile yüksek hassasiyetli uyku (~1μs jitter, Python sleep ~10ms)."""
    if seconds <= 0:
        return
    tv_sec  = int(seconds)
    tv_nsec = int((seconds - tv_sec) * 1_000_000_000)
    if tv_nsec >= 1_000_000_000:
        tv_sec += 1; tv_nsec -= 1_000_000_000
    _libc.clock_nanosleep(_CLOCK_MONOTONIC, 0,
                          ctypes.byref(_Timespec(tv_sec, tv_nsec)), None)

# ─── Stream Manager ──────────────────────────────────────────────────────────

class StreamManager:
    """
    Named pipe (FIFO) üzerinden tek FFmpeg süreci ile 7/24 yayın.

    Mimari (v4.1 — video pipe + SCHED_FIFO + clock_nanosleep):
      - Named pipe (video byte akışı) → FFmpeg stdin
      - _writer thread: SCHED_FIFO prio=50, clock_nanosleep ile hassas rate control
      - Filler: batch yokken siyah kare ts yazılır (RTMP bağlantısı canlı kalır)
      - Batch: binary concat .ts dosyası, VBR oranında pipe'a yazılır
      - setpts=N/fps/TB: FFmpeg tarafında monotonic PTS üretir (DTS süreksizliği yok)
      - -re YOK: rate control yazıcı tarafında, FFmpeg mümkün olduğunca hızlı okur

    Eski sorunlar ve çözümleri:
      - sleep() jitter (~10ms) → clock_nanosleep (~1μs) ile çözüldü
      - 512KB chunk → 32KB chunk: daha sık yazma, pipe hiç boşalmaz
      - SCHED_FIFO: real-time öncelik, preemption yok
    """

    def __init__(self, cfg: dict, work_dir: Path):
        self._cfg        = cfg
        self._work_dir   = work_dir
        self._ffmpeg     = cfg.get("ffmpeg", "/usr/bin/ffmpeg")
        self._pipe_path  = work_dir / "stream.pipe"
        self._fps        = cfg.get("fps", 25)
        self._w          = cfg.get("width", 1080)
        self._h          = cfg.get("height", 1920)
        self._vbr        = cfg.get("video_bitrate", cfg.get("bitrate", 2500))
        self._abr        = cfg.get("audio_bitrate", 128)
        self._filler_path: Optional[Path] = None

        # maxsize=1: build loop en fazla 1 batch önceden inşa eder.
        # B (streaming read) ile RAM tasarrufu zaten sağlanıyor; queue=1
        # build/play yakınlığını korur (eski queue=2 → 32dk önden inşa, gereksiz).
        self._batch_q: Queue = Queue(maxsize=1)

        self._stop         = threading.Event()
        self._pipe_broken  = threading.Event()   # writer → supervisor: pipe koptu
        self._proc:         Optional[subprocess.Popen] = None
        self._writer_thr:   Optional[threading.Thread] = None
        self._monitor_thr:  Optional[threading.Thread] = None
        self._supervisor_thr: Optional[threading.Thread] = None

        # Crash kurtarma: _bg_fetch tarafından alınan ama henüz yazılmamış batch.
        # Thread ölse de self üzerinde yaşar, yeni writer bunu önce kullanır.
        # B refactor sonrası sadece path saklıyoruz (data RAM'e yüklenmiyor).
        self._saved_next_item: Optional[tuple] = None

        self.current_batch: str = ""

    # ── Public ───────────────────────────────────────────────────────────────

    def _generate_filler(self) -> Optional[Path]:
        """
        5 saniyelik siyah kare filler.
        Batch hazırlanırken playlist'e tekrar tekrar eklenerek
        RTMP bağlantısı canlı tutulur.
        """
        filler_path = self._work_dir / "filler.ts"
        if filler_path.exists():
            return filler_path

        w, h, fps, vbr = self._w, self._h, self._fps, self._vbr
        clock_text = r"%{localtime\:%H\:%M}"
        wait_esc   = _esc_drawtext("Hazırlanıyor...")

        vf = (
            f"drawtext=fontfile={FONT_PATH}:text='{clock_text}':"
            f"x=w-150:y=18:fontsize=48:fontcolor=white:"
            f"box=1:boxcolor=black@0.45:boxborderw=8,"
            f"drawtext=fontfile={FONT_PATH}:text='{wait_esc}':"
            f"x=(w-text_w)/2:y=(h-text_h)/2:fontsize=36:fontcolor=white@0.6:"
            f"box=1:boxcolor=black@0.3:boxborderw=10"
        )

        cmd = [
            self._ffmpeg, "-y", "-hide_banner", "-loglevel", "warning",
            "-f", "lavfi", "-i", f"color=black:size={w}x{h}:rate={fps}",
            "-t", "5",
            "-vf", vf,
            "-c:v", "libx264", "-preset", "ultrafast",
            "-b:v", f"{vbr}k", "-maxrate", f"{vbr}k", "-bufsize", f"{vbr*2}k",
            "-g", str(fps * 2), "-an",
            "-f", "mpegts",
            str(filler_path),
        ]
        try:
            result = subprocess.run(
                cmd, capture_output=True, timeout=30,
                start_new_session=True, **_NW,
            )
            if result.returncode == 0:
                log.info(f"[stream] Filler hazır ({filler_path.stat().st_size/1e6:.2f}MB)")
                return filler_path
            else:
                err = result.stderr[-300:].decode("utf-8", errors="replace")
                log.warning(f"[stream] Filler oluşturulamadı: {err}")
                return None
        except Exception as e:
            log.warning(f"[stream] Filler hatası: {e}")
            return None

    def start(self):
        """Video FIFO oluştur, writer, FFmpeg ve supervisor'ı başlat."""
        self._filler_path = self._generate_filler()
        self._launch_ffmpeg_and_writer()

        self._supervisor_thr = threading.Thread(
            target=self._supervisor, daemon=True, name="ffmpeg_supervisor"
        )
        self._supervisor_thr.start()

    def _launch_ffmpeg_and_writer(self):
        """Pipe oluştur, writer ve FFmpeg'i (yeniden) başlat."""
        if self._pipe_path.exists():
            self._pipe_path.unlink()
        os.mkfifo(str(self._pipe_path))
        self._pipe_broken.clear()

        # Writer önce başlar (FIFO write ucunu açmak için FFmpeg'i bekler)
        self._writer_thr = threading.Thread(
            target=self._writer, daemon=True, name="pipe_writer"
        )
        self._writer_thr.start()

        # FFmpeg başlar → pipe read ucunu açar → writer unblock olur
        self._start_ffmpeg()

        self._monitor_thr = threading.Thread(
            target=self._monitor, daemon=True, name="ffmpeg_monitor"
        )
        self._monitor_thr.start()

    def feed(self, batch_path: Path, duration_sec: float):
        """
        Bir batch dosyasını stream kuyruğuna ekle.
        Queue doluysa (maxsize=2) bloklanır → build loop hızını stream'e eşitler.
        """
        while not self._stop.is_set():
            try:
                self._batch_q.put((batch_path, duration_sec), timeout=2)
                log.info(f"[stream] Kuyruğa eklendi: {batch_path.name} (~{duration_sec:.0f}s)")
                return
            except Exception:
                continue

    def stop(self):
        self._stop.set()
        try:
            self._batch_q.put(None, block=False)
        except Exception:
            pass
        # Process group SIGTERM → wait 5s → SIGKILL fallback.
        # start_new_session=True ile spawn edilen ffmpeg'ler kendi grubunda;
        # tüm alt ffmpeg'ler (filter/encoder thread'leri) tek darbede ölür.
        if self._proc and self._proc.poll() is None:
            _kill_proc_group(self._proc, timeout=5)

    # ── Private ──────────────────────────────────────────────────────────────

    def _build_cmd(self) -> list[str]:
        # MediaMTX relay mimarisi:
        # Bu FFmpeg yalnızca localhost:1935'e (MediaMTX) yazar — ağ hatası yok.
        # MediaMTX runOnReady ile kendi FFmpeg'ini başlatır ve YouTube + Kick'e
        # FIFO muxer ile iletir.
        #
        # v4.2 — PTS jump'a dayanıklı sadeleştirilmiş filter chain:
        # Önceki sürüm (v4.1) filter_complex (fps=N + setpts + drawtext + aevalsrc
        # + aresample) kullanıyordu. Filler→batch geçişinde input PTS geri sıçradığı
        # için fps filter input bekledi, libx264 lookahead doldu, frame counter
        # 60s sabit kaldı → watchdog kill. Tekrarlayan crash gözlemlendi.
        #
        # Yeni yaklaşım:
        #   - filter_complex KALDIRILDI (scale/pad zaten transcode_city'de yapılıyor;
        #     batch ve filler 1280x720 25fps)
        #   - Audio için ayrı lavfi anullsrc input (silent stereo 44.1k AAC)
        #   - -r 25 -vsync cfr: output framerate ZORLA sabit, input PTS ne olursa olsun
        #     drop/dup ile 25fps üretir → PTS jump'tan etkilenmez
        #   - -tune zerolatency: libx264 lookahead=0, anında encode
        #   - -copyts + -avoid_negative_ts make_zero: PTS shift'leri toleranslı
        #   - Clock overlay kaldırıldı (gerekirse transcode_city içinde gömülebilir)
        mediamtx_url = self._cfg.get("mediamtx_rtmp", "rtmp://127.0.0.1:1935/live/stream")
        out_args = ["-f", "flv", mediamtx_url]

        fps = self._fps
        vbr = self._vbr
        abr = self._abr

        # v4.5 — Klasik müzik audio kaynağı (telifsiz Internet Archive PD koleksiyonu)
        # 128 mp3, ~17 saat müzik /opt/KameraShorts/music/ altında.
        # playlist.txt karışık sıra, -stream_loop -1 ile sonsuz tekrar.
        # Eski anullsrc (sessiz) → concat demuxer'a değişti.
        music_playlist = "/opt/KameraShorts/music/playlist.txt"
        from os.path import exists as _exists
        has_music = _exists(music_playlist)
        audio_input = (
            ["-stream_loop", "-1", "-f", "concat", "-safe", "0", "-i", music_playlist]
            if has_music else
            ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100"]
        )

        # v4.5 — Anik mesaj overlay: drawtext + textfile + reload=1
        # /var/lib/kamerashorts/stream_message.txt dosyasini her frame okur.
        # Dosya bos ise drawtext gorunmez (text="" hide). Dolu ise alt-orta'da
        # kirmizi banner gorunur. Dashboard /api/stream-message POST eder.
        msg_file = "/var/lib/kamerashorts/stream_message.txt"
        from os.path import exists as _exists, dirname as _dirname
        from os import makedirs as _makedirs
        _makedirs(_dirname(msg_file), exist_ok=True)
        if not _exists(msg_file):
            with open(msg_file, "w", encoding="utf-8") as _f:
                _f.write("")
        font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
        msg_vf = (
            f"drawtext=fontfile={font_path}:"
            f"textfile={msg_file}:reload=1:"
            f"x=(w-text_w)/2:y=h-180:"
            f"fontsize=44:fontcolor=white:"
            f"box=1:boxcolor=red@0.85:boxborderw=14:"
            f"shadowx=2:shadowy=2:shadowcolor=black@0.8"
        )

        return [
            "nice", "-n", "-5",
            self._ffmpeg, "-hide_banner",
            # v4.4 — minimal cmd. Önceki sürümlerde:
            #   v4.1 filter_complex (fps+setpts+drawtext+aevalsrc+aresample) PTS jump'ta
            #        deadlock yapıyordu → watchdog crash.
            #   v4.2 -tune zerolatency tek-thread → 0.97x → YouTube tee disconnect.
            #   v4.3 -r 25 -vsync cfr -avoid_negative_ts kombo DEADLOCK → output yok.
            # v4.4: minimum dependencies, libx264 default multi-thread, +genpts PTS için.
            "-fflags", "+genpts+discardcorrupt",
            "-i", str(self._pipe_path),
        ] + audio_input + [
            "-map", "0:v:0", "-map", "1:a:0",
            "-vf", msg_vf,
            "-c:v", "libx264", "-preset", "ultrafast",
            "-b:v", f"{vbr}k", "-maxrate", f"{vbr}k", "-bufsize", f"{vbr*2}k",
            "-g", str(fps * 2),
            # Ses: AAC encode + ambient seviyesi (%40 = sokak fonu hissi).
            # Müzik telifsiz → YouTube Content ID claim riski yok.
            "-af", "volume=0.4",
            "-c:a", "aac", "-b:a", f"{abr}k",
            "-stats",
        ] + out_args

    def _supervisor(self):
        """
        FFmpeg çöktüğünde otomatik yeniden başlatır.
        _pipe_broken eventi set edilince tetiklenir (writer veya monitor tarafından).

        Spurious-crash koruması: backoff bitince proc.poll() kontrolü yapılır.
        Eğer FFmpeg hâlâ çalışıyorsa (eski thread'lerin neden olduğu sahte sinyal),
        flag temizlenir ve restart YAPILMAZ. Bu, sağlıklı FFmpeg'in öldürülmesini önler.
        """
        backoff = 5
        while not self._stop.is_set():
            # Pipe kopmasını bekle (normal çalışmada bu olay hiç gelmez)
            self._pipe_broken.wait(timeout=10)
            if self._stop.is_set():
                break
            if not self._pipe_broken.is_set():
                continue

            log.warning(f"[supervisor] FFmpeg çöktü! {backoff}s sonra kontrol ediliyor...")
            self._stop.wait(backoff)
            if self._stop.is_set():
                break

            # ── SPURIOUS CRASH KONTROLÜ ─────────────────────────────────────
            # Backoff bittikten sonra FFmpeg hâlâ canlıysa, _pipe_broken eski
            # bir thread tarafından set edilmiş demektir. Sağlıklı FFmpeg'i
            # öldürme — sadece flag'i temizle ve devam et.
            if self._proc and self._proc.poll() is None:
                log.info(
                    f"[supervisor] ✓ FFmpeg (PID={self._proc.pid}) hâlâ çalışıyor — "
                    f"sahte alarm, restart iptal. Flag temizleniyor."
                )
                self._pipe_broken.clear()
                # Sağlıklı çalışıyorsa backoff'u sıfırla (sonraki gerçek crash için)
                backoff = 5
                continue

            # Gerçek crash — eski proc zaten ölü ama emin olalım
            if self._proc:
                try:
                    self._proc.wait(timeout=2)
                except Exception:
                    try: self._proc.kill()
                    except Exception: pass

            log.info("[supervisor] Yeniden başlatılıyor...")
            self._launch_ffmpeg_and_writer()
            backoff = min(backoff * 2, 60)  # 5s → 10s → 20s → 40s → 60s (max)
            log.info(f"[supervisor] Yeniden başlatıldı. Sonraki backoff: {backoff}s")

    def _start_ffmpeg(self):
        cmd = self._build_cmd()
        log.info("[stream] FFmpeg başlatılıyor (video pipe + SCHED_FIFO + nanosleep)...")
        # start_new_session=True → process group leader. stop() içinde killpg
        # ile tüm alt thread'leri tek darbede temiz öldürürüz.
        self._proc = subprocess.Popen(
            cmd, stderr=subprocess.PIPE, start_new_session=True, **_NW,
        )
        _track_proc(self._proc)
        log.info(f"[stream] FFmpeg PID={self._proc.pid}")

    def _write_to_pipe(self, pipe_fd, data: bytes, content_duration_sec: float):
        """
        Veriyi pipe'a belirtilen süre içinde yazar (rate limiting).

        content_duration_sec > 0  → filler: gerçek süreye göre oranla
        content_duration_sec == 0 → batch: VBR*1.02 B/s (hafif hızlı, buffer için)
        """
        # KRİTİK: pipe zaten kırılmışsa, asla yazma — eski thread'lerin spurious
        # _pipe_broken set etmesini engeller (supervisor sağlıklı FFmpeg öldürmesin)
        if self._pipe_broken.is_set() or self._stop.is_set():
            return
        CHUNK = 32768  # 32KB — daha sık yazma, pipe hiç boşalmaz
        total = len(data)
        if total == 0:
            return

        vbr_bps   = self._vbr * 1000 / 8        # byte/s (video)
        abr_bps   = self._abr * 1000 / 8        # byte/s (audio)
        total_bps = (vbr_bps + abr_bps) * 1.02  # toplam + %2 overhead

        if content_duration_sec > 0:
            # Filler: dosyanın gerçek süresine göre yaz → her filler tam content_duration_sec sürer
            # _writer'da get_nowait() kullandığımız için arada gap yok → net 1.0x
            target_bps = total / content_duration_sec
        else:
            target_bps = total_bps

        written = 0
        t_start = time.monotonic()
        while written < total and not self._stop.is_set():
            end   = min(written + CHUNK, total)
            chunk = data[written:end]
            try:
                pipe_fd.write(chunk)
                pipe_fd.flush()
            except BrokenPipeError:
                log.error("[pipe] ✗ Kırık pipe — FFmpeg kapandı")
                self._pipe_broken.set()  # supervisor'ı tetikle, _stop set etme
                return
            written += len(chunk)

            # Monotonic zamanlama
            elapsed  = time.monotonic() - t_start
            expected = written / target_bps
            slack    = expected - elapsed
            if slack > 0.0001:
                _nanosleep(slack)

    def _write_file_to_pipe(self, pipe_fd, file_path: Path, content_duration_sec: float):
        """
        Dosyayı streaming halinde pipe'a yazar — RAM'e yüklemeden.
        _write_to_pipe(bytes) yerine batch dosyaları için bu kullanılır:
          - read_bytes() ile 100MB tampon yerine 32KB chunk
          - target_bps file size'a göre veya VBR'e göre hesaplanır
          - Rate control aynı (clock_nanosleep)

        Filler için _write_to_pipe (bytes versiyonu) korunur — 5MB tamponu
        sürekli yeniden okumamak için tek sefer yüklenir.
        """
        if self._pipe_broken.is_set() or self._stop.is_set():
            return
        try:
            total = file_path.stat().st_size
        except OSError as e:
            log.error(f"[pipe] Dosya stat hatası: {e}")
            return
        if total == 0:
            return

        CHUNK = 32768
        vbr_bps   = self._vbr * 1000 / 8
        abr_bps   = self._abr * 1000 / 8
        total_bps = (vbr_bps + abr_bps) * 1.02

        if content_duration_sec > 0:
            target_bps = total / content_duration_sec
        else:
            target_bps = total_bps

        written = 0
        t_start = time.monotonic()
        try:
            with open(file_path, "rb") as src:
                while not self._stop.is_set():
                    chunk = src.read(CHUNK)
                    if not chunk:
                        break
                    try:
                        pipe_fd.write(chunk)
                        pipe_fd.flush()
                    except BrokenPipeError:
                        log.error("[pipe] ✗ Kırık pipe — FFmpeg kapandı")
                        self._pipe_broken.set()
                        return
                    written += len(chunk)

                    # Monotonic zamanlama
                    elapsed  = time.monotonic() - t_start
                    expected = written / target_bps
                    slack    = expected - elapsed
                    if slack > 0.0001:
                        _nanosleep(slack)
        except FileNotFoundError:
            log.error(f"[pipe] Dosya kayıp: {file_path}")
            return
        except Exception as e:
            log.error(f"[pipe] Dosya okuma hatası: {e}")
            self._pipe_broken.set()

    def _writer(self):
        """
        Video pipe yazıcı — SCHED_FIFO önceliğiyle çalışır.

        Sıra:
          1. SCHED_FIFO prio=50 al
          2. FIFO aç (FFmpeg bağlanana kadar bloklanır)
          3. Filler döngüsü: batch_q'dan batch gelene kadar filler yaz
          4. Batch döngüsü: batch'leri sırayla pipe'a yaz, arasında filler
        """
        if _set_realtime(50):
            log.info("[pipe] SCHED_FIFO prio=50 ayarlandı")
        else:
            log.warning("[pipe] SCHED_FIFO ayarlanamadı (root gerekli?)")

        # Crash sonrası yeni writer: stale label sıfırla (filler/batch ayrımı net olsun)
        self.current_batch = ""

        log.info("[pipe] FIFO açılıyor (FFmpeg bağlanana kadar bekler)...")
        try:
            with open(str(self._pipe_path), "wb") as pipe:
                log.info("[pipe] FIFO bağlandı, yazma başlıyor")

                filler_data = None
                filler_dur  = 0.0
                if self._filler_path and self._filler_path.exists():
                    filler_data = self._filler_path.read_bytes()
                    filler_dur  = 5.0  # filler.ts süresi

                # ── Crash kurtarma: önceki writer thread'den kalan path ──
                # B refactor: data RAM'e yüklenmiyor; sadece path saklanır.
                # Yeni writer dosyayı kendisi streaming olarak açar.
                if self._saved_next_item is not None:
                    log.info(f"[pipe] ↩ Kurtarılan batch devreye alındı: {self._saved_next_item[0].name}")
                    item = self._saved_next_item
                    self._saved_next_item = None
                else:
                    # Normal: batch gelene kadar filler döngüsü
                    # get_nowait() kullanıyoruz — 0.5s timeout filler'lar arası boşluk yaratır
                    # ve speed 0.93x'e düşer. Anlık kontrol + filler döngüsü daha iyi.
                    # KRİTİK: _pipe_broken kontrolü ÇOK ÖNEMLİ — eski writer thread'in
                    # pipe koptuktan sonra sonsuza kadar spinning yapmasını engeller.
                    item = None
                    while not self._stop.is_set() and not self._pipe_broken.is_set():
                        try:
                            item = self._batch_q.get_nowait()
                            break
                        except Empty:
                            if filler_data:
                                self._write_to_pipe(pipe, filler_data, filler_dur)
                            else:
                                time.sleep(0.05)
                            continue

                if self._stop.is_set() or item is None:
                    return

                # Batch döngüsü
                # Tasarım: batch yaz → filler+arka plan okuma → sonraki batch
                # preload_done.wait() KULLANILMIYOR — pipe hiç boş kalmaz

                while item is not None and not self._stop.is_set():
                    batch_path, duration_sec = item

                    self.current_batch = batch_path.name
                    log.info(f"[pipe] ▶ Yazılıyor: {batch_path.name} (~{duration_sec:.0f}s)")
                    # Bu batch artık aktif yazılıyor — saved_next temizlendi (başarılı teslim)
                    self._saved_next_item = None

                    # Streaming yazma — RAM'e tam batch yüklenmiyor (32KB chunk)
                    self._write_file_to_pipe(pipe, batch_path, 0.0)

                    if self._stop.is_set() or self._pipe_broken.is_set():
                        break

                    # Batch bitti → filler yaz + sonraki batch'i arka planda al
                    # _bg_fetch sadece kuyruktan path alır; disk okuma writer'a kaldı.
                    next_item: list = [None]
                    read_done = threading.Event()

                    def _bg_fetch():
                        while not self._stop.is_set() and not self._pipe_broken.is_set():
                            try:
                                next_item[0] = self._batch_q.get_nowait()
                                # Hemen self'e kaydet — thread ölürse path kaybolmaz
                                self._saved_next_item = next_item[0]
                                log.info(f"[pipe] ✓ Sıradaki hazır: {next_item[0][0].name}")
                                break
                            except Empty:
                                time.sleep(0.05)
                        read_done.set()

                    fetch_thr = threading.Thread(target=_bg_fetch, daemon=True, name="bg_fetch")
                    fetch_thr.start()

                    # Arka plan path alımı biterken filler yaz (pipe boş kalmaz)
                    warned = False
                    while not read_done.is_set() and not self._stop.is_set() and not self._pipe_broken.is_set():
                        if filler_data:
                            if not warned and next_item[0] is None:
                                log.info("[pipe] Sonraki batch bekleniyor, filler yazılıyor...")
                                warned = True
                            self._write_to_pipe(pipe, filler_data, filler_dur)
                        else:
                            time.sleep(0.05)

                    item = next_item[0]

        except BrokenPipeError:
            log.error("[pipe] ✗ Kırık pipe — FFmpeg kapandı")
            self._pipe_broken.set()
        except Exception as e:
            log.error(f"[pipe] FIFO hatası: {e}")
            self._pipe_broken.set()

    def _monitor(self):
        """
        FFmpeg stderr → gelişmiş durum logları.

        Takip edilen kategoriler:
          [durum]   — her saniye speed/fps/frame
          [fifo]    — FIFO muxer recovery olayları (disconnect / reconnect / hata)
          [rtmp]    — RTMP bağlantı olayları
          [trend]   — speed trendi uyarısı (3 ardışık düşük okuma)
          [watchdog]— frame donması (zombi FFmpeg tespiti)
          [stream]  — diğer FFmpeg uyarı/hataları
        """
        if not self._proc:
            return

        speed_re = re.compile(r"speed=\s*([\d.]+)x")
        fps_re   = re.compile(r"fps=\s*([\d.]+)")
        frame_re = re.compile(r"frame=\s*(\d+)")
        buf = b""

        # Trend takibi: son N speed değeri
        _speed_history: list[float] = []
        _TREND_WINDOW  = 5      # kaç okuma
        _TREND_THRESH  = 0.90   # bu altında kalırsa uyar

        # FIFO recovery zamanlama
        _fifo_disconnect_at: Optional[float] = None

        # Watchdog başlangıç değerleri
        self._wd_frame = None
        self._wd_since = time.monotonic()

        while True:
            chunk = self._proc.stderr.read(256)
            if not chunk:
                break
            buf += chunk
            while b"\r" in buf or b"\n" in buf:
                ri = buf.find(b"\r")
                ni = buf.find(b"\n")
                idx = min(ri if ri >= 0 else len(buf), ni if ni >= 0 else len(buf))
                line = buf[:idx].decode("utf-8", errors="replace").strip()
                buf  = buf[idx + 1:]
                if not line:
                    continue

                sm  = speed_re.search(line)
                fm  = fps_re.search(line)
                frm = frame_re.search(line)

                if sm and fm and frm:
                    speed = float(sm.group(1))
                    fps_v = float(fm.group(1))
                    frame = int(frm.group(1))

                    # ── Durum logu ──────────────────────────────────────────
                    lvl = logging.WARNING if speed < 0.85 else logging.INFO
                    log.log(lvl,
                        f"[durum] batch={self.current_batch} "
                        f"speed={speed:.2f}x fps={fps_v:.0f} frame={frame}"
                    )

                    # ── Trend analizi ────────────────────────────────────────
                    _speed_history.append(speed)
                    if len(_speed_history) > _TREND_WINDOW:
                        _speed_history.pop(0)
                    if len(_speed_history) == _TREND_WINDOW:
                        avg = sum(_speed_history) / _TREND_WINDOW
                        all_low = all(s < _TREND_THRESH for s in _speed_history)
                        if all_low:
                            log.warning(
                                f"[trend] ⚠ Son {_TREND_WINDOW} okumada speed düşük "
                                f"(ort={avg:.2f}x) — CPU yükü veya pipe sorunu olabilir"
                            )
                            _speed_history.clear()  # tekrar uyarma

                    # ── Watchdog (zombi tespiti) — 60s ──────────────────────
                    # FIFO muxer reconnect süresi: 5s wait + ~10s YouTube handshake
                    # Bu süre içinde frame donabilir — bu normal, watchdog erken tetiklenmemeli.
                    # 60s: FIFO'ya yeterli süre verirken gerçek zombileri (program kilitleri) yakalar.
                    if self._wd_frame is None:
                        self._wd_frame = frame
                        self._wd_since = time.monotonic()
                    elif frame != self._wd_frame:
                        self._wd_frame = frame
                        self._wd_since = time.monotonic()
                    elif time.monotonic() - self._wd_since > 60:
                        log.error(
                            f"[watchdog] Frame {frame} 60s boyunca değişmedi — "
                            f"FFmpeg zombi (FIFO da kurtaramadı), öldürülüyor"
                        )
                        if self._proc and self._proc.poll() is None:
                            self._proc.kill()
                        self._wd_since = time.monotonic()

                else:
                    # ── FIFO muxer recovery mesajları ───────────────────────
                    low = line.lower()

                    if "recovery attempt" in low or "will attempt recovery" in low:
                        _fifo_disconnect_at = _fifo_disconnect_at or time.monotonic()
                        # Deneme numarasını çıkar
                        m = re.search(r"attempt[^\d]*(\d+)", low)
                        attempt_n = m.group(1) if m else "?"
                        log.warning(
                            f"[fifo] ⚡ Yeniden bağlanmaya çalışıyor "
                            f"(deneme #{attempt_n}) — YouTube bağlantısı kopmuş olabilir"
                        )

                    elif "recovery successful" in low:
                        dur = time.monotonic() - (_fifo_disconnect_at or time.monotonic())
                        log.info(f"[fifo] ✓ Bağlantı yeniden kuruldu ({dur:.1f}s sonra)")
                        _fifo_disconnect_at = None

                    elif "non-recoverable" in low:
                        log.error(f"[fifo] ✗ Kurtarılamaz hata — FIFO muxer teslim oldu: {line}")

                    # ── RTMP bağlantı olayları ───────────────────────────────
                    elif "rtmp" in low and any(k in low for k in
                            ("connect", "disconnect", "publish", "close", "handshake")):
                        log.info(f"[rtmp] {line}")

                    # ── flv mux normal kapanış gürültüsü ─────────────────────
                    # "Failed to update header with correct duration/filesize"
                    # → flv format'ının canlı yayında her zaman yazdığı bir not.
                    # Yanıltıcı WARNING vermesin.
                    elif "failed to update header" in low:
                        log.debug(f"[stream] {line}")

                    # ── Genel hata/uyarılar ──────────────────────────────────
                    elif any(k in low for k in
                            ("error", "invalid", "failed", "corrupt", "broken", "drop")):
                        log.warning(f"[stream] {line}")

                    else:
                        log.debug(f"[stream] {line}")

        rc = self._proc.wait()
        _untrack_proc(self._proc)
        if not self._stop.is_set():
            log.error(
                f"[stream] ⚠ FFmpeg beklenmedik çıkış (rc={rc}) — supervisor tetikleniyor"
            )
            self._pipe_broken.set()

# ─── Ana Uygulama ────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="KameraShorts Live Streamer v4")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--log",    default=None)
    args = parser.parse_args()

    _setup_logging(args.log)

    import os as _os
    try:
        _os.nice(-10)
        log.info("[main] Süreç önceliği artırıldı (nice -10)")
    except Exception as _e:
        log.warning(f"[main] Nice ayarlanamadı: {_e}")
    cfg      = load_config(args.config)
    work_dir = Path(cfg.get("work_dir", "/tmp/ks_v4"))
    work_dir.mkdir(parents=True, exist_ok=True)

    cameras   = cfg.get("cameras", [])
    city_dur  = cfg.get("city_duration", 600)
    total_dur = city_dur * len(cameras)

    log.info("=" * 60)
    log.info("KameraShorts Live Streamer v4.1 — video pipe + SCHED_FIFO + nanosleep")
    log.info("=" * 60)
    log.info(f"FFmpeg     : {cfg.get('ffmpeg', '/usr/bin/ffmpeg')}")
    log.info(f"MediaMTX   : {cfg.get('mediamtx_rtmp', 'rtmp://127.0.0.1:1935/live/stream')}")
    log.info(f"YouTube    : {cfg.get('youtube_rtmp_url', '')[:50]}... (MediaMTX relay)")
    log.info(f"Kick       : {'var' if cfg.get('kick_rtmp_url') else 'yok'} (MediaMTX relay)")
    log.info(f"Çözünürlük : {cfg.get('width', 1080)}x{cfg.get('height', 1920)} @ {cfg.get('fps', 25)}fps")
    log.info(f"Video br   : {cfg.get('video_bitrate', 2500)}k")
    log.info(f"Şehir sür. : {city_dur}s/şehir × {len(cameras)} şehir = ~{total_dur//60}dk/batch")
    log.info(f"Şehirler   : {[c.get('city', c.get('type')) for c in cameras]}")
    log.info(f"Work dir   : {work_dir}")

    # ── Başlangıç temizliği ─────────────────────────────────────────────────
    log.info("[main] Başlangıç temizliği yapılıyor...")
    for p in work_dir.glob("batch_*.ts"):
        try:
            p.unlink()
            log.info(f"[main] Silindi: {p.name}")
        except Exception as e:
            log.warning(f"[main] Silinemedi {p.name}: {e}")
    for p in work_dir.glob("b[0-9]*"):
        shutil.rmtree(p, ignore_errors=True)
    # 1 saatten eski tmp*.ts artıkları (geçmiş crash'lerden — disk yer kazanımı)
    _now = time.time()
    _orphan = 0
    for tmp in work_dir.rglob("tmp*.ts"):
        try:
            if _now - tmp.stat().st_mtime > 3600:
                tmp.unlink()
                _orphan += 1
        except Exception:
            pass
    if _orphan:
        log.info(f"[main] Eski tmp segment temizlendi: {_orphan} dosya")
    for fifo in ["stream.pipe", "playlist.fifo"]:
        fp = work_dir / fifo
        if fp.exists():
            fp.unlink()

    global_stop = threading.Event()

    def _sig(sig, _):
        log.info(f"Sinyal {sig} alındı, kapatılıyor...")
        global_stop.set()

    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT,  _sig)

    builder = BatchBuilder(cfg, work_dir, global_stop)
    stream  = StreamManager(cfg, work_dir)

    # ── FFmpeg + Filler başlat (stream öncesi RTMP bağlantısını kur) ────────
    stream.start()

    # ── Batch 0 hazır olur olmaz stream başlar ──────────────────────────────
    # Batch 0 oynuyor (~10dk), Batch 1 arka planda inşa ediliyor (~10dk).
    # Süreler eşit olduğu için Batch 1 tam zamanında hazır → kesintisiz akış.
    # Eski yöntem (2 batch bekle) ~20dk filler oynatıyordu — artık ~10dk.
    log.info("[main] Ön hazırlık: Batch 0 inşa ediliyor (filler oynuyor)...")
    result0 = builder.build(0)
    if result0 is None or global_stop.is_set():
        log.error("[main] Batch 0 başarısız, çıkılıyor")
        stream.stop()
        return
    batch0, dur0 = result0

    log.info("[main] Batch 0 hazır — gerçek yayın başlıyor! (Batch 1 arka planda inşa ediliyor)")
    stream.feed(batch0, dur0)

    # ── Build + Feed döngüsü (bid=1'den başlar) ────────────────────────────
    # Akış:
    #   Batch 0 oynuyor: Build loop Batch 1'i yapıyor (~10dk)
    #   Batch 0 biter  : Batch 1 playlist'te hazırsa anında başlar (gecikmesiz)
    #                    hazır değilse filler araya girer (beklenmedik durum)
    #   → Her zaman 1 batch önde, servis açılışta ~10dk filler

    def _build_loop():
        bid = 1
        while not global_stop.is_set():
            result = builder.build(bid)
            if global_stop.is_set():
                break
            if result is not None:
                batch, dur = result
                stream.feed(batch, dur)  # Queue doluysa (maxsize=1) bloklanır
                # Disk leak fix: en yeni 2 batch'i tut, kalanını sil.
                # Writer 1 tanesini okuyor, builder 1 tanesini yeni yarattı.
                # Eski batch'ler streamer'ın hiç gerek duymadığı dosyalardır.
                try:
                    batches = sorted(
                        work_dir.glob("batch_*.ts"),
                        key=lambda p: p.stat().st_mtime,
                        reverse=True,
                    )
                    for old_batch in batches[2:]:
                        try:
                            size_mb = old_batch.stat().st_size // (1024 * 1024)
                            old_batch.unlink(missing_ok=True)
                            log.info(f"[builder] Eski batch silindi: {old_batch.name} ({size_mb}MB)")
                        except Exception as e:
                            log.warning(f"[builder] {old_batch.name} silinemedi: {e}")
                except Exception as e:
                    log.warning(f"[builder] Cleanup hatası: {e}")
            else:
                log.error(f"[main] Batch {bid} başarısız, 10s sonra tekrar deneniyor")
                global_stop.wait(10)
            bid += 1

    build_thread = threading.Thread(target=_build_loop, daemon=True, name="build_loop")
    build_thread.start()

    log.info("[main] 7/24 döngü başladı. Durdurmak için SIGTERM gönderin.")
    global_stop.wait()

    log.info("[main] Durduruluyor...")
    stream.stop()
    # Builder içindeki aktif transcoder ffmpeg'ler için ekstra güvenlik:
    # önce TERM, sonra (2s) KILL — process group bazlı.
    _kill_tracked(signal.SIGTERM)
    time.sleep(2)
    _kill_tracked(signal.SIGKILL)
    log.info("Çıkış.")


if __name__ == "__main__":
    main()
