"""İstanbul IBB turistik kameraları — livestream.ibb.gov.tr
Stream URL'leri test edilmiş, sadece çalışan kameralar dahil edilmiştir.
"""
import random
import requests
import warnings

BASE_URL = "https://livestream.ibb.gov.tr/cam_turistik/{slug}.stream/playlist.m3u8"

# Tüm URL'ler test edilmiş, HTTP 200 dönen kameralar
CAMERAS = [
    {"id": "anadoluhisari",  "name": "Anadolu Hisarı",     "location": "Anadolu Hisarı, Beykoz, İstanbul",      "slug": "b_anadoluhisari"},
    {"id": "buyukcamlica",   "name": "Büyük Çamlıca",      "location": "Büyük Çamlıca, Üsküdar, İstanbul",      "slug": "b_buyukcamlıca"},
    {"id": "dragos",         "name": "Dragos",              "location": "Dragos, Kartal, İstanbul",               "slug": "b_dragos"},
    {"id": "eminonu",        "name": "Eminönü",             "location": "Eminönü, Fatih, İstanbul",               "slug": "b_eminonu"},
    {"id": "emirgan",        "name": "Emirgan",             "location": "Emirgan Korusu, Sarıyer, İstanbul",      "slug": "b_emirgan"},
    {"id": "eyupsultan",     "name": "Eyüp Sultan",         "location": "Eyüp Sultan Camii, Eyüp, İstanbul",     "slug": "b_eyupsultan"},
    {"id": "hidivkasri",     "name": "Hidiv Kasrı",         "location": "Hidiv Kasrı, Çubuklu, İstanbul",        "slug": "b_hidivkasri"},
    {"id": "kadikoy",        "name": "Kadıköy",             "location": "Kadıköy, İstanbul",                      "slug": "b_kadikoy"},
    {"id": "kapalicarsi",    "name": "Kapalı Çarşı",        "location": "Kapalı Çarşı, Fatih, İstanbul",         "slug": "b_kapalicarsi"},
    {"id": "kucukcekmece",   "name": "Küçükçekmece",        "location": "Küçükçekmece Gölü, İstanbul",           "slug": "b_kucukcekmece"},
    {"id": "metrohan",       "name": "Metrohan",            "location": "Metrohan, Karaköy, İstanbul",            "slug": "b_metrohan"},
    {"id": "miniaturk",      "name": "Miniatürk",           "location": "Miniatürk, Eyüp, İstanbul",              "slug": "b_miniatürk"},
    {"id": "misircarsisi",   "name": "Mısır Çarşısı",       "location": "Mısır Çarşısı, Eminönü, İstanbul",      "slug": "b_misircarsisi"},
    {"id": "ortakoy",        "name": "Ortaköy",             "location": "Ortaköy, Beşiktaş, İstanbul",           "slug": "b_ortakoy"},
    {"id": "pierreloti",     "name": "Pierre Loti",         "location": "Pierre Loti Tepesi, Eyüp, İstanbul",    "slug": "b_pierreloti"},
    {"id": "salacak",        "name": "Salacak",             "location": "Salacak, Üsküdar, İstanbul",             "slug": "b_salacak"},
    {"id": "sarachane",      "name": "Saraçhane",           "location": "Saraçhane, Fatih, İstanbul",             "slug": "b_sarachane"},
    {"id": "sultanahmet",    "name": "Sultanahmet",         "location": "Sultanahmet Meydanı, Fatih, İstanbul",  "slug": "b_sultanahmet"},
    {"id": "sultanahmet2",   "name": "Sultanahmet 2",       "location": "Sultanahmet, Fatih, İstanbul",           "slug": "b_sultanahmet2"},
    {"id": "ulusparki",      "name": "Ulus Parkı",          "location": "Ulus Parkı, Beşiktaş, İstanbul",        "slug": "b_ulusparki"},
    {"id": "uskudar",        "name": "Üsküdar",             "location": "Üsküdar İskelesi, İstanbul",             "slug": "b_uskudar"},
]

# stream_url'i slug'dan üret
for cam in CAMERAS:
    cam["stream_url"] = BASE_URL.format(slug=cam["slug"])


class IstanbulRegistry:
    def __init__(self):
        self._session = requests.Session()
        self._session.headers["User-Agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"

    def get_all_cameras(self) -> list[dict]:
        return list(CAMERAS)

    def get_random_cameras(self, count: int = 6) -> list[dict]:
        pool = list(CAMERAS)
        random.shuffle(pool)
        return pool[:count]

    def check_stream(self, camera: dict, timeout: int = 8) -> bool:
        """Stream URL'nin erişilebilir olup olmadığını kontrol et."""
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
