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


def _brightness(frame_path: str) -> float:
    """Karedeki ortalama parlaklığı döndür (0-255). PIL yoksa 128 döner."""
    try:
        from PIL import Image
        import numpy as np
        arr = np.array(Image.open(frame_path).convert("L"))
        return float(arr.mean())
    except Exception:
        return 128.0


def _dynamic_min_score(brightness: float) -> int:
    """Parlaklığa göre dinamik MIN_SCORE.

    Gece IR modu (< 60) → 2   (çok karanlık, YOLO hassasiyeti düşük)
    Alacakaranlık (60-100)→ 3
    Gündüz (> 100)         → 4  (varsayılan MIN_SCORE)
    """
    if brightness < 60:
        return 2
    if brightness < 100:
        return 3
    return MIN_SCORE


# CLAHE kontrastlı kare çekme filtresi
# eq=contrast: IR görüntüde nesne sınırlarını belirginleştirir
_VF_YOLO = "scale=640:-1,crop=iw:ih*0.75:0:0,eq=contrast=1.4:brightness=0.05"

# COCO sınıflarından sokak/trafik için puanlar
# Otobüs/kamyon kameralarında zemin, damper içi, tavan → 0 puan → elenir
OBJECT_SCORES = {
    # person skoru düşürüldü — otobüs kamerası alt karede her zaman yolcu görür
    # Araç/otobüs tespiti çok daha güvenilir "sokak sahnesi" göstergesi
    0:  1,   # person (1'e düşürüldü — otobüs yolcusu false positive'e karşı)
    1:  2,   # bicycle
    2:  3,   # car
    3:  2,   # motorcycle
    5:  4,   # bus
    7:  2,   # truck
    9:  1,   # traffic light
    11: 1,   # stop sign
    13: 1,   # bench
    56: 1,   # chair
    60: 1,   # dining table
}

MIN_SCORE   = 4    # Zemin/damper → 0, tek uzak araç yetmez; sokak sahnesi gerekli
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


