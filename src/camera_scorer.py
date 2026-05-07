"""Ankara EGO kamera skor sistemi.

Her kayıt öncesi adaylardan kısa birer kare çeker, analiz eder,
en kaliteli görüntüyü veren kamerayı seçer.

Skor (0-100):
  Parlaklık   0-30  — çok karanlık veya aşırı parlak düşük puan
  Hareket     0-30  — hareketsiz/donuk düşük puan
  Netlik      0-20  — bulanık düşük puan
  Saat bonusu 0-20  — gün doğumu/batımı yüksek puan
"""
import logging
import os
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

log = logging.getLogger("kamerashorts")

try:
    from PIL import Image, ImageFilter
    PIL_OK = True
except ImportError:
    PIL_OK = False
    log.warning("Pillow yuklu degil, skor sistemi devre disi. pip install pillow")

MIN_SCORE = 35   # Bu skorun altındaki kameralar atlanır
FRAME_TIMEOUT = 12   # Saniye — bir kare çekmek için max süre
MAX_WORKERS = 4   # Paralel kamera analizi


class CameraScorer:
    def __init__(self, ffmpeg_path: str):
        self.ffmpeg = ffmpeg_path

    # ------------------------------------------------------------------
    def pick_best(self, candidates: list[dict], now: datetime = None,
                  top_n: int = 5) -> list[dict]:
        """
        Aday listesinden en iyi `top_n` kamerayı skor sırasıyla döndür.
        PIL yoksa veya hata olursa orijinal listeyi döndür.
        """
        if not PIL_OK or not candidates:
            return candidates

        if now is None:
            now = datetime.now()

        # Sadece ilk N adayı analiz et — zaman kazanmak için
        pool = candidates[:max(top_n * 2, 10)]

        scored = []
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
            futures = {ex.submit(self._score_camera, c, now): c for c in pool}
            for future in as_completed(futures):
                cam = futures[future]
                try:
                    s = future.result()
                except Exception:
                    s = 0
                scored.append((s, cam))
                log.info(f"  Skor [{cam.get('license_plate','?')}]: {s}/100")

        # Skora göre sırala, minimum eşiği geç
        scored.sort(key=lambda x: x[0], reverse=True)
        passed = [c for s, c in scored if s >= MIN_SCORE]

        if not passed:
            log.warning("Hicbir kamera minimum skoru gecemedi, ham listeye donuluyor")
            return [c for _, c in scored]   # yine de en iyiden başla

        log.info(f"Skor filtresi: {len(passed)}/{len(scored)} kamera gecti (min {MIN_SCORE})")
        return passed

    # ------------------------------------------------------------------
    def _score_camera(self, camera: dict, now: datetime) -> int:
        stream_url = camera.get("stream_url", "")
        if not stream_url:
            return 0

        with tempfile.TemporaryDirectory() as tmp:
            f1 = os.path.join(tmp, "f1.jpg")
            f2 = os.path.join(tmp, "f2.jpg")

            ok1 = self._grab_frame(stream_url, f1, seek=1)
            if not ok1:
                return 0

            ok2 = self._grab_frame(stream_url, f2, seek=3)

            brightness  = self._get_brightness(f1)
            b_score     = self._brightness_score(brightness)        # 0-30
            m_score     = self._motion_score(f1, f2) if ok2 else 5  # 0-30
            sh_score    = self._sharpness_score(f1)                  # 0-20
            t_score     = self._time_bonus(now)                      # 0-20

            return b_score + m_score + sh_score + t_score

    # ------------------------------------------------------------------
    def _grab_frame(self, url: str, out_path: str, seek: int = 1) -> bool:
        cmd = [
            self.ffmpeg, "-y",
            "-ss", str(seek),
            "-i", url,
            "-frames:v", "1",
            "-q:v", "4",
            "-vf", "scale=160:90",   # küçük boyut → hızlı analiz
            out_path
        ]
        try:
            subprocess.run(cmd, capture_output=True, timeout=FRAME_TIMEOUT)
            return Path(out_path).exists() and Path(out_path).stat().st_size > 500
        except Exception:
            return False

    # ------------------------------------------------------------------
    def _get_brightness(self, img_path: str) -> float:
        """Ortalama parlaklık 0-255."""
        try:
            img = Image.open(img_path).convert("L")
            pixels = list(img.getdata())
            return sum(pixels) / len(pixels)
        except Exception:
            return 128

    def _brightness_score(self, b: float) -> int:
        """0-30 puan. İdeal aralık: 50-200."""
        if b < 15:   return 0   # Siyah / çok karanlık
        if b < 35:   return 8   # Karanlık
        if b < 50:   return 18
        if b <= 200: return 30  # İdeal
        if b <= 220: return 18
        return 5                 # Aşırı parlak / overexposed

    def _motion_score(self, img1_path: str, img2_path: str) -> int:
        """0-30 puan. İki kare arası fark = hareket."""
        try:
            img1 = Image.open(img1_path).convert("L")
            img2 = Image.open(img2_path).convert("L")
            p1 = list(img1.getdata())
            p2 = list(img2.getdata())
            diff = sum(abs(a - b) for a, b in zip(p1, p2)) / max(len(p1), 1)
            # diff: 0-255 arası, tipik hareketli sahne: 5-30
            if diff < 1:   return 0   # Tamamen donuk
            if diff < 3:   return 5   # Çok az hareket
            if diff < 8:   return 15
            if diff < 20:  return 25
            return 30                  # Çok hareketli
        except Exception:
            return 10

    def _sharpness_score(self, img_path: str) -> int:
        """0-20 puan. Kenar yoğunluğu = netlik."""
        try:
            img = Image.open(img_path).convert("L")
            edges = img.filter(ImageFilter.FIND_EDGES)
            pixels = list(edges.getdata())
            avg = sum(pixels) / max(len(pixels), 1)
            # avg: düşük = bulanık, yüksek = net
            if avg < 3:   return 0
            if avg < 6:   return 8
            if avg < 12:  return 14
            return 20
        except Exception:
            return 10

    def _time_bonus(self, now: datetime) -> int:
        """0-20 puan. Gün doğumu/batımı en yüksek."""
        h = now.hour
        if 6  <= h <= 7:  return 20   # Gün doğumu — altın ışık
        if 18 <= h <= 20: return 20   # Gün batımı — altın ışık
        if 7  <= h <= 9:  return 15   # Sabah trafiği
        if 12 <= h <= 13: return 12   # Öğle kalabalığı
        if 17 <= h <= 18: return 14   # Akşam trafiği
        if 21 <= h <= 22: return 8    # Akşam ışıkları
        if h >= 23 or h <= 5: return 2  # Gece
        return 10
