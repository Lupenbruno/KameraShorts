"""Ambient ses + TTS anons ekler."""
import subprocess
import shutil
import tempfile
import os
from pathlib import Path


class AudioMixer:
    def __init__(self, config: dict):
        self.ffmpeg = config.get("ffmpeg_path") or shutil.which("ffmpeg") or "ffmpeg"

    def _generate_tts(self, text: str, out_mp3: str) -> bool:
        """Microsoft Edge neural TTS ile Türkçe ses üret."""
        try:
            import asyncio, edge_tts
            async def _run():
                tts = edge_tts.Communicate(text, voice="tr-TR-EmelNeural", rate="-10%")
                await tts.save(out_mp3)
            asyncio.run(_run())
            return Path(out_mp3).exists() and Path(out_mp3).stat().st_size > 1000
        except Exception:
            return False

    def add_audio(self, video_path: str, metadata: dict, location: str) -> str:
        """Videoya ambient + TTS sesi ekle, yeni dosya döndür."""
        # tts_text doğrudan verilmişse kullan, yoksa title'dan türet
        if metadata.get("tts_text"):
            tts_text = metadata["tts_text"]
        else:
            title = metadata.get("title", "")
            tts_text = title.replace("#Shorts", "").replace(" - ", ". ").strip()

        video = Path(video_path)
        out_path = video.parent / (video.stem + "_audio.mp4")

        with tempfile.TemporaryDirectory() as tmp_dir:
            tts_wav = os.path.join(tmp_dir, "tts.mp3")
            tts_ok = self._generate_tts(tts_text, tts_wav)

            # Video ses kanalı var mı kontrol et
            probe = subprocess.run([self.ffmpeg, "-i", str(video)], capture_output=True, text=True)
            has_audio = "Audio" in probe.stderr

            if tts_ok:
                if has_audio:
                    # Orijinal ses + TTS, gürültü yok
                    audio_filter = "[0:a]volume=0.9[orig];[1:a]volume=1.6,adelay=500|500[tts];[orig][tts]amix=inputs=2:duration=first[out]"
                else:
                    # TTS + çok kısık pembe gürültü arka plan (0.02 = neredeyse duyulmaz)
                    audio_filter = "anoisesrc=c=pink:r=44100,volume=0.02[amb];[1:a]volume=1.6,adelay=500|500[tts];[amb][tts]amix=inputs=2:duration=longest[out]"
                cmd = [
                    self.ffmpeg, "-y",
                    "-i", str(video), "-i", tts_wav,
                    "-filter_complex", audio_filter,
                    "-map", "0:v", "-map", "[out]",
                    "-c:v", "copy", "-c:a", "aac", "-shortest",
                    str(out_path)
                ]
            else:
                if has_audio:
                    # Sadece orijinal ses, gürültü yok
                    cmd = [self.ffmpeg, "-y", "-i", str(video),
                           "-map", "0:v", "-map", "0:a",
                           "-c:v", "copy", "-c:a", "aac", str(out_path)]
                else:
                    # Ses yok, sessiz video
                    cmd = [self.ffmpeg, "-y", "-i", str(video),
                           "-map", "0:v", "-an",
                           "-c:v", "copy", str(out_path)]

            result = subprocess.run(cmd, capture_output=True, timeout=300)
            if result.returncode == 0 and out_path.exists():
                if video.exists():
                    video.unlink()
                shutil.move(str(out_path), str(video))
                return str(video)

        return str(video)  # hata olursa orjinali döndür
