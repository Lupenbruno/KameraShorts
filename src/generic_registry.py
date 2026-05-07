"""Generic city camera registry — kamera listesi config'den okunur, döngüsel sıra."""
import json
import warnings
from pathlib import Path

import requests


class GenericRegistry:
    def __init__(self, cameras: list[dict], index_file: str):
        self.cameras = cameras
        self.index_file = Path(index_file)
        self._session = requests.Session()
        self._session.headers["User-Agent"] = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        )
        self.index_file.parent.mkdir(parents=True, exist_ok=True)

    def get_all_cameras(self) -> list[dict]:
        return list(self.cameras)

    def get_next_cameras(self, count: int = 1) -> list[dict]:
        """Sıradaki kameraları döndür, indeksi ilerlet."""
        if not self.cameras:
            return []
        idx = self._read_index()
        result = []
        tried = 0
        while len(result) < count and tried < len(self.cameras):
            cam = self.cameras[idx % len(self.cameras)]
            idx += 1
            tried += 1
            result.append(dict(cam))
        self._write_index(idx)
        return result

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
            if self.index_file.exists():
                return json.loads(self.index_file.read_text())["index"]
        except Exception:
            pass
        return 0

    def _write_index(self, idx: int):
        self.index_file.write_text(
            json.dumps({"index": idx % len(self.cameras)})
        )
