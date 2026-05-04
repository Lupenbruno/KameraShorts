"""İstanbul IBB turistik kameraları — tüm 24 kamera, döngüsel sıra."""
import json
import random
import warnings
from pathlib import Path

import requests

BASE_URL   = "https://livestream.ibb.gov.tr/cam_turistik/{slug}.stream/playlist.m3u8"
INDEX_FILE = Path("data/istanbul_cam_index.json")

# Sitedeki 24 kamera — tümü test edilmiş, HTTP 200 doğrulandı
CAMERAS = [
    {"id": "anadoluhisari",   "name": "Anadolu Hisarı",    "location": "Anadolu Hisarı, Beykoz, İstanbul",          "slug": "b_anadoluhisari"},
    {"id": "beyazitkule1",    "name": "Beyazıt Kulesi 1",   "location": "Beyazıt Kulesi, Fatih, İstanbul",           "slug": "b_beyazitkule"},
    {"id": "beyazitkule2",    "name": "Beyazıt Kulesi 2",   "location": "Beyazıt Kulesi, Fatih, İstanbul",           "slug": "b_beyazitkule2new"},
    {"id": "beyazitmeydan",   "name": "Beyazıt Meydanı",    "location": "Beyazıt Meydanı, Fatih, İstanbul",          "slug": "b_beyazitmeydani"},
    {"id": "buyukcamlica",    "name": "Büyük Çamlıca",      "location": "Büyük Çamlıca Tepesi, Üsküdar, İstanbul",   "slug": "b_buyukcamlıca"},
    {"id": "dragos",          "name": "Dragos",              "location": "Dragos Sahili, Kartal, İstanbul",            "slug": "b_dragos"},
    {"id": "eminonu",         "name": "Eminönü",             "location": "Eminönü Meydanı, Fatih, İstanbul",          "slug": "b_eminonu"},
    {"id": "eyupsultan",      "name": "Eyüp Sultan",         "location": "Eyüp Sultan Camii, Eyüp, İstanbul",         "slug": "b_eyupsultan"},
    {"id": "hidivkasri",      "name": "Hidiv Kasrı",         "location": "Hidiv Kasrı, Çubuklu, Beykoz, İstanbul",   "slug": "b_hidivkasri"},
    {"id": "kadikoy",         "name": "Kadıköy",             "location": "Kadıköy İskelesi, Kadıköy, İstanbul",       "slug": "b_kadikoy"},
    {"id": "kapalicarsi",     "name": "Kapalı Çarşı",        "location": "Kapalı Çarşı, Fatih, İstanbul",             "slug": "b_kapalicarsi"},
    {"id": "kizkulesi",       "name": "Kız Kulesi",          "location": "Kız Kulesi, Üsküdar, İstanbul",             "slug": "new_Kızkulesi"},
    {"id": "kucukcekmece",    "name": "Küçükçekmece",        "location": "Küçükçekmece Gölü, İstanbul",               "slug": "b_kucukcekmece"},
    {"id": "metrohan",        "name": "Metrohan",            "location": "Metrohan, Karaköy, İstanbul",               "slug": "b_metrohan"},
    {"id": "miniaturk",       "name": "Miniatürk",           "location": "Miniatürk Parkı, Eyüp, İstanbul",           "slug": "b_miniatürk"},
    {"id": "misircarsisi",    "name": "Mısır Çarşısı",       "location": "Mısır Çarşısı, Eminönü, İstanbul",          "slug": "b_misircarsisi"},
    {"id": "ortakoy",         "name": "Ortaköy",             "location": "Ortaköy Camii, Beşiktaş, İstanbul",         "slug": "b_ortakoy"},
    {"id": "pierreloti",      "name": "Pierre Loti",         "location": "Pierre Loti Tepesi, Eyüp, İstanbul",        "slug": "b_pierreloti"},
    {"id": "salacak",         "name": "Salacak",             "location": "Salacak Sahili, Üsküdar, İstanbul",         "slug": "b_salacak"},
    {"id": "sarachane",       "name": "Saraçhane",           "location": "Saraçhane Parkı, Fatih, İstanbul",          "slug": "b_sarachane"},
    {"id": "sultanahmet1",    "name": "Sultanahmet 1",       "location": "Sultanahmet Meydanı, Fatih, İstanbul",      "slug": "b_sultanahmet"},
    {"id": "taksimmeydan",    "name": "Taksim Meydanı",      "location": "Taksim Meydanı, Beyoğlu, İstanbul",         "slug": "b_taksim_meydan"},
    {"id": "ulusparki",       "name": "Ulus Parkı",          "location": "Ulus Parkı, Beşiktaş, İstanbul",            "slug": "b_ulusparki"},
    {"id": "uskudar",         "name": "Üsküdar",             "location": "Üsküdar İskelesi, Üsküdar, İstanbul",       "slug": "b_uskudar"},
]

# stream_url'i slug'dan üret
for _cam in CAMERAS:
    _cam["stream_url"] = BASE_URL.format(slug=_cam["slug"])


class IstanbulRegistry:
    def __init__(self):
        self._session = requests.Session()
        self._session.headers["User-Agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
        INDEX_FILE.parent.mkdir(parents=True, exist_ok=True)

    def get_all_cameras(self) -> list[dict]:
        return list(CAMERAS)

    def get_next_cameras(self, count: int = 1) -> list[dict]:
        """Sıradaki kameraları döndür, indexi ilerlet.
        24 kamera sırayla döner: her slot farklı bir kamera."""
        idx = self._read_index()
        result = []
        tried = 0
        while len(result) < count and tried < len(CAMERAS):
            cam = CAMERAS[idx % len(CAMERAS)]
            idx += 1
            tried += 1
            result.append(cam)
        self._write_index(idx)
        return result

    def get_random_cameras(self, count: int = 6) -> list[dict]:
        pool = list(CAMERAS)
        random.shuffle(pool)
        return pool[:min(count, len(pool))]

    def check_stream(self, camera: dict, timeout: int = 8) -> bool:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                r = self._session.head(
                    camera["stream_url"], timeout=timeout,
                    allow_redirects=True, verify=False
                )
            return r.status_code == 200
        except Exception:
            return False

    def _read_index(self) -> int:
        try:
            if INDEX_FILE.exists():
                return json.loads(INDEX_FILE.read_text())["index"]
        except Exception:
            pass
        return 0

    def _write_index(self, idx: int):
        INDEX_FILE.write_text(json.dumps({"index": idx % len(CAMERAS)}))
