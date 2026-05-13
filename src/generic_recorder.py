"""Generic HLS kamera kaydedici — herhangi bir şehir için kullanılır."""
import hashlib
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from src.ai_filter import analyze_clip

log = logging.getLogger("kamerashorts")
_NW = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}


class GenericRecorder:
    def __init__(self, clips_dir: str, duration: int, ffmpeg_path: str = None,
                 vertical: bool = False):
        self.duration = duration
        self.clips_dir = Path(clips_dir)
        self.clips_dir.mkdir(parents=True, exist_ok=True)
        _ff = ffmpeg_path or ""
        if _ff and not Path(_ff).exists():
            _ff = ""
        self.ffmpeg = _ff or shutil.which("ffmpeg") or "ffmpeg"
        self.vertical = vertical  # True → 1080x1920 Shorts, False → 1280x720 landscape

    def record(self, camera: dict, capture_time: datetime) -> str | None:
        cam_id = camera["id"]
        ts = capture_time.strftime("%Y%m%d_%H%M")
        out_path = self.clips_dir / f"{cam_id}_{ts}.mp4"
        stream_url = camera["stream_url"]

        if self.vertical:
            vf = "crop=ih*9/16:ih,scale=1080:1920"
        else:
            vf = ("scale=1280:720:force_original_aspect_ratio=decrease,"
                  "pad=1280:720:(ow-iw)/2:(oh-ih)/2")

        cmd = [
            self.ffmpeg, "-y",
            "-tls_verify", "0",      # SSL sertifika doğrulamasını atla (bazı şehirler)
            "-i", stream_url,
            "-t", str(self.duration),
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-crf", "26",
            "-c:a", "aac",
            "-movflags", "+faststart",
            "-vf", vf,
            str(out_path)
        ]

        try:
            result = subprocess.run(
                cmd, capture_output=True, timeout=self.duration * 5, **_NW
            )
            if result.returncode == 0 and out_path.exists():
                if out_path.stat().st_size > 500_000:
                    if self._is_frozen(str(out_path)):
                        log.warning(f"[{cam_id}] Donuk video, atlanıyor")
                        out_path.unlink(missing_ok=True)
                        return None
                    # Post-kayıt YOLO kontrolü — 5 kare ile tam analiz
                    score, dyn_min, _ = analyze_clip(str(out_path), self.ffmpeg, self.duration)
                    if score < dyn_min:
                        log.warning(f"[{cam_id}] YOLO elendi, atlanıyor")
                        out_path.unlink(missing_ok=True)
                        return None
                    return str(out_path)
            return None
        except subprocess.TimeoutExpired:
            log.warning(f"[{cam_id}] Timeout")
            out_path.unlink(missing_ok=True)
            return None
        except Exception as e:
            log.error(f"[{cam_id}] Kayıt hatası: {e}")
            return None

    def _is_frozen(self, video_path: str) -> bool:
        return self._check_bitrate(video_path) or self._check_frames(video_path)

    def _check_bitrate(self, video_path: str) -> bool:
        """Saniye başına 80KB altı = donuk."""
        try:
            size_kb = Path(video_path).stat().st_size / 1024
            return (size_kb / self.duration) < 80
        except Exception:
            return False

    def _check_frames(self, video_path: str) -> bool:
        """5 farkli saniyeden kare cek, neredeyse hepsi ayniysa donuk."""
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
