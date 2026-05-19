"""
KameraShorts Live Debug Modülü
==============================
Her stream hatasının tam nedenini, hangi bileşende, kaçıncı saniyede olduğunu loglar.

HTTP debug sunucusu: http://localhost:9998/
  /status   → Anlık durum (kamera, fps, bitrate, heartbeat)
  /events   → Son 100 olay (JSON)
  /dts      → Segment sınırı DTS geçmişi
  /alerts   → Sadece uyarılar
"""
import json
import logging
import re
import threading
import time
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Optional

log = logging.getLogger("live.debug")

# ─────────────────────────────────────────────────────────────
# EventLogger — JSON-lines dosya + hafızada son 300 olay
# ─────────────────────────────────────────────────────────────

class EventLogger:
    def __init__(self, log_path: str):
        from pathlib import Path
        self._path = Path(log_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._recent: deque = deque(maxlen=300)

    def log(self, component: str, event: str, **kw):
        record = {
            "t": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            "component": component,
            "event": event,
            **{k: v for k, v in kw.items() if v is not None},
        }
        line = json.dumps(record, ensure_ascii=False)
        with self._lock:
            self._recent.append(record)
            try:
                with open(self._path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except Exception:
                pass
        level = (logging.WARNING if event in {
            "backward_dts", "frame_drop", "low_fps", "underrun",
            "timeout", "ffmpeg_error", "relay_expired", "large_gap",
            "broken_pipe", "relay_failed", "queue_empty"
        } else logging.INFO)
        log.log(level, "[%s] %s %s", component, event,
                " ".join(f"{k}={v}" for k, v in kw.items()))

    def recent(self, n: int = 100) -> list:
        with self._lock:
            return list(self._recent)[-n:]

    def alerts(self, n: int = 50) -> list:
        _ALERT = {
            "backward_dts", "frame_drop", "low_fps", "underrun",
            "timeout", "ffmpeg_error", "relay_expired", "large_gap",
            "broken_pipe", "relay_failed"
        }
        with self._lock:
            return [e for e in self._recent if e.get("event") in _ALERT][-n:]


# ─────────────────────────────────────────────────────────────
# DTSMonitor — Segment sınırlarında DTS takibi
# ─────────────────────────────────────────────────────────────

class DTSMonitor:
    """
    Her HLS segmenti kendi DTS zaman damgasıyla başlar.
    Birden fazla segment birleştirilince DTS geri gidebilir → paket düşer.
    Bu sınıf her segmentin başındaki ve sonundaki DTS'i takip eder,
    geri-giden atlamaları loglar.
    """

    def __init__(self, ev: EventLogger):
        self._ev = ev
        self._last_end_dts: Optional[float] = None
        self._last_seg_dur: float = 0.0
        self._jumps: deque = deque(maxlen=50)
        self._seg_total = 0
        self._lock = threading.Lock()

    def on_segment_start(self, camera: str, seg_idx: int,
                         first_dts: Optional[float] = None):
        """Yeni segment pipe'a yazılmaya başladığında çağır."""
        with self._lock:
            self._seg_total += 1
            if first_dts is None or self._last_end_dts is None:
                return
            expected = self._last_end_dts + self._last_seg_dur
            diff = first_dts - expected
            entry = {
                "camera": camera, "seg": seg_idx,
                "prev_end": round(self._last_end_dts, 4),
                "new_first": round(first_dts, 4),
                "expected": round(expected, 4),
                "diff_s": round(diff, 4),
            }
            if diff < -0.3:
                self._jumps.append(entry)
                self._ev.log("dts_monitor", "backward_dts", **entry)
            elif diff > 5.0:
                self._ev.log("dts_monitor", "large_gap", **entry)

    def on_segment_end(self, last_dts: float, seg_duration: float):
        """Segment pipe'a yazılıp bittikten sonra çağır."""
        with self._lock:
            self._last_end_dts = last_dts
            self._last_seg_dur = seg_duration

    def reset(self, reason: str = "camera_switch"):
        with self._lock:
            self._last_end_dts = None
            self._last_seg_dur = 0.0
        self._ev.log("dts_monitor", "reset", reason=reason)

    def recent_jumps(self, n: int = 20) -> list:
        with self._lock:
            return list(self._jumps)[-n:]


# ─────────────────────────────────────────────────────────────
# FFmpegStderrParser — fps/bitrate/drop/dup ayrıştırıcı
# ─────────────────────────────────────────────────────────────

class FFmpegStderrParser:
    """
    FFmpeg progress satırlarını parse eder:
      frame= 238 fps= 25 q=28.0 size= 3072kB time=00:00:09.52
      bitrate=2641.5kbits/s dup=6 drop=0 speed=1.0x

    Bilinen zararsız satırları sessizce yok sayar.
    drop > 0 veya fps < 5 olduğunda uyarı loglar.
    """

    _PROG_RE = re.compile(
        r"frame=\s*(\d+)\s+fps=\s*([\d.]+).*?"
        r"size=\s*(?:(\d+)kB|N/A)\s+time=([\d:]+\.?\d*)\s+"
        r"bitrate=\s*(?:([\d.]+)kbits/s|N/A)"
        r"(?:.*?dup=(\d+))?"
        r"(?:.*?drop=(\d+))?"
        r"(?:.*?speed=\s*([\d.]+)x)?",
        re.DOTALL
    )

    # Sessizce yok sayılacak kalıplar
    _NOISE = [
        r"Packet corrupt",
        r"Last message repeated",
        r"deprecated pixel format",
        r"Estimating duration",
        r"DTS .*, resampling",
        r"non monotonous",
        r"Application provided invalid",
        r"Past duration",
        r"max_analyze_duration",
        r"data is not aligned",
        r"swscaler",
    ]
    _NOISE_RE = re.compile("|".join(_NOISE), re.IGNORECASE)

    def __init__(self, component: str, ev: EventLogger, on_stats=None):
        self.component = component
        self._ev = ev
        self._on_stats = on_stats  # callback(dict) → DebugCollector
        self._last: dict = {}
        self._lock = threading.Lock()

    def feed(self, line: str):
        line = line.strip()
        if not line:
            return
        if self._NOISE_RE.search(line):
            return

        m = self._PROG_RE.search(line)
        if m:
            # grup 3=size_kb(veya None), 5=bitrate(veya None), 6=dup, 7=drop, 8=speed
            stats = {
                "frame":        int(m.group(1)),
                "fps":          float(m.group(2)),
                "size_kb":      int(m.group(3)) if m.group(3) else 0,
                "bitrate_kbps": float(m.group(5)) if m.group(5) else 0.0,
                "dup":          int(m.group(6) or 0),
                "drop":         int(m.group(7) or 0),
                "speed":        float(m.group(8)) if m.group(8) else 0.0,
            }
            with self._lock:
                prev_drop = self._last.get("drop", 0)
                prev_fps  = self._last.get("fps", 25.0)
                self._last = stats

            if self._on_stats:
                self._on_stats(stats)

            if stats["drop"] > prev_drop:
                self._ev.log(self.component, "frame_drop",
                             drop=stats["drop"], fps=stats["fps"],
                             bitrate_kbps=stats["bitrate_kbps"])
            if stats["fps"] < 5.0 and prev_fps >= 5.0:
                self._ev.log(self.component, "low_fps",
                             fps=stats["fps"],
                             bitrate_kbps=stats["bitrate_kbps"])
            # speed < 0.9 → gerçek zamanlı altında, YouTube donma görebilir
            if stats["speed"] > 0 and stats["speed"] < 0.9:
                self._ev.log(self.component, "below_realtime",
                             speed=stats["speed"], fps=stats["fps"])
            return

        lower = line.lower()
        if any(x in lower for x in [
            "error", "connection refused", "broken pipe",
            "no route to host", "connection timed out", "no such file"
        ]):
            self._ev.log(self.component, "ffmpeg_error", msg=line[:300])
        elif "warning" in lower and "deprecated" not in lower:
            self._ev.log(self.component, "ffmpeg_warning", msg=line[:200])

    def current(self) -> dict:
        with self._lock:
            return dict(self._last)


# ─────────────────────────────────────────────────────────────
# BitrateMonitor — 10 saniyelik kayan pencere
# ─────────────────────────────────────────────────────────────

class BitrateMonitor:
    def __init__(self, target_kbps: int, ev: EventLogger):
        self._target = target_kbps
        self._ev = ev
        self._samples: deque = deque(maxlen=10)
        self._last_alert = 0.0
        self._lock = threading.Lock()

    def record(self, kbps: float):
        with self._lock:
            self._samples.append(kbps)
            if len(self._samples) >= 5:
                avg = sum(self._samples) / len(self._samples)
                if avg < self._target * 0.5 and (time.time() - self._last_alert) > 30:
                    self._last_alert = time.time()
                    self._ev.log("bitrate", "underrun",
                                 avg_kbps=round(avg, 1),
                                 target_kbps=self._target,
                                 pct=round(avg / self._target * 100))

    def avg(self) -> float:
        with self._lock:
            if not self._samples:
                return 0.0
            return round(sum(self._samples) / len(self._samples), 1)


# ─────────────────────────────────────────────────────────────
# RelayCountdown — Ankara 40sn TTL takibi
# ─────────────────────────────────────────────────────────────

class RelayCountdown:
    """
    Relay başlatıldıktan 40 saniye sonra sona erer.
    33. saniyede proaktif yenileme önerir (7sn marj).
    """

    def __init__(self, ttl: int = 40, renew_at: int = 33):
        self._ttl = ttl
        self._renew_at = renew_at
        self._start: Optional[float] = None
        self._lock = threading.Lock()

    def start(self):
        with self._lock:
            self._start = time.time()

    def age(self) -> float:
        with self._lock:
            return (time.time() - self._start) if self._start else 0.0

    def needs_renewal(self) -> bool:
        return self.age() >= self._renew_at

    def expired(self) -> bool:
        return self.age() >= self._ttl

    def reset(self):
        with self._lock:
            self._start = None


# ─────────────────────────────────────────────────────────────
# TransitionTimer — Kamera geçiş süresi ölçer
# ─────────────────────────────────────────────────────────────

class TransitionTimer:
    def __init__(self, ev: EventLogger):
        self._ev = ev
        self._t0: Optional[float] = None
        self._from = ""
        self._to = ""
        self._milestones: list = []

    def begin(self, from_cam: str, to_cam: str):
        self._t0 = time.time()
        self._from = from_cam
        self._to = to_cam
        self._milestones = []
        self._ev.log("transition", "begin", from_cam=from_cam, to_cam=to_cam)

    def mark(self, milestone: str):
        if self._t0:
            elapsed = round(time.time() - self._t0, 3)
            self._milestones.append({"name": milestone, "elapsed_s": elapsed})
            self._ev.log("transition", "milestone",
                         name=milestone, elapsed_s=elapsed, to_cam=self._to)

    def done(self):
        if self._t0:
            total = round(time.time() - self._t0, 3)
            self._ev.log("transition", "complete",
                         from_cam=self._from, to_cam=self._to,
                         total_s=total, milestones=self._milestones,
                         ok=(total < 6.0))
            self._t0 = None


# ─────────────────────────────────────────────────────────────
# PipelineHeartbeat — Bileşen sağlık izleme
# ─────────────────────────────────────────────────────────────

class PipelineHeartbeat:
    """
    Her bileşen düzenli olarak beat() çağırır.
    timeout süresi boyunca sessiz kalan bileşen için uyarı loglanır.
    """

    def __init__(self, ev: EventLogger, timeout: float = 15.0):
        self._ev = ev
        self._timeout = timeout
        self._beats: dict[str, float] = {}
        self._alerted: set[str] = set()
        self._lock = threading.Lock()
        threading.Thread(target=self._watch, daemon=True).start()

    def beat(self, name: str):
        with self._lock:
            self._beats[name] = time.time()
            self._alerted.discard(name)

    def _watch(self):
        while True:
            time.sleep(5)
            try:
                now = time.time()
                with self._lock:
                    for name, last in list(self._beats.items()):
                        gap = now - last
                        if gap > self._timeout and name not in self._alerted:
                            self._alerted.add(name)
                            self._ev.log("heartbeat", "timeout",
                                         component=name, silent_s=round(gap, 1))
            except Exception:
                pass

    def status(self) -> dict:
        now = time.time()
        with self._lock:
            return {n: {"last_beat_ago_s": round(now - t, 1)}
                    for n, t in self._beats.items()}


# ─────────────────────────────────────────────────────────────
# DebugCollector — Merkezi hub + HTTP sunucusu
# ─────────────────────────────────────────────────────────────

class DebugCollector:
    """
    Tüm debug bileşenlerini bir araya toplar.
    HTTP sunucusu: http://0.0.0.0:{debug_port}/
    """

    def __init__(self, cfg: dict):
        debug_log = cfg.get("debug_log", "/var/log/kamerashorts-live-debug.log")
        self.ev        = EventLogger(debug_log)
        self.dts       = DTSMonitor(self.ev)
        self.bitrate   = BitrateMonitor(cfg.get("bitrate", 2500), self.ev)
        self.relay_cd  = RelayCountdown()
        self.transition = TransitionTimer(self.ev)
        self.heartbeat = PipelineHeartbeat(self.ev)

        self._cam = ""
        self._bcast_stats: dict = {}
        self._stream_start = time.time()
        self._lock = threading.Lock()

        port = cfg.get("debug_port", 9998)
        self._start_http(port)
        self.ev.log("debug", "started", port=port, log=debug_log)

    # ── Public API ───────────────────────────────────────────

    def set_camera(self, name: str):
        with self._lock:
            self._cam = name
        self.heartbeat.beat("orchestrator")

    def on_broadcaster_stats(self, stats: dict):
        with self._lock:
            self._bcast_stats = stats
        if "bitrate_kbps" in stats:
            self.bitrate.record(stats["bitrate_kbps"])
        self.heartbeat.beat("broadcaster")

    def make_broadcaster_parser(self) -> FFmpegStderrParser:
        return FFmpegStderrParser(
            "broadcaster", self.ev, on_stats=self.on_broadcaster_stats
        )

    def make_feeder_parser(self) -> FFmpegStderrParser:
        return FFmpegStderrParser("feeder", self.ev)

    def make_transition_parser(self) -> FFmpegStderrParser:
        return FFmpegStderrParser("transition", self.ev)

    def status(self) -> dict:
        with self._lock:
            return {
                "uptime_s":         round(time.time() - self._stream_start, 1),
                "current_camera":   self._cam,
                "broadcaster":      dict(self._bcast_stats),
                "bitrate_avg_kbps": self.bitrate.avg(),
                "relay_age_s":      round(self.relay_cd.age(), 1),
                "heartbeats":       self.heartbeat.status(),
                "recent_dts_jumps": self.dts.recent_jumps(5),
            }

    # ── HTTP Server ──────────────────────────────────────────

    def _start_http(self, port: int):
        collector = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                routes = {
                    "/":        lambda: {"endpoints": ["/status", "/events", "/dts", "/alerts"]},
                    "/status":  lambda: collector.status(),
                    "/events":  lambda: {"events": collector.ev.recent(100)},
                    "/dts":     lambda: {"jumps": collector.dts.recent_jumps()},
                    "/alerts":  lambda: {"alerts": collector.ev.alerts()},
                }
                fn = routes.get(self.path)
                if not fn:
                    self.send_response(404)
                    self.end_headers()
                    return
                body = json.dumps(fn(), ensure_ascii=False, indent=2).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        srv = HTTPServer(("0.0.0.0", port), Handler)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.name = "debug-http"
        t.start()
