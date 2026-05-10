"""YOLOv8-nano ile AI destekli klip kalite filtresi.

Klipten 3 kare çeker, her karede nesne tespiti yapar.
Sokak/trafik sahnesi için ilgili nesneleri puanlar.
Tavan, zemin, karanlık, boş görüntüler elenir.
"""
import logging
import os
import subprocess
import sys
import tempfile
from pathlib import Path

log = logging.getLogger("kamerashorts")

# COCO sınıflarından sokak/trafik için puanlar
OBJECT_SCORES = {
    0:  3,   # person
    1:  2,   # bicycle
    2:  2,   # car
    3:  2,   # motorcycle
    5:  2,   # bus
    7:  2,   # truck
    9:  1,   # traffic light
    11: 1,   # stop sign
    13: 1,   # bench
}

MIN_SCORE   = 1    # Bu skorun altı → elenir
CONF_THRESH = 0.30 # Güven eşiği

_model = None
_available = None


def _load_model():
    global _model, _available
    if _available is not None:
        return _available
    try:
        from ultralytics import YOLO
        import torch
        torch.set_num_threads(2)  # CPU thread limiti
        # yolov8n.pt ilk çalıştırmada otomatik indirilir (~6MB)
        _model = YOLO("yolov8n.pt", verbose=False)
        _model.to("cpu")
        _available = True
        log.info("AI filtresi aktif (YOLOv8-nano)")
    except Exception as e:
        log.warning(f"AI filtresi yuklenemedi, devre disi: {e}")
        _available = False
    return _available


def score_clip(video_path: str, ffmpeg: str = "ffmpeg", duration: int = 30) -> int:
    """
    Klipten 3 kare çek, YOLO ile analiz et, toplam skor döndür.
    Skor 0 → tavan/zemin/boş/karanlık
    Skor ≥ 1 → geçerli sokak sahnesi
    """
    if not _load_model():
        return 99  # AI yoksa hep geçir

    step = max(3, duration // 4)
    timestamps = [step, step * 2, step * 3]
    total = 0

    with tempfile.TemporaryDirectory() as tmp:
        for i, t in enumerate(timestamps):
            frame = os.path.join(tmp, f"frame_{i}.jpg")
            cmd = [
                ffmpeg, "-y", "-ss", str(t),
                "-i", video_path,
                "-frames:v", "1",
                "-q:v", "3",
                "-vf", "scale=640:-1",
                frame
            ]
            _NW = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
            try:
                subprocess.run(cmd, capture_output=True, timeout=10, **_NW)
            except Exception:
                continue

            if not Path(frame).exists():
                continue

            try:
                results = _model(frame, conf=CONF_THRESH, verbose=False)
                for r in results:
                    for cls_id in (r.boxes.cls.tolist() if r.boxes else []):
                        total += OBJECT_SCORES.get(int(cls_id), 0)
            except Exception as e:
                log.debug(f"YOLO kare analiz hatasi: {e}")

    return total


def is_interesting(video_path: str, ffmpeg: str = "ffmpeg", duration: int = 30) -> bool:
    """True → yükle, False → atla."""
    score = score_clip(video_path, ffmpeg, duration)
    log.info(f"AI skor: {score} ({'GECTI' if score >= MIN_SCORE else 'ELENDI'})")
    return score >= MIN_SCORE
