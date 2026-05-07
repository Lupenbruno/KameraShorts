"""6 dilde başlık ve açıklama üretici.

Desteklenen diller: tr, en, es, ar, de, ru
"""
from datetime import datetime

# Saat açıklamaları — dil bazlı
TIME_DESC = {
    "tr": {
        (6, 7):   "Gün Doğumu",
        (7, 9):   "Sabah",
        (9, 12):  "Öğleden Önce",
        (12, 14): "Öğle",
        (14, 17): "Öğleden Sonra",
        (17, 19): "Akşamüstü",
        (19, 21): "Akşam",
        (21, 23): "Gece",
        (23, 24): "Gece Yarısı",
        (0, 6):   "Gece",
    },
    "en": {
        (6, 7):   "Sunrise",
        (7, 9):   "Morning",
        (9, 12):  "Late Morning",
        (12, 14): "Noon",
        (14, 17): "Afternoon",
        (17, 19): "Late Afternoon",
        (19, 21): "Evening",
        (21, 23): "Night",
        (23, 24): "Midnight",
        (0, 6):   "Night",
    },
    "es": {
        (6, 7):   "Amanecer",
        (7, 9):   "Mañana",
        (9, 12):  "Media Mañana",
        (12, 14): "Mediodía",
        (14, 17): "Tarde",
        (17, 19): "Última Hora de la Tarde",
        (19, 21): "Atardecer",
        (21, 23): "Noche",
        (23, 24): "Medianoche",
        (0, 6):   "Noche",
    },
    "ar": {
        (6, 7):   "شروق الشمس",
        (7, 9):   "الصباح",
        (9, 12):  "قبل الظهر",
        (12, 14): "الظهر",
        (14, 17): "بعد الظهر",
        (17, 19): "آخر النهار",
        (19, 21): "المساء",
        (21, 23): "الليل",
        (23, 24): "منتصف الليل",
        (0, 6):   "الليل",
    },
    "de": {
        (6, 7):   "Sonnenaufgang",
        (7, 9):   "Morgen",
        (9, 12):  "Vormittag",
        (12, 14): "Mittag",
        (14, 17): "Nachmittag",
        (17, 19): "Spätnachmittag",
        (19, 21): "Abend",
        (21, 23): "Nacht",
        (23, 24): "Mitternacht",
        (0, 6):   "Nacht",
    },
    "ru": {
        (6, 7):   "Рассвет",
        (7, 9):   "Утро",
        (9, 12):  "Позднее утро",
        (12, 14): "Полдень",
        (14, 17): "День",
        (17, 19): "Поздний день",
        (19, 21): "Вечер",
        (21, 23): "Ночь",
        (23, 24): "Полночь",
        (0, 6):   "Ночь",
    },
}

# Şehir isimleri
CITY_NAMES = {
    "Ankara":   {"tr": "Ankara",   "en": "Ankara",   "es": "Ankara",    "ar": "أنقرة",      "de": "Ankara",   "ru": "Анкара"},
    "İstanbul": {"tr": "İstanbul", "en": "Istanbul",  "es": "Estambul",  "ar": "إسطنبول",    "de": "Istanbul", "ru": "Стамбул"},
    "Konya":    {"tr": "Konya",    "en": "Konya",     "es": "Konya",     "ar": "قونية",      "de": "Konya",    "ru": "Конья"},
    "Çorum":    {"tr": "Çorum",    "en": "Çorum",     "es": "Çorum",     "ar": "جوروم",      "de": "Çorum",    "ru": "Чорум"},
    "Türkiye":  {"tr": "Türkiye",  "en": "Turkey",    "es": "Turquía",   "ar": "تركيا",      "de": "Türkei",   "ru": "Турция"},
}

