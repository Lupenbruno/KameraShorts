"""Generates YouTube titles like '3/5/2026 15:00 - TEM Otoyolu - Ankara Otobüsü'"""
from datetime import datetime
from src.multilingual_titles import ankara_localizations

WEEKDAYS_TR = ["Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar"]

VEHICLE_TYPE_TR = {
    "Solo": "Solo Otobüs",
    "ELK": "Elektrikli Otobüs",
    "Körüklü": "Körüklü Otobüs",
    "Körüklü ELK": "Körüklü Elektrikli Otobüs",
    "Minibüs": "Minibüs",
}


class TitleGenerator:
    def __init__(self, config: dict):
        self.tags = config["youtube"].get("tags", [])

    def generate(self, vehicle: dict, location: str, capture_time: datetime) -> dict:
        d = capture_time.day
        m = capture_time.month
        y = capture_time.year
        t = capture_time.strftime("%H:%M")
        plate = vehicle.get("license_plate", "?")
        vtype = VEHICLE_TYPE_TR.get(vehicle.get("vehicle_type", ""), "Otobüs")
        speed = vehicle.get("speed", 0)
        weekday = WEEKDAYS_TR[capture_time.weekday()]

        # Başlık: "3/5/2026 15:00 - TEM Otoyolu, Keçiören #Shorts"
        title = f"{d}/{m}/{y} {t} - {location} #Shorts"
        if len(title) > 100:
            title = title[:97] + "..."

        description = (
            f"🚌 Ankara {vtype} - Canlı Kamera\n"
            f"📍 {location}\n"
            f"🚗 Hız: {speed} km/h\n"
            f"📅 {weekday}, {d}/{m}/{y} - Saat {t}\n"
            f"🔢 Plaka: {plate}\n\n"
            f"Ankara Büyükşehir Belediyesi EGO otobüslerinden canlı kamera görüntüleri.\n"
            f"Kaynak: seyret.ankara.bel.tr\n\n"
            f"#ankara #trafik #otobüs #shorts #ankaratrafik #ego #canlikamera"
        )

        tags = list(self.tags) + [
            "ankara", "ego", "otobüs", plate.lower(),
            location.split(",")[0].lower(), weekday.lower()
        ]

        # Çok dilli başlık/açıklama
        localizations = ankara_localizations(location, capture_time)

        return {
            "title": title,
            "description": description,
            "tags": tags[:15],
            "category_id": "22",
            "privacy_status": "public",
            "localizations": localizations,
        }