def score_clip(video_path: str, ffmpeg: str = "ffmpeg", duration: int = 30) -> tuple[int, int]:
    """
    Klipten 5 kare çek, YOLO ile analiz et.
    Döndürür: (toplam_skor, dinamik_esik)
    Skor < esik → elenecek | skor >= esik → geçecek
    """
    if not _load_model():
        return 99, MIN_SCORE  # AI yoksa hep geçir

    _NW = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
    step = max(2, duration // 6)
    timestamps = [step, step * 2, step * 3, step * 4, step * 5]
    total = 0
    first_brightness = 128.0

    with tempfile.TemporaryDirectory() as tmp:
        for i, t in enumerate(timestamps):
            frame = os.path.join(tmp, f"frame_{i}.jpg")
            cmd = [ffmpeg, "-y", "-ss", str(t), "-i", video_path,
                   "-frames:v", "1", "-q:v", "3", "-vf", _VF_YOLO, frame]
            try:
                subprocess.run(cmd, capture_output=True, timeout=10, **_NW)
            except Exception:
                continue
            if not Path(frame).exists():
                continue
            # İlk karedeki parlaklığı ölç (dinamik eşik için)
            if i == 0:
                first_brightness = _brightness(frame)
            try:
                results = _model(frame, conf=CONF_THRESH, verbose=False)
                for r in results:
                    for cls_id in (r.boxes.cls.tolist() if r.boxes else []):
                        total += OBJECT_SCORES.get(int(cls_id), 0)
            except Exception as e:
                log.debug(f"YOLO kare analiz hatasi: {e}")

    dyn_min = _dynamic_min_score(first_brightness)
    log.debug(f"score_clip: toplam={total}, parlaklik={first_brightness:.0f}, esik={dyn_min}")
    return total, dyn_min


# Türkçe nesne açıklamaları (TTS için)
_TR_NAMES = {
    0:  ("kişi",    "kişi"),
    1:  ("bisiklet","bisiklet"),
    2:  ("araç",    "araç"),
    3:  ("motosiklet", "motosiklet"),
    5:  ("otobüs",  "otobüs"),
    7:  ("kamyon",  "kamyon"),
    9:  ("trafik lambası", "trafik lambası"),
    11: ("dur tabelası",  "dur tabelası"),
    13: ("bank",    "bank"),
}


def describe_clip(video_path: str, ffmpeg: str = "ffmpeg", duration: int = 30) -> str:
    """Klipteki nesneleri say, Türkçe cümle döndür.

    Örnek: "3 araç, 2 kişi ve 1 otobüs görüntülendi."
    YOLO yoksa "" döner.
    """
    if not _load_model():
        return ""

    _NW = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
    step = max(2, duration // 6)
    timestamps = [step, step * 2, step * 3]   # 3 kare yeterli
    counts: dict[int, int] = {}

    with tempfile.TemporaryDirectory() as tmp:
        for i, t in enumerate(timestamps):
            frame = os.path.join(tmp, f"f{i}.jpg")
            try:
                subprocess.run(
                    [ffmpeg, "-y", "-ss", str(t), "-i", video_path,
                     "-frames:v", "1", "-q:v", "3", "-vf", _VF_YOLO, frame],
                    capture_output=True, timeout=10, **_NW
                )
            except Exception:
                continue
            if not Path(frame).exists():
                continue
            try:
                results = _model(frame, conf=CONF_THRESH, verbose=False)
                for r in results:
                    for cls_id in (r.boxes.cls.tolist() if r.boxes else []):
                        cls_id = int(cls_id)
                        if cls_id in _TR_NAMES:
                            counts[cls_id] = max(counts.get(cls_id, 0), 1)
                            counts[cls_id] += 0   # sadece varlık, tekrar sayma
            except Exception:
                pass

    # Maksimum sayıyı bulmak için tüm karelerdeki en yüksek değeri al
    # (yukarıdaki döngü her karede en az 1 sayıyor, tekrar çalıştır)
    counts = {}
    with tempfile.TemporaryDirectory() as tmp:
        for i, t in enumerate(timestamps):
            frame = os.path.join(tmp, f"f{i}.jpg")
            try:
                subprocess.run(
                    [ffmpeg, "-y", "-ss", str(t), "-i", video_path,
                     "-frames:v", "1", "-q:v", "3",
                     "-vf", _VF_YOLO, frame],
                    capture_output=True, timeout=10, **_NW
                )
            except Exception:
                continue
            if not Path(frame).exists():
                continue
            try:
                frame_counts: dict[int, int] = {}
                results = _model(frame, conf=CONF_THRESH, verbose=False)
                for r in results:
                    for cls_id in (r.boxes.cls.tolist() if r.boxes else []):
                        cls_id = int(cls_id)
                        if cls_id in _TR_NAMES:
                            frame_counts[cls_id] = frame_counts.get(cls_id, 0) + 1
                for cls_id, cnt in frame_counts.items():
                    counts[cls_id] = max(counts.get(cls_id, 0), cnt)
            except Exception:
                pass

    if not counts:
        return ""

    # Öncelik sırası: otobüs > araç > kamyon > kişi > diğer
    priority = [5, 2, 7, 0, 3, 1, 9, 11, 13]
    parts = []
    for cls_id in priority:
        if cls_id not in counts:
            continue
        cnt = counts[cls_id]
        singular, plural = _TR_NAMES[cls_id]
        word = plural if cnt > 1 else singular
        parts.append(f"{cnt} {word}")

    if not parts:
        return ""

    if len(parts) == 1:
        return f"{parts[0]} görüntülendi."
    return ", ".join(parts[:-1]) + f" ve {parts[-1]} görüntülendi."


def _sky_bonus(frame_path: str) -> int:
    """Üst 1/3'te gökyüzü var mı?

    Gökyüzü = mavi kanal, yol/asfalt = gri (R≈G≈B).
    Mavi - kırmızı farkı >15 ise gökyüzü → +3 puan.
    PIL/numpy yoksa 0 döner (sessizce atlar).
    """
    try:
        from PIL import Image
        import numpy as np
        img = Image.open(frame_path).convert("RGB")
        arr = np.array(img)
        h = arr.shape[0]
        top = arr[: h // 3]          # üst 1/3
        r_mean = top[:, :, 0].mean()
        g_mean = top[:, :, 1].mean()
        b_mean = top[:, :, 2].mean()
        # Gökyüzü: mavi baskın VE yeterince parlak (çok karanlık değil)
        is_sky = (b_mean - r_mean > 15) and (b_mean > 80)
        bonus = 3 if is_sky else 0
        log.debug(f"Gökyüzü: R={r_mean:.0f} G={g_mean:.0f} B={b_mean:.0f} → +{bonus}p")
        return bonus
    except Exception:
        return 0


def quick_check(stream_url: str, ffmpeg: str = "ffmpeg") -> bool:
    """Stream URL'den 1 kare çek, YOLO + gökyüzü kontrolü yap.

    Kayıt başlamadan önce çağrılır — zemin/damper/karanlık ise False döner.
    YOLO yoksa her zaman True döner (geçir).
    ~3 saniye sürer.
    """
    if not _load_model():
        return True

    _NW = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}

    with tempfile.TemporaryDirectory() as tmp:
        frame = os.path.join(tmp, "qc.jpg")
        cmd = [
            ffmpeg, "-y",
            "-tls_verify", "0",
            "-i", stream_url,
            "-frames:v", "1",
            "-ss", "2",
            "-q:v", "4",
            "-vf", _VF_YOLO,
            frame
        ]
        try:
            subprocess.run(cmd, capture_output=True, timeout=15, **_NW)
        except Exception:
            return True  # timeout → geçir, asıl kayıtta anlaşılır

        if not Path(frame).exists() or Path(frame).stat().st_size < 500:
            return True  # kare alınamadı → geçir

        try:
            results = _model(frame, conf=CONF_THRESH, verbose=False)
            score = 0
            for r in results:
                for cls_id in (r.boxes.cls.tolist() if r.boxes else []):
                    score += OBJECT_SCORES.get(int(cls_id), 0)
        except Exception:
            return True

        # Gökyüzü görünüyorsa kamera açısı doğru → bonus puan
        score += _sky_bonus(frame)
        # Dinamik eşik — gece IR modunda daha düşük
        dyn_min = _dynamic_min_score(_brightness(frame))

    passed = score >= dyn_min
    log.info(f"Ön kontrol: {score}p (esik:{dyn_min}) → {'GECTI' if passed else 'ELENDI'}")
    return passed


def is_interesting(video_path: str, ffmpeg: str = "ffmpeg", duration: int = 30) -> bool:
    """True → yükle, False → atla."""
    score, dyn_min = score_clip(video_path, ffmpeg, duration)
    gecti = score >= dyn_min
    log.info(f"AI skor: {score} (esik:{dyn_min}) → {'GECTI' if gecti else 'ELENDI'}")
    return gecti


def best_frame(video_path: str, ffmpeg: str = "ffmpeg", duration: int = 30) -> str | None:
    """En yüksek YOLO skoruna sahip kareyi 1280x720 thumbnail olarak kaydet.

    Çıktı: video_path ile aynı dizinde .jpg dosyası.
    YOLO yoksa ortadaki kare kullanılır.
    """
    _NW = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
    thumb_path = str(Path(video_path).with_suffix(".jpg"))
    vf = ("scale=1280:720:force_original_aspect_ratio=decrease,"
          "pad=1280:720:(ow-iw)/2:(oh-ih)/2")

    step = max(2, duration // 6)
    timestamps = [step, step * 2, step * 3, step * 4, step * 5]

    if not _load_model():
        # YOLO yoksa ortadaki kareyi al
        cmd = [ffmpeg, "-y", "-ss", str(duration // 2), "-i", video_path,
               "-frames:v", "1", "-q:v", "2", "-vf", vf, thumb_path]
        try:
            subprocess.run(cmd, capture_output=True, timeout=10, **_NW)
        except Exception:
            return None
        return thumb_path if Path(thumb_path).exists() else None

    best_score = -1
    best_t = timestamps[len(timestamps) // 2]

    with tempfile.TemporaryDirectory() as tmp:
        for i, t in enumerate(timestamps):
            frame = os.path.join(tmp, f"f{i}.jpg")
            cmd = [ffmpeg, "-y", "-ss", str(t), "-i", video_path,
                   "-frames:v", "1", "-q:v", "3", "-vf", "scale=640:-1", frame]
            try:
                subprocess.run(cmd, capture_output=True, timeout=10, **_NW)
            except Exception:
                continue
            if not Path(frame).exists():
                continue
            score = 0
            try:
                results = _model(frame, conf=CONF_THRESH, verbose=False)
                for r in results:
                    for cls_id in (r.boxes.cls.tolist() if r.boxes else []):
                        score += OBJECT_SCORES.get(int(cls_id), 0)
            except Exception:
                pass
            if score > best_score:
                best_score = score
                best_t = t

    # En iyi timestamp'ten tam kaliteli thumbnail çek
    cmd = [ffmpeg, "-y", "-ss", str(best_t), "-i", video_path,
           "-frames:v", "1", "-q:v", "2", "-vf", vf, thumb_path]
    try:
        subprocess.run(cmd, capture_output=True, timeout=10, **_NW)
    except Exception:
        return None
    log.info(f"Thumbnail: t={best_t}s, skor={best_score}, {Path(thumb_path).name}")
    return thumb_path if Path(thumb_path).exists() else None
