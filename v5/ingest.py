#!/usr/bin/env python3
"""KameraShorts v5 — Ingest.

Tek sehirden HLS segmentlerini indirip /var/lib/kamerashorts/segments/<city>/
altina yazar ve SQLite'a metadata kayitlar.

Tasarim:
- TRANSCODE YOK. Segment'leri oldugu gibi (mpegts) kopyalar.
- Pillow ile sadece METADATA (brightness, motion) hesaplar.
- Halka tampon: max N segment, eski olanlari siler.
- Sehir rotasyonu: rotation_minutes gecince yeni kamera secer.
- Ankara icin: relay TTL renewal.
- systemd template: kshorts-ingest@ankara.service vb.

Kullanim:
    python -m v5.ingest --city ankara
"""
import argparse
import os
import random
import sys
import time
from pathlib import Path
from typing import Optional

import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from v5 import common, db

log = common.setup_logging("ingest")


def make_session(ssl_verify: bool = True, headers: dict = None) -> requests.Session:
    s = requests.Session()
    adapter = requests.adapters.HTTPAdapter(
        pool_connections=10, pool_maxsize=20, pool_block=False,
    )
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    s.headers["User-Agent"] = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    )
    if headers:
        s.headers.update(headers)
    s.verify = ssl_verify
    return s


class CameraResolver:
    """Sehir tipine gore canli bir kamera secer. rotation_minutes gecince
    yeni secim yapilir. Ankara icin relay yonetimi de buradadir."""

    def __init__(self, city: str, cfg: dict):
        self.city = city
        self.cfg = cfg
        self.ssl_verify = cfg.get("ssl_verify", True)
        self.rotation_seconds = cfg.get("rotation_minutes", 5) * 60
        self.session = make_session(self.ssl_verify)
        self._current: Optional[dict] = None
        self._chosen_at: float = 0
        self._relay_dvr: Optional[str] = None
        self._relay_provider: str = "ego"
        self._last_relay_renew: float = 0

    def current(self) -> Optional[dict]:
        now = time.time()
        if self._current is None or (now - self._chosen_at) > self.rotation_seconds:
            new = self._pick_new()
            if new:
                self._current = new
                self._chosen_at = now
                log.info("[%s] kamera secildi: %s", self.city, new["name"])
        if self._relay_dvr and (now - self._last_relay_renew) > self.cfg.get(
            "relay_renew_seconds", 33
        ):
            self._renew_relay()
        return self._current

    def _pick_new(self) -> Optional[dict]:
        t = self.cfg.get("type")
        try:
            if t == "ankara_api":
                return self._pick_ankara()
            if t == "istanbul_api":
                return self._pick_istanbul()
            if t == "direct_random":
                return self._pick_direct_random()
        except Exception as e:
            log.error("[%s] resolve hatasi: %s", self.city, e)
        return None

    def _pick_ankara(self) -> Optional[dict]:
        data = self.session.get(self.cfg["status_url"], timeout=15).json()
        BUS = {"Solo", "ELK", "Koruklu", "Koruklu ELK", "Minibus", "Midibus"}
        active = [v for v in data
                  if v.get("stream_url") and v.get("dvr_serial_number")
                  and v.get("is_visible") and v.get("vehicle_type") in BUS]
        if not active:
            active = [v for v in data
                      if v.get("stream_url") and v.get("dvr_serial_number")
                      and v.get("is_visible")]
        if not active:
            return None
        active.sort(key=lambda v: -(float(v.get("speed", 0) or 0)))
        v = active[0]
        plate = v.get("license_plate") or v["dvr_serial_number"]
        self._relay_dvr = v["dvr_serial_number"]
        self._relay_provider = v.get("source", "ego")
        self._start_relay()
        return {
            "name": plate,
            "stream_url": v["stream_url"],
            "plate": plate,
            "vehicle_type": v.get("vehicle_type", ""),
        }

    def _pick_istanbul(self) -> Optional[dict]:
        cams = self.cfg.get("cameras", [])
        if not cams:
            return None
        cam = random.choice(cams)
        url = self.cfg["base_url"].format(slug=cam["slug"])
        return {"name": cam["name"], "stream_url": url}

    def _pick_direct_random(self) -> Optional[dict]:
        streams = self.cfg.get("streams", [])
        if not streams:
            return None
        url = random.choice(streams)
        return {"name": url.rsplit("/", 2)[-2], "stream_url": url}

    def _start_relay(self):
        if not self._relay_dvr:
            return
        try:
            u = self.cfg["relay_start_url"].format(
                dvr=self._relay_dvr, provider=self._relay_provider,
            )
            self.session.post(u, timeout=10)
            self._last_relay_renew = time.time()
            log.info("[%s] relay baslatildi: %s", self.city, self._relay_dvr)
        except Exception as e:
            log.warning("[%s] relay baslatma hatasi: %s", self.city, e)

    def _renew_relay(self):
        try:
            u = self.cfg["relay_start_url"].format(
                dvr=self._relay_dvr, provider=self._relay_provider,
            )
            self.session.post(u, timeout=8)
            self._last_relay_renew = time.time()
        except Exception:
            pass


