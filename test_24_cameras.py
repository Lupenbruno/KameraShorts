"""24 İstanbul kamerasından 20 saniyelik test klipleri çek → Masaüstü."""
import subprocess
import warnings
import requests
from pathlib import Path
from datetime import datetime

FFMPEG = r"C:\Users\arsen\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-8.1-full_build\bin\ffmpeg.exe"
DESKTOP = Path(r"C:\Users\arsen\OneDrive\Masaüstü\istanbul_test")
DESKTOP.mkdir(exist_ok=True)

DURATION = 20  # saniye

BASE_URL = "https://livestream.ibb.gov.tr/cam_turistik/{slug}.stream/playlist.m3u8"

CAMERAS = [
    {"id": "anadoluhisari",  "name": "Anadolu Hisarı",    "slug": "b_anadoluhisari"},
    {"id": "beyazitkule1",   "name": "Beyazıt Kulesi 1",  "slug": "b_beyazitkule"},
    {"id": "beyazitkule2",   "name": "Beyazıt Kulesi 2",  "slug": "b_beyazitkule2new"},
    {"id": "beyazitmeydan",  "name": "Beyazıt Meydanı",   "slug": "b_beyazitmeydani"},
    {"id": "buyukcamlica",   "name": "Büyük Çamlıca",     "slug": "b_buyukcamlıca"},
    {"id": "dragos",         "name": "Dragos",             "slug": "b_dragos"},
    {"id": "eminonu",        "name": "Eminönü",            "slug": "b_eminonu"},
    {"id": "eyupsultan",     "name": "Eyüp Sultan",        "slug": "b_eyupsultan"},
    {"id": "hidivkasri",     "name": "Hidiv Kasrı",        "slug": "b_hidivkasri"},
    {"id": "kadikoy",        "name": "Kadıköy",            "slug": "b_kadikoy"},
    {"id": "kapalicarsi",    "name": "Kapalı Çarşı",       "slug": "b_kapalicarsi"},
    {"id": "kizkulesi",      "name": "Kız Kulesi",         "slug": "new_Kızkulesi"},
    {"id": "kucukcekmece",   "name": "Küçükçekmece",       "slug": "b_kucukcekmece"},
    {"id": "metrohan",       "name": "Metrohan",           "slug": "b_metrohan"},
    {"id": "miniaturk",      "name": "Miniatürk",          "slug": "b_miniatürk"},
    {"id": "misircarsisi",   "name": "Mısır Çarşısı",      "slug": "b_misircarsisi"},
    {"id": "ortakoy",        "name": "Ortaköy",            "slug": "b_ortakoy"},
    {"id": "pierreloti",     "name": "Pierre Loti",        "slug": "b_pierreloti"},
    {"id": "salacak",        "name": "Salacak",            "slug": "b_salacak"},
    {"id": "sarachane",      "name": "Saraçhane",          "slug": "b_sarachane"},
    {"id": "sultanahmet1",   "name": "Sultanahmet 1",      "slug": "b_sultanahmet"},
    {"id": "taksimmeydan",   "name": "Taksim Meydanı",     "slug": "b_taksim_meydan"},
    {"id": "ulusparki",      "name": "Ulus Parkı",         "slug": "b_ulusparki"},
    {"id": "uskudar",        "name": "Üsküdar",            "slug": "b_uskudar"},
]

for cam in CAMERAS:
    cam["stream_url"] = BASE_URL.format(slug=cam["slug"])

sess = requests.Session()
sess.headers["User-Agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"

ok, fail, skip = [], [], []
total = len(CAMERAS)

print(f"\n{'='*60}")
print(f"  İstanbul 24 Kamera Testi — {datetime.now().strftime('%H:%M:%S')}")
print(f"  Kayıt süresi: {DURATION}s | Hedef: {DESKTOP}")
print(f"{'='*60}\n")

for i, cam in enumerate(CAMERAS, 1):
    name = cam["name"]
    cam_id = cam["id"]
    url = cam["stream_url"]
    out = DESKTOP / f"{i:02d}_{cam_id}.mp4"

    print(f"[{i:02d}/{total}] {name} ... ", end="", flush=True)

    # Stream erişim kontrolü (hızlı HEAD)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            r = sess.head(url, timeout=6, allow_redirects=True, verify=False)
        if r.status_code != 200:
            print(f"ATLA (HTTP {r.status_code})")
            skip.append(name)
            continue
    except Exception as e:
        print(f"ATLA (baglanti hatasi)")
        skip.append(name)
        continue

    # FFmpeg ile kayıt
    cmd = [
        FFMPEG, "-y",
        "-i", url,
        "-t", str(DURATION),
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "26",
        "-c:a", "aac",
        "-vf", "scale=1280:720:force_original_aspect_ratio=decrease,"
               "pad=1280:720:(ow-iw)/2:(oh-ih)/2",
        str(out)
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, timeout=DURATION + 30)
        if result.returncode == 0 and out.exists() and out.stat().st_size > 200_000:
            size_kb = out.stat().st_size // 1024
            print(f"OK  ({size_kb} KB)")
            ok.append(name)
        else:
            print(f"BASARISIZ (boyut kucuk veya hata)")
            out.unlink(missing_ok=True)
            fail.append(name)
    except subprocess.TimeoutExpired:
        print(f"TIMEOUT")
        out.unlink(missing_ok=True)
        fail.append(name)
    except Exception as e:
        print(f"HATA: {e}")
        fail.append(name)

print(f"\n{'='*60}")
print(f"  SONUC: {len(ok)} OK  |  {len(fail)} BASARISIZ  |  {len(skip)} ATLANDI")
print(f"{'='*60}")
if ok:
    print(f"\n  Kaydedilenler ({len(ok)}):")
    for n in ok:
        print(f"    + {n}")
if fail:
    print(f"\n  Basarisiz ({len(fail)}):")
    for n in fail:
        print(f"    - {n}")
if skip:
    print(f"\n  Atlananlar ({len(skip)}):")
    for n in skip:
        print(f"    ~ {n}")
print(f"\n  Klasor: {DESKTOP}")