# Kanal tagları
CHANNEL_TAGS = {
    "tr": "#CanlıKamera #Türkiye #CanlıYayın",
    "en": "#LiveCamera #Turkey #LiveStream",
    "es": "#CámaraEnVivo #Turquía #EnVivo",
    "ar": "#كاميرا_مباشرة #تركيا #بث_مباشر",
    "de": "#LiveKamera #Türkei #LiveStream",
    "ru": "#ЖиваяКамера #Турция #Прямой",
}


def _get_time_desc(lang: str, hour: int) -> str:
    table = TIME_DESC.get(lang, TIME_DESC["en"])
    for (start, end), desc in table.items():
        if start <= hour < end:
            return desc
    return ""


def _city(city_key: str, lang: str) -> str:
    return CITY_NAMES.get(city_key, {}).get(lang, city_key)


# ------------------------------------------------------------------
# ANKARA — otobüs kameraları
# ------------------------------------------------------------------
def ankara_localizations(location: str, now: datetime) -> dict:
    """Her dil için {title, description} döndür."""
    h = now.hour
    t = now.strftime("%H:%M")
    date = now.strftime("%d.%m.%Y")

    result = {}

    # Türkçe
    result["tr"] = {
        "title": f"{_get_time_desc('tr', h)} — {location} 🚌 #Shorts",
        "description": (
            f"🚌 Ankara EGO Otobüsü — Canlı Kamera\n"
            f"📍 {location}\n"
            f"🕐 {t} · {date}\n\n"
            f"Ankara sokaklarından gerçek zamanlı otobüs kamera görüntüleri.\n\n"
            f"#Ankara #EGO #CanlıKamera #AnkaraSokaklari #Shorts"
        ),
    }

    # İngilizce
    result["en"] = {
        "title": f"{_get_time_desc('en', h)} in Ankara 🚌 | {location} #Shorts",
        "description": (
            f"🚌 Ankara EGO Bus — Live Camera\n"
            f"📍 {location}\n"
            f"🕐 {t} · {date}\n\n"
            f"Real-time bus camera footage from the streets of Ankara, Turkey.\n\n"
            f"#Ankara #Turkey #LiveCamera #Bus #Shorts"
        ),
    }

    # İspanyolca
    result["es"] = {
        "title": f"{_get_time_desc('es', h)} en Ankara 🚌 | {location} #Shorts",
        "description": (
            f"🚌 Autobús EGO de Ankara — Cámara en Vivo\n"
            f"📍 {location}\n"
            f"🕐 {t} · {date}\n\n"
            f"Imágenes en tiempo real de cámaras de autobuses en las calles de Ankara, Turquía.\n\n"
            f"#Ankara #Turquía #CámaraEnVivo #Autobús #Shorts"
        ),
    }

    # Arapça
    result["ar"] = {
        "title": f"{_get_time_desc('ar', h)} في أنقرة 🚌 | {location} #Shorts",
        "description": (
            f"🚌 حافلة EGO في أنقرة — كاميرا مباشرة\n"
            f"📍 {location}\n"
            f"🕐 {t} · {date}\n\n"
            f"لقطات في الوقت الفعلي من كاميرات الحافلات في شوارع أنقرة، تركيا.\n\n"
            f"#أنقرة #تركيا #كاميرا_مباشرة #حافلة #Shorts"
        ),
    }

    # Almanca
    result["de"] = {
        "title": f"{_get_time_desc('de', h)} in Ankara 🚌 | {location} #Shorts",
        "description": (
            f"🚌 Ankara EGO Bus — Live-Kamera\n"
            f"📍 {location}\n"
            f"🕐 {t} · {date}\n\n"
            f"Echtzeit-Buskaueraufnahmen aus den Straßen von Ankara, Türkei.\n\n"
            f"#Ankara #Türkei #LiveKamera #Bus #Shorts"
        ),
    }

    # Rusça
    result["ru"] = {
        "title": f"{_get_time_desc('ru', h)} в Анкаре 🚌 | {location} #Shorts",
        "description": (
            f"🚌 Автобус EGO в Анкаре — Живая Камера\n"
            f"📍 {location}\n"
            f"🕐 {t} · {date}\n\n"
            f"Прямая трансляция с камер автобусов на улицах Анкары, Турция.\n\n"
            f"#Анкара #Турция #ЖиваяКамера #Автобус #Shorts"
        ),
    }

    return result


