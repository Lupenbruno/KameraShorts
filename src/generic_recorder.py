"""Generic HLS kamera kaydedici — herhangi bir şehir için (havuz kameraları).

- record_url(): havuz şehirleri için. Referer header + tls_verify 0 ile m3u8'den
  40s kaydeder, 1080×1920 dikey Shorts canvas (blur arka plan + ortalı landscape).
- YOLO taraması ZORUNLU (fail-closed): kayıt-sonrası analiz GEÇMEZSE klip silinir.
  YOLO out-of-process (src.clip_recorder.analyze_clip → yolo_runner subprocess) —
  daemon RAM'ini kirletmez; modeli yüklenemezse score=0 → klip reddedilir.
"""
import hashlib
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

# Out-of-process, FAIL-CLOSED YOLO sarmalayıcısı (RAM dostu — model subprocess'te).
from src.clip_recorder import analyze_clip

log = logging.getLogger("kamerashorts")
_NW = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}

# 1080×1920 dikey Shorts: blur'lu zoom arka plan + ortada landscape (kırpılmaz).
SHORTS_VF = (
    "split=2[v1][v2];"
    "[v1]scale=1080:1920:force_original_aspect_ratio=increase,"
        "crop=1080:1920,boxblur=20:1[bg];"
    "[v2]scale=1080:-2:force_original_aspect_ratio=decrease[fg];"
    "[bg][fg]overlay=(W-w)/2:(H-h)/2"
)


def _safe_name(name: str) -> str:
    s = "".join(ch for ch in (name or "") if ch.isalnum())
    return (s[:28] or "cam")


