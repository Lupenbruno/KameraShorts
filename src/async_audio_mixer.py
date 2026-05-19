"""
AudioMixer'ın async versiyonu.

Farklar:
  - FFmpeg subprocess.run → asyncio.create_subprocess_exec (run_ffmpeg)
  - edge_tts zaten async, doğrudan await
  - timeout sabit 600s değil, duration × 3 dinamik
"""
import asyncio
import logging
import os
import shutil
import tempfile
from pathlib import Path

from src.async_subprocess import run_ffmpeg

log = logging.getLogger("kamerashorts")

FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


class AsyncAudioMixer:
    def __init__(self, config: dict):
        _ff = config.get("ffmpeg_path") or ""
        if _ff and not Path(_ff).exists():
            _ff = ""
        self.ffmpeg = _ff or shutil.which("ffmpeg") or "ffmpeg"

    # ── TTS ───────────────────────────────────────────────────────────────
    async def _generate_tts(self, text: str, out_mp3: str) -> bool:
        """Microsoft Edge Neural TTS — zaten async, 30s timeout."""
        try:
            import edge_tts
            tts = edge_tts.Communicate(text, voice="tr-TR-EmelNeural", rate="-10%")
            await asyncio.wait_for(tts.save(out_mp3), timeout=30)
            return Path(out_mp3).exists() and Path(out_mp3).stat().st_size > 1000
        except Exception as e:
            log.warning(f"TTS hatası: {e}")
            return False

    # ── Hava durumu overlay ───────────────────────────────────────────────
    def _weather_drawtext(self, weather: dict, city: str) -> str:
        if not weather:
            return ""
        temp = weather["temp"]
        cond = weather["condition"]
        city_short = city.split()[0]
        tr_map = str.maketrans("çğıöşüÇĞİÖŞÜ", "cgiosucgiosu")
        cond_safe = cond.translate(tr_map)
        city_safe = city_short.translate(tr_map)
        text = f"{city_safe}  |  {cond_safe}  {temp}C"
        font = FONT_PATH if Path(FONT_PATH).exists() else "DejaVuSans-Bold"
        return (
            f"drawtext=text='{text}':"
            f"fontfile={font}:"
            f"fontcolor=white:fontsize=26:"
            f"x=w-tw-18:y=18:"
            f"box=1:boxcolor=black@0.45:boxborderw=8"
        )

    # ── Ana fonksiyon ─────────────────────────────────────────────────────
    async def add_audio(
        self,
        video_path: str,
        metadata: dict,
        location: str,
        weather: dict = None,
        duration: int = 180,
    ) -> str:
        """Videoya TTS + ambient ses + hava durumu overlay ekle (async)."""
        city = metadata.get("city", location.split(",")[-1].strip())

        # TTS metni
        tts_text = (
            metadata.get("tts_text")
            or metadata.get("title", "").replace("#Shorts", "").replace(" - ", ". ").strip()
        )
        if weather:
            tts_text += f" Hava {weather['condition']}, {weather['temp']} derece."
            if weather.get("humidity"):
                tts_text += f" Nem yüzde {weather['humidity']}."
            if weather.get("wind_kmh"):
                tts_text += f" Rüzgar saatte {weather['wind_kmh']} kilometre."

        video = Path(video_path)
        out_path = video.parent / (video.stem + "_audio.mp4")
        drawtext = self._weather_drawtext(weather, city) if weather else ""

        with tempfile.TemporaryDirectory() as tmp_dir:
            tts_mp3 = os.path.join(tmp_dir, "tts.mp3")

            # TTS ve probe paralel çalıştır
            tts_ok, (_, _, probe_stderr) = await asyncio.gather(
                self._generate_tts(tts_text, tts_mp3),
                run_ffmpeg([self.ffmpeg, "-i", str(video)], timeout=10),
                return_exceptions=False,
            )
            has_audio = b"Audio" in probe_stderr

            # Video filtre / codec seçimi
            if drawtext:
                vcodec_args = ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "26"]
                vf_arg = ["-vf", drawtext]
            else:
                vcodec_args = ["-c:v", "copy"]
                vf_arg = []

            if tts_ok:
                if has_audio:
                    af = (
                        "[0:a]volume=0.9[orig];"
                        "[1:a]volume=1.6,adelay=500|500[tts];"
                        "[orig][tts]amix=inputs=2:duration=first[out]"
                    )
                else:
                    af = (
                        "anoisesrc=c=pink:r=44100,volume=0.02[amb];"
                        "[1:a]volume=1.6,adelay=500|500[tts];"
                        "[amb][tts]amix=inputs=2:duration=longest[out]"
                    )
                cmd = (
                    [self.ffmpeg, "-y", "-i", str(video), "-i", tts_mp3]
                    + ["-filter_complex", af]
                    + vf_arg
                    + ["-map", "0:v", "-map", "[out]"]
                    + vcodec_args
                    + ["-c:a", "aac", "-shortest", str(out_path)]
                )
            elif has_audio:
                cmd = (
                    [self.ffmpeg, "-y", "-i", str(video)]
                    + vf_arg
                    + ["-map", "0:v", "-map", "0:a"]
                    + vcodec_args
                    + ["-c:a", "aac", str(out_path)]
                )
            else:
                cmd = (
                    [self.ffmpeg, "-y", "-i", str(video)]
                    + vf_arg
                    + ["-map", "0:v", "-an"]
                    + vcodec_args
                    + [str(out_path)]
                )

            timeout_sec = max(duration * 3, 120)
            try:
                returncode, _, _ = await run_ffmpeg(cmd, timeout=timeout_sec)
            except asyncio.TimeoutError:
                log.error(f"AudioMixer timeout ({timeout_sec}s): {video.name}")
                return str(video)

            if returncode == 0 and out_path.exists():
                if video.exists():
                    video.unlink()
                shutil.move(str(out_path), str(video))
                return str(video)

            log.warning(f"AudioMixer FFmpeg başarısız (rc={returncode}), orijinal kullanılıyor")
            return str(video)