def follow_master(url: str, session: requests.Session) -> str:
    """2 katmanli HLS (IBB): master -> chunklist."""
    try:
        r = session.get(url, timeout=8)
        if r.status_code != 200 or "#EXT-X-STREAM-INF" not in r.text:
            return url
        lines = r.text.splitlines()
        for i, line in enumerate(lines):
            if line.startswith("#EXT-X-STREAM-INF"):
                if i + 1 < len(lines):
                    sub = lines[i + 1].strip()
                    if sub and not sub.startswith("#"):
                        return sub if sub.startswith("http") else (
                            url.rsplit("/", 1)[0] + "/" + sub
                        )
    except Exception:
        pass
    return url


_last_frame_bytes: Optional[bytes] = None
_last_frame_shape: Optional[tuple] = None


def analyze_segment(seg_path: Path, ffmpeg: str) -> tuple[float, float]:
    """Segment'ten 1 kare cek, brightness + motion hesapla."""
    global _last_frame_bytes, _last_frame_shape
    import subprocess
    import tempfile
    try:
        from PIL import Image
        import numpy as np
    except ImportError:
        return 128.0, 0.0

    frame_path = None
    try:
        fd, frame_path = tempfile.mkstemp(suffix=".jpg")
        os.close(fd)
        subprocess.run(
            [ffmpeg, "-y", "-loglevel", "error", "-ss", "3",
             "-i", str(seg_path), "-frames:v", "1",
             "-vf", "scale=160:90", frame_path],
            capture_output=True, timeout=10, start_new_session=True,
        )
        if not Path(frame_path).exists():
            return 128.0, 0.0
        img = Image.open(frame_path).convert("L")
        arr = np.array(img)
        brightness = float(arr.mean())
        motion = 0.0
        if _last_frame_bytes is not None and _last_frame_shape == arr.shape:
            try:
                prev = np.frombuffer(_last_frame_bytes, dtype=np.uint8).reshape(arr.shape)
                motion = float(np.abs(arr.astype(int) - prev.astype(int)).mean())
            except Exception:
                pass
        _last_frame_bytes = arr.tobytes()
        _last_frame_shape = arr.shape
        return brightness, motion
    except Exception:
        return 128.0, 0.0
    finally:
        if frame_path and Path(frame_path).exists():
            try:
                os.unlink(frame_path)
            except Exception:
                pass