class GenericRecorder:
    def __init__(self, clips_dir: str, duration: int, ffmpeg_path: str = None,
                 vertical: bool = False):
        self.duration = duration
        self.clips_dir = Path(clips_dir)
        self.clips_dir.mkdir(parents=True, exist_ok=True)
        _ff = ffmpeg_path or ""
        if _ff and not Path(_ff).exists():
            _ff = ""
        self.ffmpeg = _ff or shutil.which("ffmpeg") or "ffmpeg"
        self.vertical = vertical

    def _frame_ok(self, frame_path: str) -> bool:
        """yolo_runner framecheck (out-of-process, RAM dostu). Önceden çıkarılmış
        kareyi ön-eler. Sonuç yoksa True (geç — asıl ZORUNLU kapı analyze_clip).
        'passed: false' (karanlık/boş) → kayıt yapılmadan atlanır (hız)."""
        import json as _json
        try:
            r = subprocess.run(
                [sys.executable, "-m", "src.yolo_runner", "framecheck",
                 "--frame", frame_path],
                capture_output=True, timeout=40, **_NW)
            out = r.stdout.decode("utf-8", errors="replace")
            for line in out.splitlines():
                if line.startswith("RESULT:"):
                    return bool(_json.loads(line[7:]).get("passed", True))
        except Exception:
            pass
        return True   # ön-eleme belirsiz → kaydet, final YOLO eler

    # ── Havuz şehirleri: taze m3u8 → 40s dikey Shorts klibi ──────────────
    def record_url(self, stream_url: str, cam_name: str,
                   capture_time: datetime, headers: dict = None) -> str | None:
        """stream_url'den (resolver'dan gelen taze m3u8) Shorts klibi kaydet.

        İKİ FAZLI (hız + ölü kameraları hızlı eleme için):
          FAZ 1 — yakalama: -c copy ile 40s indir (ağ-bağımlı; ölü/yavaş kamera
                  ~10-70s'de düşer, ağır blur encode'una hiç girmez).
          FAZ 2 — canvas: YEREL dosyadan 1080×1920 dikey Shorts (ultrafast;
                  ağ beklemesi yok, CPU-bağımlı, deterministik).
        headers: {'Referer': '...'} — tvkur/embed kameraları için zorunlu (canlı
        oynatımda content.tvkur.com Referer ister; -headers ile geçilir).
        ZORUNLU YOLO (fail-closed) en sonda yerel dosyaya uygulanır. Döner: path|None.
        """
        ts = capture_time.strftime("%Y%m%d_%H%M%S")
        out_path = self.clips_dir / f"{_safe_name(cam_name)}_{ts}.mp4"

        # Input opsiyonları (-i'den ÖNCE): SSL atla + 10s read timeout + Referer/header
        in_opts = ["-tls_verify", "0", "-rw_timeout", "10000000"]
        hdr_lines = "".join(f"{k}: {v}\r\n" for k, v in (headers or {}).items() if v)
        if hdr_lines:
            in_opts += ["-headers", hdr_lines]

        # FAZ 0 — Referer'lı tek-kare ÖN-ELEME (kaydetmeden hızlı karar).
        # Ölü/karanlık/boş kamera burada ~10-20s'de düşer; 40s yakalama + ağır
        # canvas encode'una HİÇ girmez. Asıl ZORUNLU kapı yine sondaki analyze_clip.
        with tempfile.TemporaryDirectory() as pre_dir:
            pre = os.path.join(pre_dir, "pre.jpg")
            pcmd = (
                [self.ffmpeg, "-y", "-hide_banner", "-loglevel", "error"]
                + in_opts
                + ["-i", stream_url, "-frames:v", "1", "-q:v", "4",
                   "-vf", "scale=640:-2", pre]
            )
            try:
                subprocess.run(pcmd, capture_output=True, timeout=20, **_NW)
            except subprocess.TimeoutExpired:
                log.info(f"[{cam_name}] ön-kare timeout (ölü/yavaş) — atla")
                return None
            if not os.path.exists(pre) or os.path.getsize(pre) < 500:
                log.info(f"[{cam_name}] ön-kare alınamadı (ölü) — atla")
                return None
            if not self._frame_ok(pre):
                log.info(f"[{cam_name}] ön-eleme: karanlık/boş — atla (kayıt yok)")
                return None

        try:
            with tempfile.TemporaryDirectory() as tmp_dir:
                raw = os.path.join(tmp_dir, "cap.ts")
                # FAZ 1 — hızlı yakalama (canlı HLS realtime → ~40-50s; ölü hızlı düşer)
                cap_cmd = (
                    [self.ffmpeg, "-y", "-hide_banner", "-loglevel", "error"]
                    + in_opts
                    + ["-i", stream_url, "-t", str(self.duration),
                       "-c", "copy", "-f", "mpegts", raw]
                )
                try:
                    r = subprocess.run(cap_cmd, capture_output=True,
                                       timeout=self.duration + 35, **_NW)
                except subprocess.TimeoutExpired:
                    log.info(f"[{cam_name}] yakalama timeout — atla")
                    return None
                if (r.returncode != 0 or not os.path.exists(raw)
                        or os.path.getsize(raw) < 200_000):
                    err = (r.stderr[-160:].decode("utf-8", errors="replace")
                           if r.stderr else "")
                    log.info(f"[{cam_name}] yakalama başarısız: {err[:140]}")
                    return None

                # FAZ 2 — yerel dosyadan dikey Shorts canvas (ultrafast; audio/CTA
                # adımları zaten tekrar encode edecek, burada hız önemli).
                cv_cmd = [
                    self.ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                    "-i", raw, "-t", str(self.duration),
                    "-c:v", "libx264", "-preset", "ultrafast", "-crf", "25",
                    "-c:a", "aac", "-movflags", "+faststart",
                    "-vf", SHORTS_VF, str(out_path),
                ]
                try:
                    r2 = subprocess.run(cv_cmd, capture_output=True,
                                        timeout=self.duration * 4, **_NW)
                except subprocess.TimeoutExpired:
                    log.warning(f"[{cam_name}] canvas timeout")
                    out_path.unlink(missing_ok=True)
                    return None
                if (r2.returncode != 0 or not out_path.exists()
                        or out_path.stat().st_size < 200_000):
                    log.info(f"[{cam_name}] canvas başarısız")
                    out_path.unlink(missing_ok=True)
                    return None

            # Donuk (tek kare takılı) kontrolü — yerel dosya
            if self._check_frames(str(out_path)):
                log.warning(f"[{cam_name}] Donuk video, atlanıyor")
                out_path.unlink(missing_ok=True)
                return None
            # ZORUNLU kayıt-sonrası YOLO (fail-closed: subprocess çalışmazsa score=0)
            score, dyn_min, _ = analyze_clip(str(out_path), self.ffmpeg, self.duration)
            if score < dyn_min:
                log.warning(f"[{cam_name}] YOLO elendi (skor {score}<{dyn_min}), atlanıyor")
                out_path.unlink(missing_ok=True)
                return None
            return str(out_path)
        except Exception as e:
            log.error(f"[{cam_name}] Kayıt hatası: {e}")
            out_path.unlink(missing_ok=True)
            return None

    # ── Eski API (config kamera dict'i) — havuz dışı kullanım için korundu ─
    def record(self, camera: dict, capture_time: datetime) -> str | None:
        cam_id = camera.get("id", camera.get("name", "cam"))
        out = self.record_url(
            camera["stream_url"], str(cam_id), capture_time,
            headers=camera.get("headers"))
        return out

    # ── Donukluk tespiti ─────────────────────────────────────────────────
    def _check_frames(self, video_path: str) -> bool:
        """5 farkli saniyeden kare cek, neredeyse hepsi ayniysa donuk."""
        try:
            hashes = []
            step = max(1, self.duration // 6)
            with tempfile.TemporaryDirectory() as tmp_dir:
                for i, t in enumerate(range(step, self.duration, step)):
                    frame = os.path.join(tmp_dir, "fr{}.jpg".format(i))
                    cmd = [self.ffmpeg, "-y", "-ss", str(t), "-i", video_path,
                           "-frames:v", "1", "-q:v", "5", frame]
                    subprocess.run(cmd, capture_output=True, timeout=10, **_NW)
                    if Path(frame).exists():
                        hashes.append(hashlib.md5(Path(frame).read_bytes()).hexdigest())
            if len(hashes) < 3:
                return False
            most_common = max(set(hashes), key=hashes.count)
            return hashes.count(most_common) >= len(hashes) - 1
        except Exception:
            return False
