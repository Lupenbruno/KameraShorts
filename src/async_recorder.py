"""
İstanbul, Çorum, Konya için async HLS kaydedici.

Ankara: ClipRecorder mevcut sync kodu executor içinde çalışır (relay mantığı karmaşık).
Diğer şehirler: asyncio.create_subprocess_exec ile gerçek async FFmpeg.
"""
import asyncio
import logging
import shutil
import warnings
from datetime import datetime
from pathlib import Path

from src.async_subprocess import run_ffmpeg

log = logging.getLogger("kamerashorts")


class AsyncCityRecorder:
    """
    GenericRecorder + IstanbulRecorder'ın async versiyonu.

    Parametreler:
      use_yolo  — Çorum/Konya için True, İstanbul için False
      vertical  — Shorts formatı (1080x1920) için True
    """

    def __init__(
        self,
        clips_dir: str,
        duration: int,
        ffmpeg_path: str = None,
        vertical: bool = False,
        use_yolo: bool = True,
    ):
        self.duration = duration
        self.clips_dir = Path(clips_dir)
        self.clips_dir.mkdir(parents=True, exist_ok=True)
        _ff = ffmpeg_path or ""
        if _ff and not Path(_ff).exists():
            _ff = ""
        self.ffmpeg = _ff or shutil.which("ffmpeg") or "ffmpeg"
        self.vertical = vertical
        self.use_yolo = use_yolo

    # ── Stream kontrol ────────────────────────────────────────────────────
    async def check_stream(self, camera: dict) -> bool:
        """HTTP HEAD isteği — blocking requests → executor'da çalışır."""
        import requests

        def _check() -> bool:
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    r = requests.head(
                        camera["stream_url"],
                        timeout=8,
                        allow_redirects=True,
                        verify=False,
                    )
                return r.status_code == 200
            except Exception:
                return False

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _check)

    # ── Ana kayıt fonksiyonu ──────────────────────────────────────────────
    async def record(self, camera: dict, capture_time: datetime) -> str | None:
        cam_id = camera["id"]
        ts = capture_time.strftime("%Y%m%d_%H%M")
        out_path = self.clips_dir / f"{cam_id}_{ts}.mp4"
        stream_url = camera["stream_url"]

        if self.vertical:
            vf = "crop=ih*9/16:ih,scale=1080:1920"
        else:
            vf = (
                "scale=1280:720:force_original_aspect_ratio=decrease,"
                "pad=1280:720:(ow-iw)/2:(oh-ih)/2"
            )

        cmd = [
            self.ffmpeg, "-y",
            "-tls_verify", "0",
            "-i", stream_url,
            "-t", str(self.duration),
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-crf", "26",
            "-c:a", "aac",
            "-movflags", "+faststart",
            "-vf", vf,
            str(out_path),
        ]

        # Timeout: clip_duration × 3 + 30s (ağ gecikmesi marjı)
        timeout = self.duration * 3 + 30

        try:
            returncode, _, _ = await run_ffmpeg(cmd, timeout=timeout)

            if returncode == 0 and out_path.exists():
                size = out_path.stat().st_size
                if size > 500_000:
                    # Düşük bitrate → donuk görüntü
                    if self._check_bitrate(str(out_path)):
                        log.warning(f"[{cam_id}] Düşük bitrate (donuk), atlanıyor")
                        out_path.unlink(missing_ok=True)
                        return None

                    if self.use_yolo:
                        loop = asyncio.get_event_loop()
                        from src.ai_filter import analyze_clip
                        score, dyn_min, _ = await loop.run_in_executor(
                            None, analyze_clip, str(out_path), self.ffmpeg, self.duration
                        )
                        if score < dyn_min:
                            log.warning(
                                f"[{cam_id}] YOLO elendi "
                                f"(skor={score} < eşik={dyn_min}), atlanıyor"
                            )
                            out_path.unlink(missing_ok=True)
                            return None

                    return str(out_path)

            log.warning(f"[{cam_id}] FFmpeg returncode={returncode} veya dosya yok")
            out_path.unlink(missing_ok=True)
            return None

        except asyncio.TimeoutError:
            log.warning(f"[{cam_id}] Timeout ({timeout}s aşıldı)")
            out_path.unlink(missing_ok=True)
            return None
        except Exception as e:
            log.error(f"[{cam_id}] Kayıt hatası: {e}")
            out_path.unlink(missing_ok=True)
            return None

    def _check_bitrate(self, video_path: str) -> bool:
        """Saniye başına 80KB altı = donuk."""
        try:
            size_kb = Path(video_path).stat().st_size / 1024
            return (size_kb / self.duration) < 80
        except Exception:
            return False
