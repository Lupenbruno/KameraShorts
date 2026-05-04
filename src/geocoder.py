"""Reverse geocoding — GPS koordinatlarından yol/mahalle adı üretir."""
import time
import requests


class Geocoder:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "KameraShorts/1.0"
        self._cache = {}

    def get_location_name(self, lat: float, lon: float) -> str:
        """Koordinatları kısa bir konum adına çevirir."""
        key = (round(float(lat), 3), round(float(lon), 3))
        if key in self._cache:
            return self._cache[key]

        try:
            r = self.session.get(
                "https://nominatim.openstreetmap.org/reverse",
                params={"lat": float(lat), "lon": float(lon), "format": "json", "zoom": 16},
                timeout=10,
            )
            data = r.json()
            addr = data.get("address", {})

            # Öncelik sırası: otoyol → yol → mahalle → ilçe
            name = (
                addr.get("motorway")
                or addr.get("trunk")
                or addr.get("primary")
                or addr.get("road")
                or addr.get("pedestrian")
                or addr.get("neighbourhood")
                or addr.get("suburb")
                or addr.get("district")
                or addr.get("county")
                or "Ankara"
            )
            # İlçe bilgisini de ekle
            district = addr.get("district") or addr.get("suburb") or ""
            if district and district.lower() not in name.lower():
                name = f"{name}, {district}"

            time.sleep(0.5)  # Nominatim rate limit (maks 2 istek/sn)
            self._cache[key] = name
            return name

        except Exception:
            return "Ankara"
