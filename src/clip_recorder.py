"""Captures HLS clips from Ankara bus cameras."""
import os
import subprocess
import shutil
import sys
import tempfile
import time
import requests
from datetime import datetime
from pathlib import Path
from src.ai_filter import is_interesting, best_frame

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
        """Relay'i başlat ve m3u8 erişilebilir olana kadar bekle."""
        dvr = vehicle.get("dvr_serial_number", "")
        provider = vehicle.get("source", "ego")
        if not dvr:
            return False
        try:
            url = RELAY_START_URL.format(dvr=dvr, provider=provider)
            self._session.post(url, timeout=10)
            # m3u8 hazır olana kadar bekle (max 15 saniye)
            stream_url = vehicle["stream_url"]
            for _ in range(5):
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
        deadline = time.time() + target_duration + 60  # max bekleme

        while total_duration < target_duration and time.time() < deadline:
            try:
                r = self._session.get(stream_url, timeout=8)
                if r.status_code != 200:
                    time.sleep(2)
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
                pass

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
                                        timeout=self.duration + 120, **_NW)

                if result.returncode == 0 and out_path.exists():
                    if out_path.stat().st_size > 100_000:
                        if self._is_frozen(str(out_path)):
                            print(f"  Donuk video, atlanıyor: {plate}")
                            out_path.unlink(missing_ok=True)
                            return None
                        if not is_interesting(str(out_path), self.ffmpeg, self.duration):
                            print(f"  AI: ilgisiz sahne, atlanıyor: {plate}")
                            out_path.unlink(missing_ok=True)
                            return None
                        best_frame(str(out_path), self.ffmpeg, self.duration)
                        return str(out_path)
                return None

        except subprocess.TimeoutExpired:
            print(f"  Timeout: {plate}")
            out_path.unlink(missing_ok=True)
            return None
        except Exception as e:
            print(f"  Hata ({plate}): {e}")
            return None

    def _is_frozen(self, video_path: str) -> bool:
        """İki kontrol: düşük bitrate VEYA aynı kareler → donuk."""
        return self._check_bitrate(video_path) or self._check_frames(video_path)

    def _is_boring(self, video_path: str) -> bool:
        """Sadece yol/tavan gösteren tekdüze görüntüleri filtrele."""
        import tempfile, os
        try:
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
                tmp = f.name
            cmd = [self.ffmpeg, "-y", "-ss", str(self.duration // 2),
                   "-i", video_path, "-frames:v", "1", tmp]
            subprocess.run(cmd, capture_output=True, timeout=10, **_NW)
            if not Path(tmp).exists():
                return False
            png_kb = Path(tmp).stat().st_size / 1024
            os.unlink(tmp)
            return png_kb < 300
        except Exception:
            return False

    def _is_blurry(self, video_path: str) -> bool:
        """Kenar tespiti uygula — kenar az ise lens bulanık/yağmurlu."""
        import tempfile, os
        try:
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
                tmp = f.name
            cmd = [
                self.ffmpeg, "-y", "-ss", str(self.duration // 2),
                "-i", video_path,
                "-frames:v", "1",
                "-vf", "edgedetect=low=0.05:high=0.2",
                tmp
            ]
            subprocess.run(cmd, capture_output=True, timeout=10, **_NW)
            if not Path(tmp).exists():
                return False
            edge_kb = Path(tmp).stat().st_size / 1024
            os.unlink(tmp)
            return edge_kb < 25
        except Exception:
            return False

    def _check_bitrate(self, video_path: str) -> bool:
        """Saniye başına 60KB altı = donuk."""
        try:
            size_kb = Path(video_path).stat().st_size / 1024
            return (size_kb / self.duration) < 60
        except Exception:
            return False

    def _check_frames(self, video_path: str) -> bool:
        """5 farklı saniyeden kare çek, neredeyse hepsi aynıysa donuk."""
        import hashlib, tempfile, os
        try:
            hashes = []
            step = max(1, self.duration // 6)
            for t in range(step, self.duration, step):
                with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
                    tmp = f.name
                try:
                    cmd = [self.ffmpeg, "-y", "-ss", str(t), "-i", video_path,
                           "-frames:v", "1", "-q:v", "5", tmp]
                    subprocess.run(cmd, capture_output=True, timeout=10, **_NW)
                    if Path(tmp).exists():
                        hashes.append(hashlib.md5(Path(tmp).read_bytes()).hexdigest())
                finally:
                    try:
                        os.unlink(tmp)
                    except Exception:
                        pass
            if len(hashes) < 3:
                return False
            most_common = max(set(hashes), key=hashes.count)
            return hashes.count(most_common) >= len(hashes) - 1
        except Exception:
            return False
