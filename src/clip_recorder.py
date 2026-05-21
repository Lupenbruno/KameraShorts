"""Captures HLS clips from Ankara bus cameras."""
import logging
import os
import subprocess
import shutil
import sys
import tempfile
import threading
import time
import requests
from datetime import datetime
from pathlib import Path
import json as _json
# Lazy YOLO subprocess wrappers — RAM tasarrufu


def quick_check(stream_url: str, ffmpeg_path: str) -> bool:
    """YOLO subprocess ÖN-kontrol (1 kare). Bu sadece bir ÖN-FİLTRE'dir —
    asıl ZORUNLU kapı kayıt-sonrası analyze_clip'tir. Bu yüzden burada hata/
    sonuç-yok durumunda True (geç) dönülür (fail-open): referer-gated veya
    EGO stream'lerde tek kare çekilemese bile kayıt denenir; taranmamış klip
    yine de analyze_clip kapısından geçemez."""
    try:
        r = subprocess.run(
            [sys.executable, "-m", "src.yolo_runner", "quickcheck",
             "--url", stream_url, "--ffmpeg", ffmpeg_path],
            capture_output=True, timeout=30, check=False,
        )
        out = r.stdout.decode("utf-8", errors="replace")
        for line in out.splitlines():
            if line.startswith("RESULT:"):
                d = _json.loads(line[7:])
                return bool(d.get("passed", True))
    except Exception:
        pass
    return True


def analyze_clip(clip_path: str, ffmpeg_path: str,
                 duration: int = 40) -> tuple[int, int, str]:
    """YOLO subprocess klip analizi (skor + threshold + thumb).

    ZORUNLU YOLO KAPISI (fail-CLOSED): subprocess çalışmaz / RESULT satırı
    gelmezse score=0 döner → çağıran 'score < threshold' görür → klip REDDEDİLİR.
    Böylece taranmamış hiçbir klip yayına çıkmaz. (yolo_runner, modeli
    yükleyemezse ai_filter de total=0 verir; iki katman da fail-closed.)"""
    try:
        r = subprocess.run(
            [sys.executable, "-m", "src.yolo_runner", "analyze",
             "--clip", clip_path, "--ffmpeg", ffmpeg_path,
             "--duration", str(duration)],
            capture_output=True, timeout=120, check=False,
        )
        out = r.stdout.decode("utf-8", errors="replace")
        for line in out.splitlines():
            if line.startswith("RESULT:"):
                d = _json.loads(line[7:])
                return (int(d.get("score", 0)),
                        int(d.get("threshold", 4)),
                        d.get("thumb", ""))
    except Exception:
        pass
    return 0, 4, ""   # fail-closed: YOLO sonuç vermedi → reddet


log = logging.getLogger("kamerashorts")

_NW = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}

RELAY_START_URL = "https://seyret.ankara.bel.tr/api/relay/start/{dvr}?provider={provider}"


class _RelayRenewer:
    """
    EGO relay TTL renewal — kayıt sırasında relay'in expire olmasını önler.

    Önemli not (önceki bug): ClipRecorder relay'i başlatıp 30s bekliyordu,
    sonra segment indirme başlıyordu. EGO relay TTL=40s, renewal yoksa
    kayıt sırasında relay sönüyordu → segment 404 → "Timeout" hatası.
    Bu thread her 33 saniyede bir relay POST atarak TTL'i yeniler.
    """
    INTERVAL = 33  # TTL 40s, 7s marjla yenile

    def __init__(self, session, dvr: str, provider: str = "ego"):
        self._session = session
        self._dvr = dvr
        self._provider = provider
        self._stop = threading.Event()

    def start(self):
        threading.Thread(target=self._loop, daemon=True,
                         name=f"relay-renew-{self._dvr[:6]}").start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.wait(self.INTERVAL):
            try:
                url = RELAY_START_URL.format(dvr=self._dvr, provider=self._provider)
                self._session.post(url, timeout=10)
                log.debug(f"[relay] {self._dvr[:6]} yenilendi")
            except Exception as e:
                log.warning(f"[relay] {self._dvr[:6]} yenileme hatası: {e}")


