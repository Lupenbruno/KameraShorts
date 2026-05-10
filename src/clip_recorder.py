"""Captures HLS clips from Ankara bus cameras."""
import subprocess
import shutil
import sys
import time
import requests
from datetime import datetime
from pathlib import Path

_NW = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}

RELAY_START_URL = "https://seyret.ankara.bel.tr/api/relay/start/{dvr}?provider={provider}"


class ClipRecorder:
    def __init__(self, config: dict):
        self.duration = config["schedule"]["clip_duration"]
        self.clips_dir = Path(config["paths"]["clips_dir"])
        self.clips_dir.mkdir(parents=True, exist_ok=True)
        self.ffmpeg = config.get("ffmpeg_path") or shutil.which("ffmpeg") or "ffmpeg"
        self._session = requests.Session()
        self._session.headers["User-Agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        self._session.headers["Referer"] = "https://seyret.ankara.bel.tr/"

    def _start_relay(self, vehicle: dict) -> bool:
        """Relay'i başlat, stream hazır olana kadar bekle."""
        dvr = vehicle.get("dvr_serial_number", "")
        provider = vehicle.get("source", "ego")
        if not dvr:
            return False
        try:
            url = RELAY_START_URL.format(dvr=dvr, provider=provider)
            self._session.post(url, timeout=10)
            time.sleep(10)  # relay başlaması için bekle
            # stream erişilebilir mi kontrol et
            r = self._session.head(vehicle["stream_url"], timeout=8, allow_redirects=True)
            return r.status_code == 200
        except Exception:
            return False

    def record(self, vehicle: dict, capture_time: datetime) -> str | None:
        device_id = vehicle["device_id"]
        plate = vehicle.get("license_plate", device_id).replace(" ", "_")
        ts = capture_time.strftime("%Y%m%d_%H%M")
        out_path = self.clips_dir / f"{plate}_{ts}.mp4"

        stream_url = vehicle["stream_url"]

        if not self._start_relay(vehicle):
            return None

        cmd = [
            self.ffmpeg, "-y",
            "-i", stream_url,
            "-t", str(self.duration),
            "-c:v", "libx264",
            "-preset", "fast",
            "-crf", "23",
            "-c:a", "aac",
            "-movflags", "+faststart",
            # Dikey 9:16 format — otobüs kamerasına göre crop
            "-vf", "scale=1080:1920:force_original_aspect_ratio=increase,"
                   "crop=1080:1920",
            str(out_path)
        ]

        try:
            result = subprocess.run(cmd, capture_output=True, timeout=90, **_NW)
            if result.returncode == 0 and out_path.exists():
                if out_path.stat().st_size > 100_000:  # 100KB minimum
                    if self._is_frozen(str(out_path)):
                        print(f"  Donuk video, atlanıyor: {plate}")
                        out_path.unlink(missing_ok=True)
                        return None
                    if self._is_boring(str(out_path)):
                        print(f"  Tekdüze görüntü (yol/tavan), atlanıyor: {plate}")
                        out_path.unlink(missing_ok=True)
                        return None
                    if self._is_blurry(str(out_path)):
                        print(f"  Bulanık/yağmurlu lens, atlanıyor: {plate}")
                        out_path.unlink(missing_ok=True)
                        return None
                    return str(out_path)
            return None
        except subprocess.TimeoutExpired:
            print(f"  Timeout: {plate}")
            return None
        except Exception as e:
            print(f"  FFmpeg hatasi ({plate}): {e}")
            return None

    def _is_frozen(self, video_path: str) -> bool:
        """İki kontrol: düşük bitrate VEYA aynı kareler → donuk."""
        return self._check_bitrate(video_path) or self._check_frames(video_path)

    def _is_boring(self, video_path: str) -> bool:
        """Sadece yol/tavan gösteren tekdüze görüntüleri filtrele.
        PNG sıkıştırması düşükse görüntü çok tekdüze demektir."""
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
            # 1080x1920 gerçek sahne için PNG genellikle 400KB+
            # Sadece yol/tavan görüntüsü ~266KB, normal sahne ~500KB+
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
            # Keskin görüntüde kenar PNG'si büyük olur (çok kenar = çok detay)
            # Bulanık/yağmurlu lens'te kenar neredeyse yok → çok küçük PNG
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
