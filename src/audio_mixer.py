"""Ambient ses + TTS anons + hava durumu overlay ekler."""
import subprocess
import shutil
import sys
import tempfile
import os
from pathlib import Path

_NW = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}

FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


class AudioMixer:
    def __init__(self, config: dict):
        _ff = config.get("ffmpeg_path") or ""
        if _ff and not Path(_ff).exists():
            _ff = ""
        self.ffmpeg = _ff or shutil.which("ffmpeg") or "ffmpeg"

    def _generate_tts(self, text: str, out_mp3: str) -> bool:
        """Microsoft Edge neural TTS ile Türkçe ses üret (30 sn timeout)."""
        try:
            import asyncio, edge_tts
            async def _run():
                tts = edge_tts.Communicate(text, voice="tr-TR-EmelNeural", rate="-10%")
                await asyncio.wait_for(tts.save(out_mp3), timeout=30)
            asyncio.run(_run())
            return Path(out_mp3).exists() and Path(out_mp3).stat().st_size > 1000
        except Exception:
            return False

    def _weather_drawtext(self, weather: dict, city: str) -> str:
        """Sağ üst köşe için FFmpeg drawtext filtresi üret.

        Örnek çıktı: Türkiye  |  Açık  22C
        Türkçe karakterler GERÇEK haliyle yazılır — DejaVu Sans Bold tüm Türkçe
        gliflerini (ç ğ ı ş İ ö ü) render eder, ASCII'ye düşürmeye gerek YOK.
        Emoji kullanmıyoruz — FFmpeg font desteği kısıtlı.
        """
        if not weather:
            return ""
        temp = weather["temp"]
        cond = weather["condition"]          # "az bulutlu", "açık" vb.
        city_short = city.split()[0]         # "Çorum Merkez" → "Çorum"

        # Türkçe AYNEN korunur. Sadece drawtext'i bozabilecek özel karakterleri
        # escape'liyoruz: ters bölü, iki nokta (filtre ayıracı) ve tek tırnak.
        text = f"{city_short}  |  {cond}  {temp}C"
        text = (text.replace("\\", "\\\\")
                    .replace(":", "\\:")
                    .replace("'", "’"))   # ' → tipografik ’ (filtre güvenli)
        font = FONT_PATH if Path(FONT_PATH).exists() else "DejaVuSans-Bold"

        return (
            f"drawtext=text='{text}':"
            f"fontfile={font}:"
            f"fontcolor=white:fontsize=26:"
            f"x=w-tw-18:y=18:"
            f"box=1:boxcolor=black@0.45:boxborderw=8"
        )

    def add_audio(self, video_path: str, metadata: dict, location: str,
                  weather: dict = None, duration: int = 180) -> str:
        """Videoya ambient + TTS sesi ve isteğe bağlı hava durumu overlay'i ekle."""
        city = metadata.get("city", location.split(",")[-1].strip())

        # TTS metni. Producer tam metni (tarih + hava + CTA, DOĞRU sırada)
        # tts_text içinde verdiyse OLDUĞU GİBİ kullan — hava durumunu TEKRAR
        # EKLEME. (Önceki bug: hava hem _build_metadata'da hem burada ekleniyordu;
        # ses "...abone olun!" CTA'sından SONRA havayı ikinci kez okuyordu.)
        # Hava'yı yalnızca tts_text YOKSA (fallback: başlıktan türetme) ekle.
        if metadata.get("tts_text"):
            tts_text = metadata["tts_text"]
        else:
            title = metadata.get("title", "")
            tts_text = title.replace("#Shorts", "").replace(" - ", ". ").strip()
            if weather:
                tts_text += f" Hava {weather['condition']}, {weather['temp']} derece."
                if weather.get("humidity"):
                    tts_text += f" Nem yüzde {weather['humidity']}."
                if weather.get("wind_kmh"):
                    tts_text += f" Rüzgar saatte {weather['wind_kmh']} kilometre."

        video    = Path(video_path)
        out_path = video.parent / (video.stem + "_audio.mp4")
        drawtext = self._weather_drawtext(weather, city) if weather else ""

        with tempfile.TemporaryDirectory() as tmp_dir:
            tts_wav = os.path.join(tmp_dir, "tts.mp3")
            tts_ok  = self._generate_tts(tts_text, tts_wav)

            # Video ses kanalı var mı?
            probe     = subprocess.run([self.ffmpeg, "-i", str(video)],
                                       capture_output=True, text=True, **_NW)
            has_audio = "Audio" in probe.stderr

            # Video filtresi — overlay varsa yeniden encode, yoksa copy
            if drawtext:
                vcodec_args = ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "26"]
                vf_arg      = ["-vf", drawtext]
            else:
                vcodec_args = ["-c:v", "copy"]
                vf_arg      = []

            if tts_ok:
                if has_audio:
                    af = "[0:a]volume=0.9[orig];[1:a]volume=1.6,adelay=500|500[tts];[orig][tts]amix=inputs=2:duration=first[out]"
                else:
                    af = "anoisesrc=c=pink:r=44100,volume=0.02[amb];[1:a]volume=1.6,adelay=500|500[tts];[amb][tts]amix=inputs=2:duration=longest[out]"
                cmd = (
                    [self.ffmpeg, "-y", "-i", str(video), "-i", tts_wav]
                    + ["-filter_complex", af]
                    + vf_arg
                    + ["-map", "0:v", "-map", "[out]"]
                    + vcodec_args
                    + ["-c:a", "aac", "-shortest", str(out_path)]
                )
            else:
                if has_audio:
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
            result = subprocess.run(cmd, capture_output=True, timeout=timeout_sec, **_NW)
            if result.returncode == 0 and out_path.exists():
                if video.exists():
                    video.unlink()
                shutil.move(str(out_path), str(video))
                return str(video)

        return str(video)