def run_ingest(city: str, cfg: dict, shutdown):
    """Sonsuz dongu: kamera sec -> playlist poll -> yeni segment indir."""
    out_dir = common.SEGMENTS_DIR / city
    out_dir.mkdir(parents=True, exist_ok=True)

    resolver = CameraResolver(city, cfg)
    ttl = cfg.get("ttl_seconds", 1800)
    seg_dur_target = cfg.get("segment_seconds", 6)
    max_keep = cfg.get("max_segments_per_city", 100)
    backoff = cfg.get("retry_backoff", [2, 5, 10, 20, 30])

    seen_urls: set[str] = set()
    seq_counter = 0
    backoff_idx = 0
    last_progress = time.time()
    ffmpeg = common.ffmpeg_path()

    common.start_heartbeat("ingest-" + city, interval=10)
    log.info("[%s] ingest basliyor (out=%s)", city, out_dir)

    while not shutdown.stopped.is_set():
        cam = resolver.current()
        if not cam:
            log.warning("[%s] kamera bulunamadi, %ss bekle", city, backoff[backoff_idx])
            shutdown.wait(backoff[backoff_idx])
            backoff_idx = min(backoff_idx + 1, len(backoff) - 1)
            continue

        stream_url = follow_master(cam["stream_url"], resolver.session)
        base = stream_url.rsplit("/", 1)[0] + "/"

        try:
            r = resolver.session.get(stream_url, timeout=8)
        except Exception as e:
            log.warning("[%s] playlist hatasi: %s", city, e)
            shutdown.wait(backoff[backoff_idx])
            backoff_idx = min(backoff_idx + 1, len(backoff) - 1)
            continue

        if r.status_code != 200:
            log.warning("[%s] HTTP %s on %s", city, r.status_code, stream_url[-40:])
            shutdown.wait(backoff[backoff_idx])
            backoff_idx = min(backoff_idx + 1, len(backoff) - 1)
            resolver._current = None
            continue

        backoff_idx = 0
        lines = r.text.splitlines()
        i = 0
        while i < len(lines):
            ln = lines[i].strip()
            if ln.startswith("#EXTINF:"):
                try:
                    dur = float(ln.split(":")[1].rstrip(",").split(",")[0])
                except Exception:
                    dur = float(seg_dur_target)
                if i + 1 < len(lines):
                    seg_ref = lines[i + 1].strip()
                    if seg_ref and not seg_ref.startswith("#"):
                        seg_url = seg_ref if seg_ref.startswith("http") else base + seg_ref
                        if seg_url not in seen_urls:
                            seen_urls.add(seg_url)
                            if shutdown.stopped.is_set():
                                return
                            ok = _fetch_and_save(
                                seg_url, out_dir, city, cam, dur,
                                seq_counter, ttl, resolver.session, ffmpeg,
                            )
                            if ok:
                                seq_counter += 1
                                last_progress = time.time()
                i += 2
            else:
                i += 1

        _trim_old_segments(out_dir, max_keep)

        if time.time() - last_progress > 30:
            log.warning("[%s] 30s segment yok -> kamera degistiriliyor", city)
            resolver._current = None
            last_progress = time.time()

        if len(seen_urls) > 500:
            seen_urls = set(list(seen_urls)[-200:])

        shutdown.wait(1.5)

    log.info("[%s] ingest kapaniyor", city)


def _fetch_and_save(url, out_dir, city, cam, dur, seq, ttl, session, ffmpeg):
    """Segment indir -> diske yaz -> DB'ye ekle."""
    try:
        sr = session.get(url, timeout=12, stream=True)
        if sr.status_code != 200:
            return False
        seg_id = "%s_%d_%06d" % (city, int(time.time()), seq)
        seg_path = out_dir / (seg_id + ".ts")
        size = 0
        with open(seg_path, "wb") as f:
            for chunk in sr.iter_content(65536):
                f.write(chunk)
                size += len(chunk)
        if size < 1000:
            seg_path.unlink(missing_ok=True)
            return False

        brightness, motion = analyze_segment(seg_path, ffmpeg)

        db.add_segment(
            seg_id=seg_id, city=city, path=str(seg_path),
            start_ts=int(time.time()),
            duration_ms=int(dur * 1000),
            size_bytes=size,
            brightness=brightness, motion=motion,
            plate=cam.get("plate", ""),
            vehicle_type=cam.get("vehicle_type", ""),
            ttl_seconds=ttl,
        )
        return True
    except Exception as e:
        log.warning("[%s] segment indirme: %s", city, e)
        return False


def _trim_old_segments(out_dir: Path, max_keep: int):
    """Halka tampon: en yeni max_keep disindakileri sil."""
    try:
        segs = sorted(out_dir.glob("*.ts"), key=lambda p: p.stat().st_mtime,
                      reverse=True)
        for old in segs[max_keep:]:
            try:
                old.unlink(missing_ok=True)
            except Exception:
                pass
    except Exception:
        pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--city", required=True,
                        choices=["ankara", "istanbul", "corum", "konya"])
    args = parser.parse_args()

    cfg = common.load_config()
    city_cfg = cfg["ingest"]["cameras"].get(args.city)
    if not city_cfg:
        log.error("Sehir config'i yok: %s", args.city)
        sys.exit(1)

    merged = dict(cfg["ingest"])
    merged.update(city_cfg)

    shutdown = common.GracefulShutdown()
    try:
        run_ingest(args.city, merged, shutdown)
    except KeyboardInterrupt:
        pass
    log.info("Cikis.")


if __name__ == "__main__":
    main()