# ------------------------------------------------------------------
# ŞEHİR KAMERALARI — İstanbul, Konya, Çorum vb.
# ------------------------------------------------------------------
def city_localizations(camera_name: str, location: str,
                       city_key: str, now: datetime) -> dict:
    """Şehir kameraları için 6 dilde {title, description}."""
    h = now.hour
    t = now.strftime("%H:%M")
    date = now.strftime("%d.%m.%Y")

    result = {}

    result["tr"] = {
        "title": f"{camera_name} — {_get_time_desc('tr', h)} 🎥 | {_city(city_key, 'tr')}",
        "description": (
            f"🎥 {camera_name} — Canlı Kamera\n"
            f"📍 {location}\n"
            f"🕐 {t} · {date}\n\n"
            f"{_city(city_key, 'tr')} şehir kameralarından canlı görüntü.\n\n"
            f"#{_city(city_key,'tr').replace('İ','I')} #CanlıKamera #turkey"
        ),
    }

    result["en"] = {
        "title": f"{camera_name} — {_get_time_desc('en', h)} 🎥 | {_city(city_key, 'en')}, Turkey",
        "description": (
            f"🎥 {camera_name} — Live Camera\n"
            f"📍 {location}\n"
            f"🕐 {t} · {date}\n\n"
            f"Live footage from city cameras in {_city(city_key, 'en')}, Turkey.\n\n"
            f"#{_city(city_key,'en')} #LiveCamera #Turkey #LiveStream"
        ),
    }

    result["es"] = {
        "title": f"{camera_name} — {_get_time_desc('es', h)} 🎥 | {_city(city_key, 'es')}, Turquía",
        "description": (
            f"🎥 {camera_name} — Cámara en Vivo\n"
            f"📍 {location}\n"
            f"🕐 {t} · {date}\n\n"
            f"Imágenes en directo de cámaras urbanas en {_city(city_key, 'es')}, Turquía.\n\n"
            f"#{_city(city_key,'es')} #CámaraEnVivo #Turquía"
        ),
    }

    result["ar"] = {
        "title": f"{camera_name} — {_get_time_desc('ar', h)} 🎥 | {_city(city_key, 'ar')}",
        "description": (
            f"🎥 {camera_name} — كاميرا مباشرة\n"
            f"📍 {location}\n"
            f"🕐 {t} · {date}\n\n"
            f"لقطات مباشرة من كاميرات المدينة في {_city(city_key, 'ar')}، تركيا.\n\n"
            f"#{_city(city_key,'ar')} #كاميرا_مباشرة #تركيا"
        ),
    }

    result["de"] = {
        "title": f"{camera_name} — {_get_time_desc('de', h)} 🎥 | {_city(city_key, 'de')}, Türkei",
        "description": (
            f"🎥 {camera_name} — Live-Kamera\n"
            f"📍 {location}\n"
            f"🕐 {t} · {date}\n\n"
            f"Live-Aufnahmen von Stadtkameras in {_city(city_key, 'de')}, Türkei.\n\n"
            f"#{_city(city_key,'de')} #LiveKamera #Türkei"
        ),
    }

    result["ru"] = {
        "title": f"{camera_name} — {_get_time_desc('ru', h)} 🎥 | {_city(city_key, 'ru')}",
        "description": (
            f"🎥 {camera_name} — Живая Камера\n"
            f"📍 {location}\n"
            f"🕐 {t} · {date}\n\n"
            f"Прямая трансляция с городских камер в {_city(city_key, 'ru')}, Турция.\n\n"
            f"#{_city(city_key,'ru')} #ЖиваяКамера #Турция"
        ),
    }

    return result
