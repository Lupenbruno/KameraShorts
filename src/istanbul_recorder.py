"""İstanbul IBB turistik kameralarından 3 dakikalık landscape video kaydeder."""
import subprocess
import shutil
import sys
from datetime import datetime
from pathlib import Path

_NW = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}


class IstanbulRecorder:
    def __init__(self, config: dict):
        self.duration = config.get("istanbul", {}).get("clip_duration", 180)  # 3 dakika
        self.clips_dir = Path(config["paths"]["istanbul_clips_dir"])
        self.clips_dir.mkdir(parents=True, exist_ok=True)
        _ff = config.get("ffmpeg_path") or ""
        if _ff and not Path(_ff).exists():
            _ff = ""
        self.ffmpeg = _ff or shutil.which("ffmpeg") or "ffmpeg"

    def record(self, camera: dict, capture_time: datetime) -> str | None:
        cam_id = camera["id"]
        ts = capture_time.strftime("%Y%m%d_%H%M")
        out_path = self.clips_dir / f"{cam_id}_{ts}.mp4"
        stream_url = camera["stream_url"]

        # Landscape format, 1280x720 — turistik kamera görseli için yeterli
        cmd = [
            self.ffmpeg, "-y",
            "-tls_verify", "0",      # SSL sertifika doğrulamasını atla (IBB sertifikası)
            "-i", stream_url,
            "-t", str(self.duration),
            "-c:v", "libx264",
            "-preset", "fast",
            "-crf", "23",
            "-c:a", "aac",
            "-movflags", "+faststart",
            "-vf", "scale=1280:720:force_original_aspect_ratio=decrease,"
                   "pad=1280:720:(ow-iw)/2:(oh-ih)/2",
            str(out_path)
        ]

        try:
            result = subprocess.run(cmd, capture_output=True, timeout=self.duration + 90, **_NW)
            if result.returncode == 0 and out_path.exists():
                if out_path.stat().st_size > 500_000:  # 500KB minimum
                    if self._is_frozen(str(out_path)):
                        print(f"  [{cam_id}] Donuk video, atlanıyor")
                        out_path.unlink(missing_ok=True)
                        return None
                    return str(out_path)
            return None
        except subprocess.TimeoutExpired:
            print(f"  [{cam_id}] Timeout")
            out_path.unlink(missing_ok=True)
            return None
        except Exception as e:
            print(f"  [{cam_id}] Hata: {e}")
            return None

    def _is_frozen(self, video_path: str) -> bool:
        """Bitrate düşükse veya kareler aynıysa donuk."""
        return self._check_bitrate(video_path) or self._check_frames(video_path)

    def _check_bitrate(self, video_path: str) -> bool:
        """Saniye başına 80KB altı = donuk."""
        try:
            size_kb = Path(video_path).stat().st_size / 1024
            return (size_kb / self.duration) < 80
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
