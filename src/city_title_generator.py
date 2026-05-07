"""Generic başlık ve metadata üretici — herhangi bir Türk şehri kamera pipeline'ı için."""
from datetime import datetime
from src.multilingual_titles import city_localizations

SAAT_TR = {
    0: "gece yarısı", 1: "gece 01:00", 2: "gece 02:00", 3: "gece 03:00",
    4: "sabah 04:00", 5: "sabah 05:00", 6: "sabah 06:00", 7: "sabah 07:00",
    8: "sabah 08:00", 9: "sabah 09:00", 10: "sabah 10:00", 11: "sabah 11:00",
    12: "öğlen", 13: "öğleden sonra 13:00", 14: "öğleden sonra 14:00",
    15: "öğleden sonra 15:00", 16: "öğleden sonra 16:00", 17: "akşamüstü 17:00",
    18: "akşam 18:00", 19: "akşam 19:00", 20: "akşam 20:00", 21: "gece 21:00",
    22: "gece 22:00", 23: "gece 23:00",
}

# config.yaml şehir anahtarı → multilingual_titles CITY_NAMES anahtarı
CITY_KEY_MAP = {
    "corum":    "Çorum",
    "konya":    "Konya",
    "istanbul": "İstanbul",
    "ankara":   "Ankara",
}


class CityTitleGenerator:
    def __init__(self, city_name: str, tags_base: list[str], city_key: str = ""):
        self.city_name = city_name
        self.tags_base = list(tags_base)
        self.city_key = city_key   # multilingual için ("corum", "konya", vb.)

    def generate(self, camera: dict, now: datetime) -> dict:
        name = camera["name"]
        location = camera["location"]
        saat = now.strftime("%H:%M")
        tarih = now.strftime("%d.%m.%Y")
        saat_desc = SAAT_TR.get(now.hour, saat)

        title = f"{name} Canlı Kamera | {saat} | {tarih}"
        description = (
            f"🎥 {name} — {saat_desc} ({tarih})\n"
            f"📍 {location}\n\n"
            f"{self.city_name} şehir kameralarından canlı görüntü.\n\n"
            f"#{self.city_name} #CanlıKamera #{''.join(name.split())} "
            f"#{self.city_name}Kamera #turkey #turkiye"
        )

        tags = list(self.tags_base) + [
            name,
            name.replace(" ", ""),
            location.split(",")[0].strip(),
        ]

        # Çok dilli başlık/açıklama
        city_names_key = CITY_KEY_MAP.get(self.city_key, self.city_name)
        localizations = city_localizations(name, location, city_names_key, now)

        return {
            "title": title,
            "description": description,
            "tags": tags,
            "camera_name": name,
            "location": location,
            "localizations": localizations,
        }
