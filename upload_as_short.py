"""Mevcut bir Istanbul klibini Shorts formatına çevirip YouTube'a yükler."""
import subprocess, shutil, yaml, sys
from pathlib import Path
from datetime import datetime

FFMPEG = r"C:\Users\arsen\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-8.1-full_build\bin\ffmpeg.exe"

# --- Ayarlar ---
INPUT  = Path("data/istanbul_clips/hidivkasri_20260504_2331.mp4")
OUTPUT = Path("data/istanbul_clips/hidivkasri_20260504_2331_short.mp4")
DURATION = 58  # saniye (60'tan az olmalı)

def convert_to_short():
    print(f"Donusturuluyor: {INPUT.name} -> {OUTPUT.name}")
    cmd = [
        FFMPEG, "-y",
        "-i", str(INPUT),
        "-t", str(DURATION),
        # Dikey 1080x1920 — merkezi crop
        "-vf", "crop=ih*9/16:ih,scale=1080:1920",
        "-c:v", "libx264", "-preset", "fast", "-crf", "23",
        "-c:a", "aac",
        "-movflags", "+faststart",
        str(OUTPUT)
    ]
    result = subprocess.run(cmd, capture_output=True, timeout=120)
    if result.returncode == 0 and OUTPUT.exists():
        print(f"OK: {OUTPUT.name} ({OUTPUT.stat().st_size//1024} KB)")
        return True
    print(f"HATA: {result.stderr.decode('utf-8','ignore')[-500:]}")
    return False

def upload():
    with open("config.yaml", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    sys.path.insert(0, ".")
    from src.youtube_uploader import YouTubeUploader

    # Istanbul config kullan
    cfg = dict(config)
    istanbul_youtube = config.get("istanbul_youtube") or config["youtube"]
    cfg["youtube"] = istanbul_youtube
    cfg["paths"] = dict(config["paths"])
    cfg["paths"]["queue_path"] = config["paths"].get("istanbul_queue_path", "data/queue/istanbul_upload_queue.json")

    uploader = YouTubeUploader(cfg)

    now = datetime.now()
    metadata = {
        "title": f"Hidiv Kasrı — İstanbul 🌉 #{now.strftime('%d/%m/%Y')} #Shorts",
        "description": (
            f"Hidiv Kasrı, Çubuklu, Beykoz, İstanbul\n"
            f"Canlı kamera görüntüsü — {now.strftime('%d/%m/%Y %H:%M')}\n\n"
            f"#İstanbul #HidivKasrı #Shorts #CanlıKamera #istanbul"
        ),
        "tags": ["İstanbul", "Hidiv Kasrı", "Shorts", "canlı kamera", "istanbul manzara", "ibb"],
        "category_id": "19",
        "privacy_status": "public",
    }

    print("YouTube'a yukleniyor...")
    # Kota sayacini bypass et — tek seferlik manuel yukleme
    uploader.daily_limit = 9999
    result = uploader.upload(str(OUTPUT), metadata)
    print(f"YUKLENDI: {result['url']}")

if __name__ == "__main__":
    if not INPUT.exists():
        print(f"HATA: Dosya bulunamadi: {INPUT}")
        sys.exit(1)

    if convert_to_short():
        upload()
