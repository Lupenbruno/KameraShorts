"""Captures HLS clips from Ankara bus cameras."""
import logging
import os
import subprocess
import shutil
import sys
import tempfile
import time
import requests
from datetime import datetime
from pathlib import Path
from src.ai_filter import quick_check, analyze_clip

log = logging.getLogger("kamerashorts")

_NW = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}

RELAY_START_URL = "https://seyret.ankara.bel.tr/api/relay/start/{dvr}?provider={provider}"


class ClipRecorder:
    def __init__(self, config: dict):
        self.duration = config["schedule"]["clip_duration"]
        self.clips_dir = Path(config["paths"]["clips_dir"])
        self.clips_dir.mkdir(parents=True, exist_ok=True)
        _ff = config.get("ffmpeg_path") or ""
        if _ff and not Path(_ff).exists():
            _ff = ""
        self.ffmpeg = _ff or shutil.which("ffmpeg") or "ffmpeg"
        self._session = requests.Session()
        self._session.headers["User-Agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        self._session.headers["Referer"] = "https://seyret.ankara.bel.tr/"

    def _start_relay(self, vehicle: dict) -> bool:
        """Relay'i başlat ve m3u8 erişilebilir olana kadar bekle.

        Stream zaten canlıysa relay'i tetiklemez (resetlemekten kaçın).
        """
        stream_url = vehicle["stream_url"]
        dvr = vehicle.get("dvr_serial_number", "")
        provider = vehicle.get("source", "ego")

        # Önce stream'in zaten canlı olup olmadığını kontrol et
        try:
            r = self._session.get(stream_url, timeout=5)
            if r.status_code == 200 and "#EXTM3U" in r.text:
                return True  # Zaten canlı, relay'i tetiklemeye gerek yok
        except Exception:
            pass

        # Canlı değilse relay'i başlat
        if not dvr:
            return False
        try:
            url = RELAY_START_URL.format(dvr=dvr, provider=provider)
            self._session.post(url, timeout=10)
            # m3u8 hazır olana kadar bekle (max 30 saniye)
            for _ in range(10):
                time.sleep(3)
                try:
                    r = self._session.get(stream_url, timeout=5)
                    if r.status_code == 200 and "#EXTM3U" in r.text:
                        return True
                except Exception:
                    pass
            return False
        except Exception:
            return False

    def _download_segments(self, stream_url: str, target_duration: int,
                           tmp_dir: str) -> list[str]:
        """
        m3u8 playlist'ten segmentleri direkt indir.
        playlist refresh yavaş olduğu için her segmenti tek tek çekiyoruz.
        Yeterli süre toplanana kadar devam eder.
        """
        base = stream_url.rsplit("/", 1)[0] + "/"
        seen = set()
        files = []
        total_duration = 0.0
        deadline = time.time() + target_duration + 20  # max bekleme
        empty_loops = 0  # ust uste bos dongu sayaci

        while total_duration < target_duration and time.time() < deadline:
            if empty_loops >= 4:  # 4 ust uste bos dongu = kamera takili, cik
                break
            try:
                r = self._session.get(stream_url, timeout=8)
                if r.status_code != 200:
                    time.sleep(2)
                    empty_loops += 1
                    continue

                lines = r.text.strip().split("\n")
                # Segment süre ve URL'lerini parse et
                i = 0
                while i < len(lines):
                    line = lines[i].strip()
                    if line.startswith("#EXTINF:"):
                        try:
                            seg_dur = float(line.split(":")[1].split(",")[0])
                        except Exception:
                            seg_dur = 2.0
                        if i + 1 < len(lines):
                            seg_name = lines[i + 1].strip()
                            if seg_name and seg_name not in seen and not seg_name.startswith("#"):
                                seen.add(seg_name)
                                seg_url = seg_name if seg_name.startswith("http") else base + seg_name
                                out_file = os.path.join(tmp_dir, f"seg_{len(files):04d}.ts")
                                try:
                                    sr = self._session.get(seg_url, timeout=10)
                                    if sr.status_code == 200 and len(sr.content) > 1000:
                                        with open(out_file, "wb") as f:
                                            f.write(sr.content)
                                        files.append(out_file)
                                        total_duration += seg_dur
                                except Exception:
                                    pass
                        i += 2
                    else:
                        i += 1
            except Exception:
                empty_loops += 1
                time.sleep(1)
                continue

            # Yeni segment geldiyse sayaci sifirla, gelmediyse artir
            if len(files) > 0 and total_duration > 0:
                empty_loops = 0
            else:
                empty_loops += 1

            if total_duration < target_duration:
                time.sleep(1)  # yeni segment bekleme

        return files

    def record(self, vehicle: dict, capture_time: datetime) -> str | None:
        device_id = vehicle["device_id"]
        plate = vehicle.get("license_plate", device_id).replace(" ", "_")
        ts = capture_time.strftime("%Y%m%d_%H%M")
        out_path = self.clips_dir / f"{plate}_{ts}.mp4"

        stream_url = vehicle["stream_url"]

        if not self._start_relay(vehicle):
            return None

        # Relay açıkken 1 kare çek, YOLO ile ön kontrol
        if not quick_check(stream_url, self.ffmpeg):
            log.warning(f"[{plate}] Ön YOLO: zemin/damper/karanlık, atlanıyor")
            return None

        try:
            with tempfile.TemporaryDirectory() as tmp_dir:
                # Segmentleri direkt indir
                segments = self._download_segments(stream_url, self.duration, tmp_dir)

                if not segments:
                    return None

                # concat listesi oluştur
                concat_file = os.path.join(tmp_dir, "concat.txt")
                with open(concat_file, "w") as f:
                    for seg in segments:
                        f.write(f"file '{seg}'\n")

                # ffmpeg ile concat → encode
                cmd = [
                    self.ffmpeg, "-y",
                    "-f", "concat", "-safe", "0",
                    "-i", concat_file,
                    "-t", str(self.duration),
                    "-c:v", "libx264",
                    "-preset", "fast",
                    "-crf", "23",
                    "-c:a", "aac",
                    "-movflags", "+faststart",
                    "-vf", "scale=1080:1920:force_original_aspect_ratio=increase,"
                           "crop=1080:1920",
                    str(out_path)
                ]
                result = subprocess.run(cmd, capture_output=True,
                                        timeout=self.duration + 45, **_NW)

                if result.returncode == 0 and out_path.exists():
                    if out_path.stat().st_size > 100_000:
                        if self._is_frozen(str(out_path)):
                            log.warning(f"[{plate}] Donuk video, atlanıyor")
                            out_path.unlink(missing_ok=True)
                            return None
                        # Post-kayıt YOLO kontrolü — 5 kare ile tam analiz
                        score, dyn_min, _ = analyze_clip(str(out_path), self.ffmpeg, self.duration)
                        if score < dyn_min:
                            log.warning(f"[{plate}] YOLO post-kayıt elendi, atlanıyor")
                            out_path.unlink(missing_ok=True)
                            return None
                        return str(out_path)
                return None

        except subprocess.TimeoutExpired:
            log.warning(f"[{plate}] Timeout")
            out_path.unlink(missing_ok=True)
            return None
        except Exception as e:
            log.error(f"[{plate}] Kayıt hatası: {e}")
            return None

    def _is_frozen(self, video_path: str) -> bool:
        """İki kontrol: düşük bitrate VEYA aynı kareler → donuk."""
        return self._check_bitrate(video_path) or self._check_frames(video_path)

    def _check_bitrate(self, video_path: str) -> bool:
        """Saniye başına 60KB altı = donuk."""
        try:
            size_kb = Path(video_path).stat().st_size / 1024
            return (size_kb / self.duration) < 60
        except Exception:
            return False

    def _check_frames(self, video_path: str) -> bool:
        """5 farkli saniyeden kare cek, neredeyse hepsi ayniysa donuk."""
        import hashlib
        try:
            hashes = []
            step = max(1, self.duration // 6)
            with tempfile.TemporaryDirectory() as tmp_dir:
                for i, t in enumerate(range(step, self.duration, step)):
                    frame = os.path.join(tmp_dir, "fr{}.jpg".format(i))
                    cmd = [self.ffmpeg, "-y", "-ss", str(t), "-i", video_path,
                           "-frames:v", "1", "-q:v", "5", frame]
                    subprocess.run(cmd, capture_output=True, timeout=10, **_NW)
                    if Path(frame).exists():
                        hashes.append(hashlib.md5(Path(frame).read_bytes()).hexdigest())
            if len(hashes) < 3:
                return False
            most_common = max(set(hashes), key=hashes.count)
            return hashes.count(most_common) >= len(hashes) - 1
        except Exception:
            return False
