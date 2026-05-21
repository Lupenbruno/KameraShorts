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



def _cv_precheck(frame_path):
    try:
        import cv2
        img = cv2.imread(frame_path, cv2.IMREAD_GRAYSCALE)
        if img is None:
            return True, 128.0
        brightness = float(img.mean())
        if brightness < 10:
            return False, brightness
        variance = float(cv2.Laplacian(img, cv2.CV_64F).var())
        if variance < 15:
            return False, brightness
        return True, brightness
    except ImportError:
        return True, 128.0
    except Exception:
        return True, 128.0


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
_VF_YOLO   = "scale=640:-1,crop=iw:ih*0.75:0:0,eq=contrast=1.4:brightness=0.05"
_VF_BRIGHT = "scale=320:-1,crop=iw:ih*0.75:0:0"   # kontrastsız — doğru parlaklık ölçümü için

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

MIN_SCORE        = 4    # Zemin/damper → 0, tek uzak araç yetmez; sokak sahnesi gerekli
CONF_THRESH      = 0.30 # YOLO tespit eşiği (skorlama için)
TTS_CONF_THRESH  = 0.70 # TTS'e giren tespit eşiği — %70 altı söylenmiyor

# Hareket/aktivite skoru (YOLO "ne var"ı, hareket "ne kadar canlı"yı ölçer).
# Ardışık kareler arası ortalama gri-piksel farkı: akan trafik/kalabalık yüksek,
# boş/park/donuk düşük. Sadece DİJİTAL DONUK (≈0) reddedilir; sakin-ama-canlı
# sahneler YOLO puanıyla geçer; aktif sahneler bonus alır (yanlış-ret riski yok).
MOTION_FROZEN    = 1.0  # ardışık kare farkı bunun altı → donuk (reddet)
MOTION_DIV       = 6    # hareket → bonus bölücü
MOTION_BONUS_CAP = 3    # aktif sahneye en fazla +3 puan

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
        return 0, MIN_SCORE  # ZORUNLU YOLO: model yoksa REDDET (fail-closed)

    _NW = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
    step = max(2, duration // 6)
    timestamps = [step, step * 2, step * 3, step * 4, step * 5]
    total = 0
    first_brightness = 128.0

    with tempfile.TemporaryDirectory() as tmp:
        # Parlaklığı kontrastsız ham kareden ölç (eq filtresi değeri şişirmesin)
        bright_frame = os.path.join(tmp, "bright.jpg")
        try:
            subprocess.run(
                [ffmpeg, "-y", "-ss", str(step), "-i", video_path,
                 "-frames:v", "1", "-q:v", "4", "-vf", _VF_BRIGHT, bright_frame],
                capture_output=True, timeout=10, **_NW
            )
            if Path(bright_frame).exists():
                first_brightness = _brightness(bright_frame)
        except Exception:
            pass

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
            try:
                results = _model(frame, conf=CONF_THRESH, verbose=False)
                for r in results:
                    for cls_id in (r.boxes.cls.tolist() if r.boxes else []):
                        total += OBJECT_SCORES.get(int(cls_id), 0)
            except Exception as e:
                log.debug(f"YOLO kare analiz hatasi: {e}")

    dyn_min = _dynamic_min_score(first_brightness)
    log.info(f"score_clip: toplam={total}, parlaklik={first_brightness:.0f}, esik={dyn_min}")
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

    # Her karede tespitleri say, her sınıf için en yüksek sayıyı tut
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
                    boxes = r.boxes
                    if not boxes:
                        continue
                    for cls_id, conf in zip(boxes.cls.tolist(), boxes.conf.tolist()):
                        cls_id = int(cls_id)
                        # Sadece %70+ güvenli tespitler TTS'e girer
                        if conf >= TTS_CONF_THRESH and cls_id in _TR_NAMES:
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


def _motion_score(frame_paths: list) -> float:
    """Ardışık kareler arası ortalama gri-piksel farkı (0=donuk, yüksek=hareketli).

    Akan trafik / yürüyen kalabalık → yüksek; boş/park/donuk → düşük. Görüntüyü
    160×90'a küçültüp |fark|.mean() alır. cv2 yoksa PIL+numpy; ikisi de yoksa
    -1 (nötr — skorlamaya dokunmaz)."""
    paths = [p for p in frame_paths if p and Path(p).exists()]
    if len(paths) < 2:
        return -1.0
    try:
        import numpy as np

        def _gray(p):
            try:
                import cv2
                img = cv2.imread(p, cv2.IMREAD_GRAYSCALE)
                if img is None:
                    raise ValueError("cv2 read None")
                return cv2.resize(img, (160, 90)).astype("float32")
            except Exception:
                from PIL import Image
                im = Image.open(p).convert("L").resize((160, 90))
                return np.asarray(im, dtype="float32")

        diffs = []
        prev = _gray(paths[0])
        for p in paths[1:]:
            cur = _gray(p)
            diffs.append(float(np.abs(cur - prev).mean()))
            prev = cur
        return (sum(diffs) / len(diffs)) if diffs else -1.0
    except Exception:
        return -1.0


def quick_check_frame(frame_path: str, eq_shift: float = 0.0) -> bool:
    """ÖNCEDEN ÇIKARILMIŞ tek kareyi değerlendir: CV (karanlık/boş) + YOLO + gökyüzü.

    Referer-bilir ön-eleme yolu: kareyi çağıran taraf (generic_recorder) Referer'lı
    çeker, biz sadece yerel dosyayı analiz ederiz — token'lı kameralarda da çalışır.
    Bu bir ÖN-FİLTRE'dir; asıl ZORUNLU kapı analyze_clip. Bu yüzden YOLO yoksa /
    kare yoksa True (geç) döner — fail-open.
    eq_shift: kare eq=brightness filtresiyle çekildiyse parlaklık düzeltmesi (0 = ham).
    """
    if not Path(frame_path).exists() or Path(frame_path).stat().st_size < 500:
        return True  # kare yok → geç (asıl kayıt/analiz eler)
    if not _load_model():
        return True
    cv_ok, _ = _cv_precheck(frame_path)
    if not cv_ok:
        log.info("Ön-eleme: CV karanlik/bos → ELENDI")
        return False
    try:
        results = _model(frame_path, conf=CONF_THRESH, verbose=False)
        score = 0
        for r in results:
            for cls_id in (r.boxes.cls.tolist() if r.boxes else []):
                score += OBJECT_SCORES.get(int(cls_id), 0)
    except Exception:
        return True
    score += _sky_bonus(frame_path)
    raw_brightness = max(0.0, _brightness(frame_path) - eq_shift)
    dyn_min = _dynamic_min_score(raw_brightness)
    passed = score >= dyn_min
    log.info(f"Ön-eleme: {score}p (esik:{dyn_min}, parlaklik:{raw_brightness:.0f}) "
             f"→ {'GECTI' if passed else 'ELENDI'}")
    return passed


def quick_check(stream_url: str, ffmpeg: str = "ffmpeg") -> bool:
    """Stream URL'den 1 kare çek (Referer YOK — Ankara/EGO yolu), quick_check_frame'e
    devret. Hata/kare-yok → True (geç). _VF_YOLO eq=brightness shift'i düzeltilir."""
    _NW = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
    with tempfile.TemporaryDirectory() as tmp:
        frame = os.path.join(tmp, "qc.jpg")
        cmd = [
            ffmpeg, "-y", "-tls_verify", "0", "-i", stream_url,
            "-frames:v", "1", "-ss", "2", "-q:v", "4", "-vf", _VF_YOLO, frame,
        ]
        try:
            subprocess.run(cmd, capture_output=True, timeout=15, **_NW)
        except Exception:
            return True  # timeout → geçir, asıl kayıtta anlaşılır
        return quick_check_frame(frame, eq_shift=13.0)


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


def analyze_clip(video_path, ffmpeg="ffmpeg", duration=30):
    """Tek geciste scoring + thumbnail. score_clip+best_frame yerine."""
    import subprocess as _sp, sys as _sys, tempfile as _tmp
    _NW = {"creationflags": _sp.CREATE_NO_WINDOW} if _sys.platform == "win32" else {}
    step = max(2, duration // 6)
    timestamps = [step, step*2, step*3, step*4, step*5]
    total = 0
    first_brightness = 128.0
    best_score = -1
    best_t = timestamps[len(timestamps)//2]
    model_ok = _load_model()
    motion_frames = []   # hareket skoru için tüm çıkarılan kareler

    with _tmp.TemporaryDirectory() as tmp:
        bright_frame = os.path.join(tmp, "bright.jpg")
        try:
            _sp.run([ffmpeg,"-y","-ss",str(step),"-i",video_path,
                     "-frames:v","1","-q:v","4","-vf",_VF_BRIGHT,bright_frame],
                    capture_output=True,timeout=10,**_NW)
            if Path(bright_frame).exists():
                first_brightness = _brightness(bright_frame)
        except Exception:
            pass

        for i, t in enumerate(timestamps):
            frame = os.path.join(tmp, "f{}.jpg".format(i))
            try:
                _sp.run([ffmpeg,"-y","-ss",str(t),"-i",video_path,
                         "-frames:v","1","-q:v","3","-vf",_VF_YOLO,frame],
                        capture_output=True,timeout=10,**_NW)
            except Exception:
                continue
            if not Path(frame).exists():
                continue
            motion_frames.append(frame)   # donuk/hareket ölçümü için (cv/yolo'dan bağımsız)
            cv_ok, _ = _cv_precheck(frame)
            if not cv_ok:
                continue
            frame_score = 0
            if model_ok:
                try:
                    results = _model(frame,conf=CONF_THRESH,verbose=False)
                    for r in results:
                        for cls_id in (r.boxes.cls.tolist() if r.boxes else []):
                            frame_score += OBJECT_SCORES.get(int(cls_id),0)
                    total += frame_score
                except Exception:
                    pass
            if frame_score > best_score:
                best_score = frame_score
                best_t = t

        # Hareket/aktivite (kareler hala mevcutken hesapla). YOLO "ne var"ı,
        # hareket "ne kadar canlı"yı verir → birlikte daha iyi "ilginç" sinyali.
        motion = _motion_score(motion_frames)

    motion_note = ""
    if motion >= 0:
        if motion < MOTION_FROZEN:
            total = 0          # dijital donuk → reddet
            motion_note = " hareket={:.1f}(DONUK→0)".format(motion)
        else:
            bonus = min(MOTION_BONUS_CAP, int(motion / MOTION_DIV))
            total += bonus     # aktif sahneye bonus (sakin sahne 0 bonus, ceza yok)
            motion_note = " hareket={:.1f}(+{})".format(motion, bonus)

    dyn_min = _dynamic_min_score(first_brightness)
    if not model_ok:
        total = 0   # ZORUNLU YOLO: model yüklenemedi → REDDET (fail-closed)
    log.info("analyze_clip: skor={} parlaklik={:.0f} esik={}{}".format(
        total,first_brightness,dyn_min,motion_note))

    thumb_path = str(Path(video_path).with_suffix(".jpg"))
    vf_t = "scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2"
    import subprocess as _sp2, sys as _sys2
    _NW2 = {"creationflags": _sp2.CREATE_NO_WINDOW} if _sys2.platform == "win32" else {}
    try:
        _sp2.run([ffmpeg,"-y","-ss",str(best_t),"-i",video_path,
                  "-frames:v","1","-q:v","2","-vf",vf_t,thumb_path],
                 capture_output=True,timeout=10,**_NW2)
        if Path(thumb_path).exists():
            log.info("Thumbnail t={}s skor={}".format(best_t,best_score))
            return total, dyn_min, thumb_path
    except Exception:
        pass
    return total, dyn_min, None
