"""Fetches live Ankara bus cameras from seyret.ankara.bel.tr/status.json"""
import random
import requests


STATUS_URL = "https://seyret.ankara.bel.tr/status.json"


class CameraRegistry:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "KameraShorts/1.0"

    # Sadece yolcu taşıyan araçları al
    BUS_TYPES = {"Solo", "ELK", "Körüklü", "Körüklü ELK", "Minibüs", "Midibüs"}

    def get_active_cameras(self, limit: int = None, buses_only: bool = False) -> list[dict]:
        """Aktif kamera olan araçları döndür."""
        data = self.session.get(STATUS_URL, timeout=15).json()

        active = [
            v for v in data
            if v.get("stream_url")
            and v.get("dvr_serial_number")
            and v.get("is_visible")
            and (not buses_only or v.get("vehicle_type") in self.BUS_TYPES)
        ]
        if limit:
            active = random.sample(active, min(limit, len(active)))
        return active

    def get_stream_url(self, vehicle: dict) -> str:
        # stream_url API'den zaten geliyor, direkt kullan
        return vehicle["stream_url"]

    def get_random_camera(self) -> dict | None:
        cameras = self.get_active_cameras()
        return random.choice(cameras) if cameras else None