class ClipRecorder:
    def __init__(self, config: dict):
        self.duration = config["schedule"]["clip_duration"]
        self.clips_dir = Path(config["paths"]["clips_dir"])
        self.clips_dir.mkdir(parents=True, exist_ok=True)
        _ff = config.get("ffmpeg_path") or ""
        if _ff and not Path(_ff).exists():
            _ff = ""
        self.ffmpeg = _ff or shutil.which("ffmpeg") or "ffmpeg"
        self._session = requests.Session()
        self._session.headers["User-Agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        self._session.headers["Referer"] = "https://seyret.ankara.bel.tr/"
        self._session.verify = False  # onstream.ankara.bel.tr self-signed cert

    def _start_relay(self, vehicle: dict) -> bool:
        """Relay'i başlat ve m3u8 erişilebilir olana kadar bekle.

        Stream zaten canlıysa relay'i tetiklemez (resetlemekten kaçın).
        """
        stream_url = vehicle["stream_url"]
        dvr = vehicle.get("dvr_serial_number", "")
        provider = vehicle.get("source", "ego")

        # Önce stream'in zaten canlı olup olmadığını kontrol et
        try:
            r = self._session.get(stream_url, timeout=5)
            if r.status_code == 200 and "#EXTM3U" in r.text:
                return True  # Zaten canlı, relay'i tetiklemeye gerek yok
        except Exception:
            pass

        # Canlı değilse relay'i başlat
        if not dvr:
            return False
        try:
            url = RELAY_START_URL.format(dvr=dvr, provider=provider)
            self._session.post(url, timeout=10)
            # m3u8 hazır olana kadar bekle (max 30 saniye)
            for _ in range(10):
                time.sleep(3)
                try:
                    r = self._session.get(stream_url, timeout=5)
                    if r.status_code == 200 and "#EXTM3U" in r.text:
                        return True
                except Exception:
                    pass
            return False
        except Exception:
            return False

    def _download_segments(self, stream_url: str, target_duration: int,
                           tmp_dir: str) -> list[str]:
        """
        m3u8 playlist'ten segmentleri direkt indir.
        onstream.ankara.bel.tr gibi uzun playlist geçmişi olan sunucularda
        inner loop target_duration dolduğunda hemen durur.
        """
        base = stream_url.rsplit("/", 1)[0] + "/"
        seen = set()
        files = []
        total_duration = 0.0
        deadline = time.time() + target_duration + 30  # max bekleme
        new_this_round = 0  # bu turda gelen yeni segment sayisi

        while total_duration < target_duration and time.time() < deadline:
            new_this_round = 0
            try:
                r = self._session.get(stream_url, timeout=8)
                if r.status_code != 200:
                    time.sleep(2)
                    continue

                lines = r.text.strip().split("\n")
                i = 0
                while i < len(lines):
                    line = lines[i].strip()
                    if line.startswith("#EXTINF:"):
                        try:
                            seg_dur = float(line.split(":")[1].split(",")[0])
                        except Exception:
                            seg_dur = 2.0
                        if i + 1 < len(lines):
                            seg_name = lines[i + 1].strip()
                            if seg_name and seg_name not in seen and not seg_name.startswith("#"):
                                seen.add(seg_name)
                                seg_url = seg_name if seg_name.startswith("http") else base + seg_name
                                out_file = os.path.join(tmp_dir, f"seg_{len(files):04d}.ts")
                                try:
                                    sr = self._session.get(seg_url, timeout=10)
                                    if sr.status_code == 200 and len(sr.content) > 1000:
                                        with open(out_file, "wb") as f:
                                            f.write(sr.content)
                                        files.append(out_file)
                                        total_duration += seg_dur
                                        new_this_round += 1
                                        # Yeterli süre toplandıysa iç döngüyü hemen kır
                                        if total_duration >= target_duration:
                                            break
                                except Exception:
                                    pass
                        i += 2
                    else:
                        i += 1
            except Exception:
                time.sleep(1)
                continue

            # Bu turda hiç yeni segment gelmediyse biraz bekle
            if new_this_round == 0:
                time.sleep(2)

        return files

    def record(self, vehicle: dict, capture_time: datetime) -> str | None:
        device_id = vehicle["device_id"]
        plate = vehicle.get("license_plate", device_id).replace(" ", "_")
        ts = capture_time.strftime("%Y%m%d_%H%M")
        out_path = self.clips_dir / f"{plate}_{ts}.mp4"

        stream_url = vehicle["stream_url"]
        dvr = vehicle.get("dvr_serial_number", "")
        provider = vehicle.get("source", "ego")

        if not self._start_relay(vehicle):
            return None

        # RELAY TTL renewal thread başlat — kayıt sırasında relay sönmesin
        relay_renewer = None
        if dvr:
            relay_renewer = _RelayRenewer(self._session, dvr, provider)
            relay_renewer.start()
            log.info(f"[{plate}] Relay renewal aktif (TTL 40s, 33s'de bir yenileme)")

        # Relay açıkken 1 kare çek, YOLO ile ön kontrol
        if not quick_check(stream_url, self.ffmpeg):
            log.warning(f"[{plate}] Ön YOLO: zemin/damper/karanlık, atlanıyor")
            if relay_renewer:
                relay_renewer.stop()
            return None

        try:
            with tempfile.TemporaryDirectory() as tmp_dir:
                # Segmentleri direkt indir
                segments = self._download_segments(stream_url, self.duration, tmp_dir)

                if not segments:
                    return None

                # concat listesi oluştur
                concat_file = os.path.join(tmp_dir, "concat.txt")
                with open(concat_file, "w") as f:
                    for seg in segments:
                        f.write(f"file '{seg}'\n")

                # ffmpeg ile concat → encode
                # Format: 1080×1920 dikey canvas (Shorts kategorisi için zorunlu)
                # İçerik: landscape kamera görüntüsü ortada (yatay korunur, kırpılmaz)
                # Arka plan: aynı içeriğin BLUR'LU + zoom'lu versiyonu (profesyonel görünüm)
                # YouTube otomatik olarak Shorts tab'ına alır (canvas 9:16).
                vf = (
                    "split=2[v1][v2];"
                    # Background: scale up + crop to 1080x1920 + blur
                    "[v1]scale=1080:1920:force_original_aspect_ratio=increase,"
                          "crop=1080:1920,boxblur=20:1[bg];"
                    # Foreground: landscape fitted to width 1080, height auto (16:9 → 1080x608)
                    "[v2]scale=1080:-2:force_original_aspect_ratio=decrease[fg];"
                    # Overlay: foreground centered on blurred background
                    "[bg][fg]overlay=(W-w)/2:(H-h)/2"
                )
                cmd = [
                    self.ffmpeg, "-y",
                    "-f", "concat", "-safe", "0",
                    "-i", concat_file,
                    "-t", str(self.duration),
                    "-c:v", "libx264",
                    "-preset", "fast",
                    "-crf", "23",
                    "-c:a", "aac",
                    "-movflags", "+faststart",
                    "-vf", vf,
                    str(out_path)
                ]
                result = subprocess.run(cmd, capture_output=True,
                                        timeout=self.duration + 90, **_NW)

                if result.returncode == 0 and out_path.exists():
                    if out_path.stat().st_size > 100_000:
                        if self._is_frozen(str(out_path)):
                            log.warning(f"[{plate}] Donuk video, atlanıyor")
                            out_path.unlink(missing_ok=True)
                            return None
                        # Post-kayıt YOLO kontrolü — 5 kare ile tam analiz
                        score, dyn_min, _ = analyze_clip(str(out_path), self.ffmpeg, self.duration)
                        if score < dyn_min:
                            log.warning(f"[{plate}] YOLO post-kayıt elendi, atlanıyor")
                            out_path.unlink(missing_ok=True)
                            return None
                        return str(out_path)
                return None

        except subprocess.TimeoutExpired:
            log.warning(f"[{plate}] Timeout")
            out_path.unlink(missing_ok=True)
            return None
        except Exception as e:
            log.error(f"[{plate}] Kayıt hatası: {e}")
            return None
        finally:
            if relay_renewer:
                relay_renewer.stop()

    def _is_frozen(self, video_path: str) -> bool:
        """İki kontrol: düşük bitrate VEYA aynı kareler → donuk."""
        return self._check_bitrate(video_path) or self._check_frames(video_path)

    def _check_bitrate(self, video_path: str) -> bool:
        """Saniye başına 60KB altı = donuk."""
        try:
            size_kb = Path(video_path).stat().st_size / 1024
            return (size_kb / self.duration) < 60
        except Exception:
            return False

    def _check_frames(self, video_path: str) -> bool:
        """5 farkli saniyeden kare cek, neredeyse hepsi ayniysa donuk."""
        import hashlib
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
