"""Ankara kameralarini canli tutar.

- Her 60s tum kameralari tarar, aktif olanlari bulur
- Aktif her kamera icin arka planda segment indirip atar (ghost viewer)
- Ana sistem get_warm() ile sicak kamera listesini alir
"""
import logging
import subprocess
import threading
import time

log = logging.getLogger("kamerashorts")


class AnkaraKeeper:
    def __init__(self, cameras: list):
        self._cameras = cameras           # tum ankara kameralari
        self._warm: dict[str, dict] = {}  # {stream_url: cam}
        self._lock = threading.Lock()
        self._procs: dict[str, subprocess.Popen] = {}  # keep-alive proc'lari

        t1 = threading.Thread(target=self._scan_loop, daemon=True)
        t1.start()

    # ------------------------------------------------------------------
    def get_warm(self) -> list:
        """Simdi canli olan kamera listesi."""
        with self._lock:
            return list(self._warm.values())

    # ------------------------------------------------------------------
    def _scan_loop(self):
        """Her 60s tum kameralari tara."""
        while True:
            self._scan()
            time.sleep(60)

    def _scan(self):
        import concurrent.futures

        def check(cam):
            try:
                r = subprocess.run(
                    ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                     cam["stream_url"], "--max-time", "4"],
                    capture_output=True
                )
                return cam, r.stdout.decode().strip() == "200"
            except Exception:
                return cam, False

        with concurrent.futures.ThreadPoolExecutor(max_workers=25) as ex:
            results = list(ex.map(check, self._cameras))

        newly_active = []
        newly_dead = []

        for cam, alive in results:
            url = cam["stream_url"]
            with self._lock:
                was_warm = url in self._warm
            if alive and not was_warm:
                newly_active.append(cam)
            elif not alive and was_warm:
                newly_dead.append(cam)

        for cam in newly_active:
            self._start_keepalive(cam)
            with self._lock:
                self._warm[cam["stream_url"]] = cam
            log.info(f"[ankara-keeper] Aktif: {cam['name']}")

        for cam in newly_dead:
            self._stop_keepalive(cam["stream_url"])
            with self._lock:
                self._warm.pop(cam["stream_url"], None)
            log.info(f"[ankara-keeper] Kapandi: {cam['name']}")

        with self._lock:
            total = len(self._warm)
        if total > 0:
            log.info(f"[ankara-keeper] {total} kamera sicak")

    # ------------------------------------------------------------------
    def _start_keepalive(self, cam: dict):
        """FFmpeg ile kamerayi izle ama /dev/null'a yaz - ghost viewer."""
        url = cam["stream_url"]
        if url in self._procs:
            return
        cmd = [
            "ffmpeg", "-v", "quiet",
            "-tls_verify", "0",
            "-reconnect", "1",
            "-reconnect_streamed", "1",
            "-reconnect_delay_max", "3",
            "-i", url,
            "-f", "null", "-"
        ]
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL)
            self._procs[url] = proc
            log.info(f"[ankara-keeper] Keep-alive basladi: {cam['name']} PID {proc.pid}")
        except Exception as e:
            log.warning(f"[ankara-keeper] Keep-alive baslanamadi: {e}")

    def _stop_keepalive(self, url: str):
        proc = self._procs.pop(url, None)
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()

    def stop_all(self):
        for url in list(self._procs):
            self._stop_keepalive(url)
