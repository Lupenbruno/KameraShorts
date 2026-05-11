"""OpenWeatherMap entegrasyonu — video başlık ve açıklamalarına hava durumu ekler.

Özellikler:
  - Şehir koordinatlarına göre anlık hava durumu
  - 10 dakika önbellek (israf yok, free tier 1000 req/gün)
  - Türkçe durum metni + emoji
  - Kar/yağmur tespiti: viral içerik potansiyeli için özel etiketler
"""
import logging
import time
from typing import Optional

import requests

log = logging.getLogger("kamerashorts")

# Şehir koordinatları (WGS84)
CITY_COORDS = {
    "ankara":   (39.9334, 32.8597),
    "istanbul": (41.0082, 28.9784),
    "corum":    (40.5506, 34.9556),
    "çorum":    (40.5506, 34.9556),
    "konya":    (37.8714, 32.4846),
}

# OpenWeatherMap condition ID → Türkçe açıklama + emoji
# https://openweathermap.org/weather-conditions
_CONDITION_MAP = {
    # Thunderstorm
    range(200, 300): ("⛈️", "fırtınalı"),
    # Drizzle
    range(300, 400): ("🌦️", "çiseleyen"),
    # Rain
    range(500, 502): ("🌧️", "yağmurlu"),
    range(502, 505): ("🌧️", "şiddetli yağmur"),
    range(511, 512): ("🌨️", "dondurucu yağmur"),
    range(520, 532): ("🌦️", "sağanak"),
    # Snow
    range(600, 602): ("❄️", "karlı"),
    range(602, 603): ("❄️", "yoğun kar"),
    range(611, 623): ("🌨️", "kar yağışlı"),
    # Atmosphere
    range(701, 710): ("🌫️", "sisli"),
    range(710, 782): ("🌫️", "dumanlı"),
    # Clear
    range(800, 801): ("☀️", "açık"),
    # Clouds
    range(801, 802): ("🌤️", "az bulutlu"),
    range(802, 803): ("⛅", "parçalı bulutlu"),
    range(803, 805): ("☁️", "bulutlu"),
}

# Önbellek: (city_key, lat_rounded, lon_rounded) → (epoch, weather_dict)
_cache: dict = {}
_CACHE_TTL = 600  # 10 dakika


def _condition_tr(weather_id: int) -> tuple[str, str]:
    """weather ID → (emoji, türkçe_metin)"""
    for key, val in _CONDITION_MAP.items():
        if isinstance(key, range) and weather_id in key:
            return val
    return ("🌡️", "bilinmiyor")


def get_weather(city_key: str = "", lat: float = None, lon: float = None,
                api_key: str = "") -> Optional[dict]:
    """Şehir veya koordinat için hava durumu döndür.

    Döndürülen dict:
    {
        "temp":       22,           # °C (int)
        "feels_like": 20,           # °C
        "humidity":   60,           # %
        "wind_kmh":   14,           # km/h
        "emoji":      "🌤️",
        "condition":  "az bulutlu",
        "id":         801,          # OWM weather ID
        "is_snow":    False,
        "is_rain":    False,
        "is_clear":   True,
        "title_str":  "🌤️ 22°C",   # başlığa koyulacak kısa metin
        "tag_str":    "açıkhava",   # etiket için
    }
    """
    if not api_key:
        return None

    # Koordinat çöz
    if lat is None or lon is None:
        coords = CITY_COORDS.get(city_key.lower())
        if not coords:
            return None
        lat, lon = coords

    cache_key = (round(lat, 2), round(lon, 2))
    if cache_key in _cache:
        ts, data = _cache[cache_key]
        if time.time() - ts < _CACHE_TTL:
            return data

    try:
        r = requests.get(
            "https://api.openweathermap.org/data/2.5/weather",
            params={
                "lat": lat,
                "lon": lon,
                "appid": api_key,
                "units": "metric",
                "lang": "tr",
            },
            timeout=8,
        )
        if not r.ok:
            log.warning(f"OWM hata {r.status_code}: {r.text[:120]}")
            return None

        d = r.json()
        wid  = d["weather"][0]["id"]
        emoji, condition = _condition_tr(wid)

        temp       = round(d["main"]["temp"])
        feels_like = round(d["main"]["feels_like"])
        humidity   = d["main"]["humidity"]
        wind_ms    = d["wind"].get("speed", 0)
        wind_kmh   = round(wind_ms * 3.6)

        is_snow  = 600 <= wid < 700
        is_rain  = (300 <= wid < 600) and not is_snow
        is_clear = wid == 800

        # Etiket metni (boşluksuz, küçük harf)
        tag_str = condition.replace(" ", "").lower()

        result = {
            "temp":       temp,
            "feels_like": feels_like,
            "humidity":   humidity,
            "wind_kmh":   wind_kmh,
            "emoji":      emoji,
            "condition":  condition,
            "id":         wid,
            "is_snow":    is_snow,
            "is_rain":    is_rain,
            "is_clear":   is_clear,
            "title_str":  f"{emoji} {temp}°C",
            "tag_str":    tag_str,
        }
        _cache[cache_key] = (time.time(), result)
        log.debug(f"OWM [{city_key or f'{lat:.2f},{lon:.2f}'}]: "
                  f"{emoji} {temp}°C {condition}")
        return result

    except Exception as e:
        log.warning(f"OWM istek hatası: {e}")
        return None
