#!/usr/bin/env python3
"""
KameraShorts Live Dashboard
============================
Kullanım: python3 live_dashboard.py --log /var/log/kamerashorts-live.log --port 8080
"""

import argparse
import json
import re
import subprocess
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Optional

# ─── State ──────────────────────────────────────────────────────────────────

class StreamState:
    def __init__(self, log_path: str):
        self.log_path   = log_path
        self._lock      = threading.Lock()
        self.service_running = False
        self.start_time: Optional[str] = None
        self.speed      = 0.0
        self.fps        = 0.0
        self.frame      = 0
        self.streaming_batch = ""
        self.building_batch_id: Optional[int] = None
        self.city_progress: dict = {}   # {city: {dur, target, status, pct}}
        self.batch_history: list = []   # [{id, name, size_mb, build_seconds}]
        self.batch_queued: int  = 0
        self.recent_logs: deque = deque(maxlen=80)
        self.speed_history: deque = deque(maxlen=120)  # (ts, speed) son 6 dk
        self._last_size = 0

        # ── Aktivite tespiti (operatör "şu an ne oluyor" görsün) ──────────
        self.current_action: str = "—"              # tek satır insan-okur özet
        self.transcode_active: Optional[dict] = None # {city, started_ts, seg_count}
        self.concat_active: bool = False             # ffmpeg concat-remux çalışıyor mu
        self.collector_phase_active: bool = False    # HLS indirme fazı aktif mi

        # ── Stream sağlığı sayaçları ──────────────────────────────────────
        self.crash_count: int = 0                    # FFmpeg çöküş sayısı (oturum)
        self.watchdog_count: int = 0                 # watchdog kill sayısı
        self.trend_warning_count: int = 0            # speed trend uyarısı sayısı
        self.false_alarm_count: int = 0              # supervisor sahte alarm

        # ── Pipe & downstream durumu ──────────────────────────────────────
        self.pipe_status: str = "unknown"            # ready | broken | connecting
        self.fifo_status: str = "stable"             # stable | reconnecting | failed
        self.fifo_recover_attempts: int = 0
        self.rtmp_events: deque = deque(maxlen=10)   # son RTMP connect/disconnect
        self.last_yt_kick_ok_ts: str = ""            # son başarılı tee mesajı zamanı

        # ── Olay timeline'ı (operatöre tarihçe) ───────────────────────────
        self.events: deque = deque(maxlen=30)        # {ts, kind, msg, severity}

        # ── Ankara relay durumu ───────────────────────────────────────────
        self.relay_status: dict = {}                 # {dvr: {status, last_ok}}

        # ── Batch playback progress (operatör batch içinde ne kadar oynadı görsün) ──
        self.current_batch_started_at: Optional[str] = None  # log ts
        self.current_batch_duration_sec: int = 0     # toplam batch içerik süresi
        self.last_streaming_frame: int = 0
        self.real_streaming: bool = False            # filler mı, gerçek batch mı

        # ── Sistem kaynakları (sampler thread doldurur) ───────────────────
        self.resources: dict = {
            "load1": 0.0, "load5": 0.0, "load15": 0.0,
            "mem_total_mb": 0, "mem_used_mb": 0, "mem_free_mb": 0,
            "swap_used_mb": 0, "swap_total_mb": 0,
            "ffmpeg_count": 0, "ffmpeg_total_cpu": 0.0,
            "disk_used_pct": 0, "disk_free_gb": 0.0,
            "log_size_mb": 0.0,                  # /var/log/kamerashorts-live.log boyutu
            "stream_uptime_sec": 0,              # Stream FFmpeg sürecinin yaşı (saniye)
            "batch_count": 0,                    # /tmp/ks_v4/batch_*.ts dosya sayısı
            "batch_total_mb": 0,                 # toplam batch disk kullanımı
        }
        self.resources_ts: str = ""

        # ── Harvester istatistikleri (harvester.py data/harvester_stats.json yazar) ──
        self.harvester: dict = {
            "ankara":   {"attempts": 0, "success": 0, "failed": 0, "queued": 0,
                         "last_run": None, "last_status": "—", "last_youtube_url": "", "last_batch": ""},
            "istanbul": {"attempts": 0, "success": 0, "failed": 0, "queued": 0,
                         "last_run": None, "last_status": "—", "last_youtube_url": "", "last_batch": ""},
            "corum":    {"attempts": 0, "success": 0, "failed": 0, "queued": 0,
                         "last_run": None, "last_status": "—", "last_youtube_url": "", "last_batch": ""},
            "konya":    {"attempts": 0, "success": 0, "failed": 0, "queued": 0,
                         "last_run": None, "last_status": "—", "last_youtube_url": "", "last_batch": ""},
        }
        self.harvester_active: bool = False     # kamerashorts-harvester.service aktif mi
        self.harvester_uptime_sec: int = 0      # Daemon uptime (saniye)

    def poll(self):
        path = Path(self.log_path)
        if not path.exists():
            return

        # Servis durumu
        try:
            r = subprocess.run(
                ["systemctl", "is-active", "kamerashorts-live"],
                capture_output=True, text=True, timeout=3
            )
            running = r.stdout.strip() == "active"
        except Exception:
            running = False

        # Log dosyasını oku (sadece yeni satırlar varsa)
        try:
            size = path.stat().st_size
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except Exception:
            return

        with self._lock:
            self.service_running = running
            self._parse(lines[-2000:])

    def _parse(self, lines: list[str]):
        city_progress   = {}
        batch_history   = []
        streaming_batch = ""
        building_id     = None
        speed = fps_v = 0.0
        frame = 0
        start_time      = self.start_time
        recent_logs     = []
        batch_queued    = 0
        speed_history   = list(self.speed_history)

        # Yeni state lokalleri
        current_action  = "—"
        transcode_active: Optional[dict] = None
        concat_active   = False
        collector_phase_active = False
        crash_count     = 0
        watchdog_count  = 0
        trend_warning_count = 0
        false_alarm_count = 0
        pipe_status     = "unknown"
        fifo_status     = "stable"
        fifo_recover_attempts = 0
        rtmp_events     = []
        last_yt_kick_ok = ""
        events: list[dict] = []
        relay_status    = {}
        current_batch_started_at = None
        current_batch_duration_sec = 0
        last_streaming_frame = 0
        real_streaming  = False

        _ts_re      = re.compile(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})')
        _src_re     = re.compile(r'\[live\s*\]\s+(INFO|WARNING|ERROR|DEBUG)\s+(.*)')
        # [durum] batch=<batch_name veya boş> speed=Xx fps=Y frame=Z
        _status_re  = re.compile(r'\[durum\] batch=(\S*) speed=([\d.]+)x fps=([\d.]+) frame=(\d+)')

        def _push_event(ts_str: str, kind: str, m: str, sev: str = "info"):
            events.append({"ts": ts_str[-8:] if ts_str else "",
                           "kind": kind, "msg": m[:120], "severity": sev})

        for raw in lines:
            line = raw.strip()
            if not line:
                continue

            ts_m  = _ts_re.match(line)
            ts    = ts_m.group(1) if ts_m else ""
            src_m = _src_re.search(line)
            level = src_m.group(1) if src_m else "INFO"
            msg   = src_m.group(2).strip() if src_m else line

            # Log listesi (INFO/WARNING/ERROR)
            if level in ("INFO", "WARNING", "ERROR"):
                recent_logs.append({"ts": ts[-8:], "level": level, "msg": msg})

            # v4 başlangıcı → state sıfırla (sayaçlar da)
            if "KameraShorts Live Streamer v4" in line:
                start_time    = ts
                city_progress = {}
                batch_history = []
                streaming_batch = ""
                building_id   = None
                batch_queued  = 0
                speed_history = []
                # Sayaçları sıfırla
                crash_count = watchdog_count = trend_warning_count = false_alarm_count = 0
                fifo_recover_attempts = 0
                pipe_status = "unknown"
                fifo_status = "stable"
                rtmp_events = []
                events = []
                relay_status = {}
                transcode_active = None
                concat_active = False
                collector_phase_active = False
                real_streaming = False
                current_batch_started_at = None
                current_batch_duration_sec = 0
                _push_event(ts, "system", "Servis başlatıldı", "info")
                continue

            # ── [durum] satırı: speed/fps/frame + batch tespiti ───────────
            sm = _status_re.search(line)
            if sm:
                batch_field = sm.group(1)
                speed = float(sm.group(2))
                fps_v = float(sm.group(3))
                frame = int(sm.group(4))
                speed_history.append((ts[-8:], speed))
                if batch_field and batch_field.startswith("batch_"):
                    streaming_batch = batch_field
                    last_streaming_frame = frame
                    real_streaming = True
                elif not batch_field:
                    real_streaming = False  # filler oynuyor
                continue

            # ── Pipe: yayın akıyor (DÜZELTME: "Yazılıyor", "Besleniyor" değil) ──
            m = re.search(r'▶ Yazılıyor: (batch_\d+\.ts) \(~(\d+)s\)', msg)
            if m:
                streaming_batch = m.group(1)
                current_batch_duration_sec = int(m.group(2))
                current_batch_started_at = ts
                batch_queued = max(0, batch_queued - 1)
                real_streaming = True
                current_action = f"▶ Yayın akıyor: {streaming_batch}"
                _push_event(ts, "stream_start", f"Yayın: {streaming_batch}", "good")
                continue

            # ── Pipe: sıradaki batch hazır (preload bilgisi) ──────────────
            m = re.search(r'✓ Sıradaki hazır: (batch_\d+\.ts)', msg)
            if m:
                batch_queued = 1
                continue

            # ── Pipe: kırılma ─────────────────────────────────────────────
            if "Kırık pipe" in msg:
                pipe_status = "broken"
                _push_event(ts, "pipe_break", "Pipe kırıldı (FFmpeg kapandı)", "error")
                continue

            # ── Pipe: FIFO bağlandı (writer hazır) ────────────────────────
            if "[pipe] FIFO bağlandı" in msg:
                pipe_status = "ready"
                continue

            # ── Pipe: SCHED_FIFO durumu ───────────────────────────────────
            if "[pipe] SCHED_FIFO prio=50 ayarlandı" in msg:
                _push_event(ts, "pipe_init", "SCHED_FIFO prio=50 aktif", "info")
                continue

            # ── Pipe: kurtarılan batch (crash sonrası) ────────────────────
            if "[pipe] ↩ Kurtarılan batch" in msg:
                _push_event(ts, "recovery", "Crash sonrası batch kurtarıldı", "info")
                continue

            # ── Builder: batch başlıyor ───────────────────────────────────
            m = re.search(r'═══ Batch (\d+) başlıyor', msg)
            if m:
                building_id   = int(m.group(1))
                city_progress = {}
                collector_phase_active = True
                current_action = f"📥 Batch {building_id} hazırlık (HLS indirme)"
                _push_event(ts, "batch_start", f"Batch {building_id} inşası başladı", "info")
                continue

            # ── Builder: indirme tamamlandı (collector fazı bitti) ────────
            m = re.search(r'Batch (\d+) indirme tamamlandı \((\d+)s\)', msg)
            if m:
                collector_phase_active = False
                current_action = f"⚙ Batch {m.group(1)} transcode aşaması"
                continue

            # ── Builder: transcode fazı başlıyor ──────────────────────────
            if "transcode başlıyor" in msg:
                current_action = "⚙ Transcode aşaması"
                continue

            # ── Builder: concat-remux ─────────────────────────────────────
            if "birleştiriliyor" in msg and "concat" in msg:
                concat_active = True
                current_action = "🔗 Concat-remux (batch birleştirme)"
                continue
            if "Concat-remux tamamlandı" in msg:
                concat_active = False
                continue

            # ── Builder: batch HAZIR ──────────────────────────────────────
            m = re.search(r'═══ Batch (\d+) HAZIR: (batch_\S+) \((\d+)MB.*toplam (\d+)s', msg)
            if m:
                bid, bname, size_mb, build_sec = int(m.group(1)), m.group(2), int(m.group(3)), int(m.group(4))
                batch_history.append({
                    "id": bid, "name": bname, "size_mb": size_mb,
                    "build_seconds": build_sec, "ts": ts[-8:],
                })
                if building_id == bid:
                    building_id = None
                _push_event(ts, "batch_ready", f"Batch {bid} hazır ({size_mb}MB, {build_sec}s)", "good")
                continue

            # ── Kuyruğa eklendi ───────────────────────────────────────────
            if "Kuyruğa eklendi" in msg:
                batch_queued = 1
                continue

            # ── Collector: başladı ────────────────────────────────────────
            m = re.search(r'\[collector:(.*?)\] Başladı → hedef ([\d.]+)s', msg)
            if m:
                city, target = m.group(1), float(m.group(2))
                city_progress[city] = {
                    "dur": 0, "target": target, "status": "indiriliyor", "pct": 0,
                }
                continue

            # ── Collector: ilerleme ───────────────────────────────────────
            m = re.search(r'\[collector:(.*?)\] İlerleme: ([\d.]+)/([\d.]+)s', msg)
            if m:
                city, dur, target = m.group(1), float(m.group(2)), float(m.group(3))
                city_progress[city] = {
                    "dur": dur, "target": target,
                    "status": "indiriliyor",
                    "pct": round(min(dur / target * 100, 100)),
                }
                continue

            # ── Collector: bitti ──────────────────────────────────────────
            m = re.search(r'\[collector:(.*?)\] Bitti: \d+ seg, ([\d.]+)/([\d.]+)s', msg)
            if m:
                city, dur, target = m.group(1), float(m.group(2)), float(m.group(3))
                ok = dur >= 30
                city_progress[city] = {
                    "dur": dur, "target": target,
                    "status": "tamamlandı" if ok else "başarısız",
                    "pct": round(min(dur / target * 100, 100)) if ok else 0,
                }
                continue

            # ── Collector: yetersiz ───────────────────────────────────────
            m = re.search(r'\[collector:(.*?)\] Yetersiz', msg)
            if m:
                city = m.group(1)
                if city in city_progress:
                    city_progress[city]["status"] = "başarısız"
                continue

            # ── Transcode: başladı ────────────────────────────────────────
            m = re.search(r'\[transcode:(.*?)\] Başladı \((\d+) seg → (\S+)\)', msg)
            if m:
                city = m.group(1)
                seg_count = int(m.group(2))
                transcode_active = {
                    "city": city, "started_ts": ts[-8:],
                    "seg_count": seg_count,
                }
                current_action = f"⚙ Transcode: {city} ({seg_count} seg)"
                continue

            # ── Transcode: bitti (OK) ─────────────────────────────────────
            m = re.search(r'\[transcode:(.*?)\] OK — (\d+)s', msg)
            if m:
                city = m.group(1)
                if transcode_active and transcode_active.get("city") == city:
                    transcode_active = None
                if city in city_progress:
                    city_progress[city]["status"] = "transcode ok"
                continue

            # ── Transcode: hata ───────────────────────────────────────────
            m = re.search(r'\[transcode:(.*?)\] (HATA|Zaman aşımı|İstisna)', msg)
            if m:
                city = m.group(1)
                if transcode_active and transcode_active.get("city") == city:
                    transcode_active = None
                if city in city_progress:
                    city_progress[city]["status"] = "transcode hata"
                _push_event(ts, "transcode_error", f"{city} transcode başarısız", "error")
                continue

            # ── Supervisor: çöktü ─────────────────────────────────────────
            if "[supervisor] FFmpeg çöktü" in msg:
                crash_count += 1
                pipe_status = "broken"
                current_action = "💥 FFmpeg çöktü → kontrol bekleniyor"
                _push_event(ts, "crash", "FFmpeg çöktü", "error")
                continue

            # ── Supervisor: sahte alarm ───────────────────────────────────
            if "sahte alarm" in msg:
                false_alarm_count += 1
                _push_event(ts, "false_alarm", "Sahte alarm (FFmpeg ayakta)", "info")
                continue

            # ── Supervisor: yeniden başlatılıyor ──────────────────────────
            if "[supervisor] Yeniden başlatılıyor" in msg:
                current_action = "↺ Stream FFmpeg restart"
                continue

            # ── Supervisor: yeniden başlatıldı ────────────────────────────
            if "[supervisor] Yeniden başlatıldı" in msg:
                pipe_status = "ready"
                _push_event(ts, "restart", "Stream FFmpeg restart başarılı", "warn")
                continue

            # ── Supervisor: ✓ ffmpeg hâlâ çalışıyor ──────────────────────
            if "[supervisor] ✓ FFmpeg" in msg:
                _push_event(ts, "supervisor_ok", "FFmpeg sağlıklı", "info")
                continue

            # ── Watchdog ──────────────────────────────────────────────────
            m = re.search(r'\[watchdog\] Frame (\d+) 60s', msg)
            if m:
                watchdog_count += 1
                _push_event(ts, "watchdog", f"Frame {m.group(1)} 60s dondu → kill", "error")
                continue

            # ── Trend uyarısı ─────────────────────────────────────────────
            if "[trend] ⚠" in msg:
                trend_warning_count += 1
                continue

            # ── FIFO recovery ─────────────────────────────────────────────
            if "[fifo] ⚡ Yeniden bağlanmaya" in msg:
                fifo_status = "reconnecting"
                fifo_recover_attempts += 1
                _push_event(ts, "fifo_reconnect", "MediaMTX→YT/Kick reconnect", "warn")
                continue
            if "[fifo] ✓ Bağlantı yeniden kuruldu" in msg:
                fifo_status = "stable"
                _push_event(ts, "fifo_ok", "FIFO yeniden bağlandı", "good")
                continue
            if "[fifo] ✗ Kurtarılamaz" in msg:
                fifo_status = "failed"
                _push_event(ts, "fifo_fail", "FIFO kurtarılamaz hata", "error")
                continue

            # ── RTMP olayları ─────────────────────────────────────────────
            m = re.search(r'\[rtmp\] (.+)', msg)
            if m:
                rtmp_events.append({"ts": ts[-8:], "msg": m.group(1)[:80]})
                continue

            # ── Ankara relay ──────────────────────────────────────────────
            m = re.search(r'\[relay\] (\S+) hazır \((\d+)s\)', msg)
            if m:
                dvr = m.group(1)
                relay_status[dvr] = {"status": "active", "last_ok": ts[-8:]}
                continue
            m = re.search(r'\[relay\] (\S+) yenilendi', msg)
            if m:
                dvr = m.group(1)
                if dvr not in relay_status:
                    relay_status[dvr] = {}
                relay_status[dvr]["last_ok"] = ts[-8:]
                relay_status[dvr]["status"] = "active"
                continue
            if "[relay]" in msg and ("hata" in msg.lower() or "yanıt vermedi" in msg.lower()):
                _push_event(ts, "relay_error", msg[:100], "warn")
                continue

            # ── Cleanup / temizlik bilgisi ────────────────────────────────
            m = re.search(r'\[main\] Eski tmp segment temizlendi: (\d+) dosya', msg)
            if m:
                _push_event(ts, "cleanup", f"Eski {m.group(1)} tmp temizlendi", "info")
                continue

        # Eğer şu an gerçek batch çalmıyorsa current_action default'la güncelle
        if not current_action or current_action == "—":
            if real_streaming and streaming_batch:
                current_action = f"▶ Yayın akıyor: {streaming_batch}"
            elif building_id is not None and collector_phase_active:
                current_action = f"📥 Batch {building_id} hazırlık (HLS indirme)"
            elif transcode_active:
                current_action = f"⚙ Transcode: {transcode_active['city']}"
            elif concat_active:
                current_action = "🔗 Concat-remux"
            elif streaming_batch:
                current_action = f"⏳ Batch bekleniyor — filler oynuyor"
            else:
                current_action = "⏳ Filler — batch bekleniyor"

        # Yaz
        self.start_time      = start_time
        self.speed           = speed
        self.fps             = fps_v
        self.frame           = frame
        self.streaming_batch = streaming_batch
        self.building_batch_id = building_id
        self.city_progress   = city_progress
        self.batch_history   = batch_history[-10:]
        self.batch_queued    = batch_queued
        self.recent_logs     = deque(recent_logs[-80:], maxlen=80)
        self.speed_history   = deque(speed_history[-120:], maxlen=120)

        self.current_action  = current_action
        self.transcode_active = transcode_active
        self.concat_active   = concat_active
        self.collector_phase_active = collector_phase_active
        self.crash_count     = crash_count
        self.watchdog_count  = watchdog_count
        self.trend_warning_count = trend_warning_count
        self.false_alarm_count = false_alarm_count
        self.pipe_status     = pipe_status
        self.fifo_status     = fifo_status
        self.fifo_recover_attempts = fifo_recover_attempts
        self.rtmp_events     = deque(rtmp_events[-10:], maxlen=10)
        self.events          = deque(events[-30:], maxlen=30)
        self.relay_status    = relay_status
        self.current_batch_started_at = current_batch_started_at
        self.current_batch_duration_sec = current_batch_duration_sec
        self.last_streaming_frame = last_streaming_frame
        self.real_streaming  = real_streaming

    def snapshot(self) -> dict:
        with self._lock:
            phase = "bekliyor"
            if self.real_streaming and self.streaming_batch:
                phase = "yayında"
            elif self.streaming_batch and not self.real_streaming:
                phase = "filler"
            elif self.building_batch_id is not None:
                phase = "hazırlanıyor"

            # Batch playback progress (frame'den hesapla)
            playback_progress_pct = 0
            playback_sec_played = 0
            playback_sec_total = self.current_batch_duration_sec
            if self.real_streaming and self.fps > 0 and playback_sec_total > 0:
                # Stream FFmpeg'in batch içinde kaç saniye işlediği:
                # frame'in mutlak değerini bilmiyoruz (her restart sıfırlanır),
                # ama batch içinde "Yazılıyor" sonrası fps × elapsed yaklaşımı.
                # Daha doğru: current_batch_started_at log ts'ini al, server_time
                # ile aradaki saniye farkı = batch içi geçen süre.
                try:
                    started = self.current_batch_started_at
                    if started:
                        # log ts formatı: "2026-05-16 18:55:41"
                        from datetime import datetime
                        st_dt = datetime.strptime(started[-8:], "%H:%M:%S")
                        now_dt = datetime.strptime(time.strftime("%H:%M:%S"), "%H:%M:%S")
                        elapsed = (now_dt - st_dt).total_seconds()
                        if elapsed < 0:  # gün dönmüş
                            elapsed += 86400
                        playback_sec_played = max(0, min(int(elapsed), playback_sec_total))
                        playback_progress_pct = round(playback_sec_played / playback_sec_total * 100)
                except Exception:
                    pass

            return {
                "ok":               self.service_running,
                "start_time":       self.start_time,
                "speed":            self.speed,
                "fps":              self.fps,
                "frame":            self.frame,
                "streaming_batch":  self.streaming_batch,
                "real_streaming":   self.real_streaming,
                "building_batch_id":self.building_batch_id,
                "city_progress":    self.city_progress,
                "batch_history":    self.batch_history,
                "batch_queued":     self.batch_queued,
                "logs":             list(self.recent_logs)[-50:],
                "speed_history":    list(self.speed_history)[-60:],
                "phase":            phase,
                "server_time":      time.strftime("%H:%M:%S"),

                # Yeni alanlar
                "current_action":   self.current_action,
                "transcode_active": self.transcode_active,
                "concat_active":    self.concat_active,
                "collector_phase_active": self.collector_phase_active,

                "health": {
                    "crash_count":         self.crash_count,
                    "watchdog_count":      self.watchdog_count,
                    "trend_warning_count": self.trend_warning_count,
                    "false_alarm_count":   self.false_alarm_count,
                    "fifo_recover_attempts": self.fifo_recover_attempts,
                },

                "pipe_status":      self.pipe_status,
                "fifo_status":      self.fifo_status,
                "rtmp_events":      list(self.rtmp_events),
                "events":           list(self.events),
                "relay_status":     self.relay_status,

                "playback": {
                    "sec_played":   playback_sec_played,
                    "sec_total":    playback_sec_total,
                    "pct":          playback_progress_pct,
                    "started_at":   self.current_batch_started_at or "",
                },

                "resources":        self.resources,
                "resources_ts":     self.resources_ts,

                # Harvester (4 şehir Stream Hasadı)
                "harvester": {
                    "active": self.harvester_active,
                    "uptime_sec": self.harvester_uptime_sec,
                    "cities": self.harvester,
                },
            }

    def sample_resources(self):
        """Sistem kaynaklarını örnekler — /proc'tan okur, hızlı."""
        try:
            # Load avg
            with open("/proc/loadavg") as f:
                parts = f.read().split()
            load1, load5, load15 = float(parts[0]), float(parts[1]), float(parts[2])

            # Memory
            mem = {}
            with open("/proc/meminfo") as f:
                for ln in f:
                    k, _, v = ln.partition(":")
                    v = v.strip().split()[0]
                    try: mem[k] = int(v)
                    except ValueError: pass
            mem_total_mb = mem.get("MemTotal", 0) // 1024
            mem_avail_mb = mem.get("MemAvailable", 0) // 1024
            mem_used_mb  = mem_total_mb - mem_avail_mb
            swap_total_mb= mem.get("SwapTotal", 0) // 1024
            swap_free_mb = mem.get("SwapFree", 0) // 1024
            swap_used_mb = swap_total_mb - swap_free_mb

            # FFmpeg sayısı (ps üzerinden)
            try:
                r = subprocess.run(["pgrep", "-c", "ffmpeg"], capture_output=True, text=True, timeout=2)
                ffmpeg_count = int(r.stdout.strip()) if r.stdout.strip() else 0
            except Exception:
                ffmpeg_count = 0

            # FFmpeg toplam CPU% (top -bn1 ile)
            ffmpeg_total_cpu = 0.0
            try:
                r = subprocess.run(
                    ["ps", "-eo", "comm,pcpu", "--no-headers"],
                    capture_output=True, text=True, timeout=2,
                )
                for ln in r.stdout.splitlines():
                    p = ln.split()
                    if len(p) >= 2 and p[0] == "ffmpeg":
                        try: ffmpeg_total_cpu += float(p[1])
                        except ValueError: pass
            except Exception:
                pass

            # Disk (/ ve /tmp aynı disk genelde)
            disk_pct = 0
            try:
                import shutil as _sh
                t = _sh.disk_usage("/")
                disk_pct = round(t.used / t.total * 100)
            except Exception:
                pass

            with self._lock:
                self.resources = {
                    "load1": round(load1, 2),
                    "load5": round(load5, 2),
                    "load15": round(load15, 2),
                    "mem_total_mb": mem_total_mb,
                    "mem_used_mb":  mem_used_mb,
                    "mem_free_mb":  mem_avail_mb,
                    "swap_total_mb": swap_total_mb,
                    "swap_used_mb":  swap_used_mb,
                    "ffmpeg_count":  ffmpeg_count,
                    "ffmpeg_total_cpu": round(ffmpeg_total_cpu, 1),
                    "disk_used_pct": disk_pct,
                }
                self.resources_ts = time.strftime("%H:%M:%S")
        except Exception as e:
            print(f"[resources] Hata: {e}")

    def sample_harvester(self):
        """Harvester istatistiklerini data/harvester_stats.json'dan yükle + servis durumu."""
        try:
            # Servis aktif mi
            try:
                r = subprocess.run(
                    ["systemctl", "is-active", "kamerashorts-harvester"],
                    capture_output=True, text=True, timeout=3,
                )
                active = r.stdout.strip() == "active"
            except Exception:
                active = False

            uptime_sec = 0
            try:
                r = subprocess.run(
                    ["systemctl", "show", "kamerashorts-harvester",
                     "--property=ActiveEnterTimestampMonotonic"],
                    capture_output=True, text=True, timeout=3,
                )
                if "=" in r.stdout:
                    val = int(r.stdout.split("=", 1)[1].strip() or 0)
                    if val > 0:
                        # Monotonic timestamps in microseconds
                        import time as _t
                        with open("/proc/uptime") as f:
                            sys_up_sec = float(f.read().split()[0])
                        active_since_sec = sys_up_sec - val / 1_000_000
                        uptime_sec = int(active_since_sec) if active_since_sec > 0 else 0
            except Exception:
                pass

            # Stats dosyasını oku
            stats = {}
            try:
                p = Path("/opt/KameraShorts/data/harvester_stats.json")
                if p.exists():
                    stats = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                pass

            with self._lock:
                self.harvester_active = active
                self.harvester_uptime_sec = uptime_sec
                for city in ("ankara", "istanbul", "corum", "konya"):
                    if city in stats:
                        self.harvester[city].update(stats[city])
        except Exception as e:
            print(f"[harvester] Hata: {e}")

# ─── HTML ────────────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="tr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>KameraShorts Live</title>
<style>
  :root{
    --bg:#0b1120;--card:#111827;--card2:#1e293b;--border:#1e293b;
    --green:#22c55e;--yellow:#f59e0b;--red:#ef4444;--blue:#3b82f6;
    --text:#e2e8f0;--muted:#64748b;--accent:#6366f1;
  }
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:var(--bg);color:var(--text);font-family:'Courier New',monospace;font-size:13px;min-height:100vh}
  a{color:inherit;text-decoration:none}

  /* ── layout ── */
  .wrap{max-width:1200px;margin:0 auto;padding:16px}
  .header{display:flex;align-items:center;gap:12px;margin-bottom:20px;padding-bottom:14px;border-bottom:1px solid var(--border)}
  .header h1{font-size:18px;font-weight:700;letter-spacing:.04em;color:#fff}
  .header .sub{color:var(--muted);font-size:11px;margin-top:2px}
  .dot{width:10px;height:10px;border-radius:50%;background:var(--muted);flex-shrink:0}
  .dot.live{background:var(--green);box-shadow:0 0 8px var(--green);animation:pulse 2s infinite}
  @keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
  .badge{padding:2px 8px;border-radius:99px;font-size:11px;font-weight:700;letter-spacing:.08em}
  .badge.green{background:#14532d;color:var(--green);border:1px solid #166534}
  .badge.yellow{background:#451a03;color:var(--yellow);border:1px solid #78350f}
  .badge.red{background:#450a0a;color:var(--red);border:1px solid #7f1d1d}
  .badge.blue{background:#1e1b4b;color:var(--blue);border:1px solid #1d4ed8}
  .badge.gray{background:#1e293b;color:var(--muted);border:1px solid #334155}

  .grid{display:grid;gap:12px}
  .grid-4{grid-template-columns:repeat(4,1fr)}
  .grid-2{grid-template-columns:1fr 1fr}
  @media(max-width:900px){.grid-4{grid-template-columns:repeat(2,1fr)}.grid-2{grid-template-columns:1fr}}
  @media(max-width:500px){.grid-4{grid-template-columns:1fr}}

  .card{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:16px}
  .card-label{font-size:10px;font-weight:700;letter-spacing:.1em;color:var(--muted);text-transform:uppercase;margin-bottom:8px}
  .card-value{font-size:28px;font-weight:700;line-height:1;color:#fff}
  .card-sub{font-size:11px;color:var(--muted);margin-top:4px}

  /* ── speed gauge ── */
  .speed-val{font-size:48px;font-weight:900;line-height:1;transition:color .3s}
  .speed-val.good{color:var(--green)}
  .speed-val.warn{color:var(--yellow)}
  .speed-val.bad{color:var(--red)}
  .speed-val.off{color:var(--muted)}

  /* ── progress bar ── */
  .progress{height:6px;background:#1e293b;border-radius:3px;overflow:hidden;margin-top:6px}
  .progress-fill{height:100%;border-radius:3px;transition:width .5s}
  .progress-fill.green{background:var(--green)}
  .progress-fill.yellow{background:var(--yellow)}
  .progress-fill.red{background:var(--red)}
  .progress-fill.blue{background:var(--blue)}
  .progress-fill.gray{background:var(--muted)}

  /* ── control buttons ── */
  .ctrl-btn{padding:6px 14px;border-radius:6px;border:none;font-size:12px;font-weight:700;cursor:pointer;letter-spacing:.04em;transition:opacity .15s}
  .ctrl-btn:hover{opacity:.8}
  .ctrl-btn:disabled{opacity:.4;cursor:not-allowed}
  .ctrl-btn.stop{background:#7f1d1d;color:#fca5a5;border:1px solid var(--red)}
  .ctrl-btn.start{background:#14532d;color:#86efac;border:1px solid var(--green)}
  .ctrl-btn.restart{background:#1e1b4b;color:#a5b4fc;border:1px solid var(--blue)}

  /* ── city cards ── */
  .city-row{display:flex;align-items:center;gap:10px;padding:10px 14px;background:var(--card2);border-radius:8px;margin-bottom:8px}
  .city-name{width:130px;flex-shrink:0;font-weight:600;font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .city-bar-wrap{flex:1}
  .city-pct{width:40px;text-align:right;flex-shrink:0;font-size:11px;color:var(--muted)}
  .city-dur{width:90px;text-align:right;flex-shrink:0;font-size:10px;color:var(--muted)}
  .city-status{width:90px;text-align:right;flex-shrink:0}

  /* ── speed graph ── */
  #speed-chart{width:100%;height:70px;display:block}

  /* ── logs ── */
  .log-area{background:#060d1a;border:1px solid var(--border);border-radius:8px;padding:12px;height:320px;overflow-y:auto;font-size:11px;line-height:1.6}
  .log-line{display:flex;gap:8px;padding:1px 0}
  .log-ts{color:#334155;flex-shrink:0;width:60px}
  .log-level{flex-shrink:0;width:55px;font-weight:700}
  .log-level.INFO{color:#475569}
  .log-level.WARNING{color:var(--yellow)}
  .log-level.ERROR{color:var(--red)}
  .log-msg{color:#94a3b8;word-break:break-all}
  .log-msg .hi{color:#e2e8f0}
  .log-msg .green{color:var(--green)}
  .log-msg .yellow{color:var(--yellow)}
  .log-msg .red{color:var(--red)}
  .log-msg .blue{color:var(--blue)}

  /* ── batch history ── */
  .batch-item{display:flex;align-items:center;gap:8px;padding:6px 10px;border-radius:6px;margin-bottom:4px;background:var(--card2);font-size:11px}
  .batch-item .bn{flex:1;color:#94a3b8}
  .batch-item .bs{color:var(--muted)}

  /* ── section ── */
  .section-title{font-size:11px;font-weight:700;letter-spacing:.1em;color:var(--muted);text-transform:uppercase;margin-bottom:10px;margin-top:4px}

  /* ── top bar ── */
  .topbar{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
  .topbar .right{margin-left:auto;display:flex;align-items:center;gap:8px;font-size:11px;color:var(--muted)}

  /* ── spinner ── */
  .spin{display:inline-block;width:10px;height:10px;border:2px solid var(--border);border-top-color:var(--accent);border-radius:50%;animation:spin .8s linear infinite}
  @keyframes spin{to{transform:rotate(360deg)}}

  /* ── now-playing bar ── */
  .now{display:flex;align-items:center;gap:14px;padding:14px 18px;background:linear-gradient(90deg,#1e1b4b,#0b1120);border:1px solid #312e81;border-radius:10px;margin-bottom:12px}
  .now-icon{font-size:24px;line-height:1}
  .now-text{flex:1;font-size:15px;font-weight:600;color:#e0e7ff}
  .now-meta{font-size:11px;color:var(--muted);text-align:right}

  /* ── batch playback progress bar ── */
  .playback-bar{height:8px;background:#1e293b;border-radius:4px;overflow:hidden;margin-top:8px}
  .playback-fill{height:100%;background:linear-gradient(90deg,#3b82f6,#22c55e);transition:width 1s linear}
  .playback-meta{display:flex;justify-content:space-between;font-size:10px;color:var(--muted);margin-top:4px}

  /* ── health badges ── */
  .health-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-top:8px}
  .health-cell{background:var(--card2);border-radius:6px;padding:8px 10px;text-align:center}
  .health-num{font-size:20px;font-weight:700;line-height:1.1}
  .health-num.zero{color:var(--green)}
  .health-num.few{color:var(--yellow)}
  .health-num.many{color:var(--red)}
  .health-label{font-size:9px;color:var(--muted);text-transform:uppercase;letter-spacing:.08em;margin-top:2px}

  /* ── pipe/fifo status pills ── */
  .pill{display:inline-flex;align-items:center;gap:6px;padding:3px 10px;border-radius:99px;font-size:11px;font-weight:600}
  .pill .pd{width:8px;height:8px;border-radius:50%}
  .pill.ok{background:#14532d33;color:var(--green)}
  .pill.ok .pd{background:var(--green);box-shadow:0 0 6px var(--green)}
  .pill.warn{background:#78350f33;color:var(--yellow)}
  .pill.warn .pd{background:var(--yellow)}
  .pill.bad{background:#7f1d1d33;color:var(--red)}
  .pill.bad .pd{background:var(--red);box-shadow:0 0 6px var(--red)}
  .pill.gray{background:#33415533;color:var(--muted)}
  .pill.gray .pd{background:var(--muted)}

  /* ── event timeline ── */
  .ev-list{max-height:280px;overflow-y:auto}
  .ev-item{display:flex;gap:10px;padding:6px 10px;border-radius:6px;background:var(--card2);margin-bottom:4px;font-size:11px;align-items:center}
  .ev-ts{color:#334155;flex-shrink:0;width:62px;font-family:monospace}
  .ev-kind{flex-shrink:0;padding:1px 6px;border-radius:3px;font-size:9px;font-weight:700;letter-spacing:.05em}
  .ev-msg{flex:1;color:#cbd5e1}
  .ev-kind.good{background:#14532d;color:var(--green)}
  .ev-kind.info{background:#1e1b4b;color:#a5b4fc}
  .ev-kind.warn{background:#78350f;color:var(--yellow)}
  .ev-kind.error{background:#7f1d1d;color:var(--red)}

  /* ── resource mini bars ── */
  .res-row{display:flex;align-items:center;gap:10px;margin-bottom:8px;font-size:11px}
  .res-label{width:80px;color:var(--muted);flex-shrink:0}
  .res-bar{flex:1;height:6px;background:#1e293b;border-radius:3px;overflow:hidden}
  .res-fill{height:100%;border-radius:3px;transition:width .5s}
  .res-fill.low{background:var(--green)}
  .res-fill.mid{background:var(--yellow)}
  .res-fill.high{background:var(--red)}
  .res-val{width:90px;text-align:right;flex-shrink:0;color:#94a3b8;font-family:monospace}

  /* ── transcode/concat in-progress chip ── */
  .chip{display:inline-block;padding:3px 10px;border-radius:6px;background:#1e1b4b;color:#a5b4fc;font-size:11px;font-weight:600;margin-right:6px}

  /* ── harvester city tile ── */
  .harv-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-top:10px}
  @media(max-width:900px){.harv-grid{grid-template-columns:repeat(2,1fr)}}
  @media(max-width:500px){.harv-grid{grid-template-columns:1fr}}
  .harv-cell{background:var(--card2);border:1px solid var(--border);border-radius:10px;padding:14px;position:relative}
  .harv-cell .h-flag{position:absolute;top:8px;right:10px;font-size:18px}
  .harv-cell .h-name{font-size:14px;font-weight:700;color:#e2e8f0;letter-spacing:.04em;margin-bottom:8px}
  .harv-cell .h-name .h-fmt{font-size:9px;color:var(--muted);font-weight:500;letter-spacing:.06em;margin-left:6px}
  .harv-cell .h-stats{font-size:11px;color:#94a3b8;line-height:1.6}
  .harv-cell .h-success{color:var(--green);font-weight:700}
  .harv-cell .h-last{margin-top:8px;padding-top:8px;border-top:1px solid var(--border);font-size:10px;color:var(--muted)}
  .harv-cell .h-status-good{color:var(--green)}
  .harv-cell .h-status-warn{color:var(--yellow)}
  .harv-cell .h-status-bad{color:var(--red)}
  .harv-cell .h-link{display:block;margin-top:4px;font-size:10px;color:var(--blue);text-decoration:none;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .harv-cell .h-link:hover{color:#60a5fa}
</style>
</head>
<body>
<div class="wrap">

  <!-- HEADER -->
  <div class="header">
    <div class="dot" id="dot"></div>
    <div>
      <h1>🎥 KameraShorts Live</h1>
      <div class="sub" id="start-time">—</div>
    </div>
    <div class="topbar" style="margin-left:auto;gap:8px;align-items:center">
      <span class="badge gray" id="phase-badge">bekliyor</span>
      <span style="color:var(--muted);font-size:11px" id="clock">—</span>
      <button class="ctrl-btn stop"    id="btn-stop"    onclick="ctrlStream('stop')">⏹ Durdur</button>
      <button class="ctrl-btn start"   id="btn-start"   onclick="ctrlStream('start')" style="display:none">▶ Başlat</button>
      <button class="ctrl-btn restart" id="btn-restart" onclick="ctrlStream('restart')">↺ Yeniden Başlat</button>
    </div>
  </div>

  <!-- ŞU ANDA NE OLUYOR (operator-facing one-liner) -->
  <div class="now" id="now-bar">
    <div class="now-icon" id="now-icon">⏳</div>
    <div class="now-text" id="now-text">Bağlanılıyor…</div>
    <div class="now-meta" id="now-meta"></div>
  </div>

  <!-- STAT CARDS -->
  <div class="grid grid-4" style="margin-bottom:12px">

    <div class="card" style="grid-column:span 1">
      <div class="card-label">Hız</div>
      <div class="speed-val off" id="speed-val">—</div>
      <div class="card-sub"><span id="fps-val">—</span> fps &nbsp;·&nbsp; frame <span id="frame-val">—</span></div>
    </div>

    <div class="card">
      <div class="card-label">Yayında</div>
      <div class="card-value" style="font-size:16px;word-break:break-all" id="streaming-val">—</div>
      <div class="card-sub" id="streaming-sub">—</div>
      <div class="playback-bar" id="pb-bar" style="display:none">
        <div class="playback-fill" id="pb-fill" style="width:0%"></div>
      </div>
      <div class="playback-meta" id="pb-meta" style="display:none">
        <span id="pb-played">0:00</span>
        <span id="pb-total">0:00</span>
      </div>
    </div>

    <div class="card">
      <div class="card-label">Hazırlanıyor</div>
      <div class="card-value" style="font-size:16px" id="building-val">—</div>
      <div class="card-sub" id="building-sub">—</div>
    </div>

    <div class="card">
      <div class="card-label">Tamamlanan Batch</div>
      <div class="card-value" id="total-batches">0</div>
      <div class="card-sub">toplam batch</div>
    </div>
  </div>

  <!-- SAĞLIK + PIPE & RTMP -->
  <div class="grid grid-2" style="margin-bottom:12px">

    <div class="card">
      <div class="section-title">Yayın Sağlığı</div>
      <div class="health-grid">
        <div class="health-cell">
          <div class="health-num zero" id="h-crash">0</div>
          <div class="health-label">çöküş</div>
        </div>
        <div class="health-cell">
          <div class="health-num zero" id="h-watchdog">0</div>
          <div class="health-label">watchdog</div>
        </div>
        <div class="health-cell">
          <div class="health-num zero" id="h-trend">0</div>
          <div class="health-label">trend ⚠</div>
        </div>
        <div class="health-cell">
          <div class="health-num zero" id="h-fifo">0</div>
          <div class="health-label">FIFO reconn.</div>
        </div>
      </div>
      <div style="margin-top:10px;font-size:11px;color:var(--muted)" id="h-note">
        Sağlıklı çalışıyor — tüm sayaçlar sıfır.
      </div>
    </div>

    <div class="card">
      <div class="section-title">Pipe & Bağlantı Durumu</div>
      <div style="display:flex;flex-direction:column;gap:8px">
        <div style="display:flex;justify-content:space-between;align-items:center">
          <span style="color:var(--muted);font-size:11px">Stream FFmpeg → Pipe</span>
          <span id="pipe-pill" class="pill gray"><span class="pd"></span>—</span>
        </div>
        <div style="display:flex;justify-content:space-between;align-items:center">
          <span style="color:var(--muted);font-size:11px">MediaMTX → YouTube/Kick (FIFO)</span>
          <span id="fifo-pill" class="pill gray"><span class="pd"></span>—</span>
        </div>
        <div style="display:flex;justify-content:space-between;align-items:center">
          <span style="color:var(--muted);font-size:11px">Aktif FFmpeg süreci</span>
          <span id="ffmpeg-count" style="color:#e2e8f0;font-weight:600;font-family:monospace">—</span>
        </div>
        <div style="display:flex;justify-content:space-between;align-items:center">
          <span style="color:var(--muted);font-size:11px">Ankara relay</span>
          <span id="relay-info" style="color:#94a3b8;font-size:11px">—</span>
        </div>
      </div>
    </div>
  </div>

  <!-- SİSTEM KAYNAKLARI -->
  <div class="card" style="margin-bottom:12px">
    <div class="section-title" style="display:flex;justify-content:space-between;align-items:center">
      <span>Sistem Kaynakları</span>
      <span id="res-ts" style="font-size:10px;color:var(--muted);font-weight:normal;text-transform:none;letter-spacing:0">—</span>
    </div>
    <div id="res-rows"></div>
  </div>

  <!-- HARVESTER — 4 ŞEHİR STREAM HASADI -->
  <div class="card" style="margin-bottom:12px">
    <div class="section-title" style="display:flex;justify-content:space-between;align-items:center">
      <span>🎬 Harvester — Stream Hasadı (4 Şehir → YouTube)</span>
      <span id="harv-meta" style="font-size:10px;color:var(--muted);font-weight:normal;text-transform:none;letter-spacing:0">—</span>
    </div>
    <div class="harv-grid" id="harv-grid">
      <div style="grid-column:1/-1;color:var(--muted);font-size:12px;text-align:center;padding:20px">
        Harvester verisi bekleniyor…
      </div>
    </div>
  </div>

  <!-- SPEED GRAPH + CITIES -->
  <div class="grid grid-2" style="margin-bottom:12px">

    <div class="card">
      <div class="section-title">Hız Geçmişi (son 6 dk)</div>
      <canvas id="speed-chart"></canvas>
      <div style="display:flex;justify-content:space-between;font-size:9px;color:var(--muted);margin-top:4px">
        <span>6 dk önce</span><span>şimdi</span>
      </div>
    </div>

    <div class="card">
      <div class="section-title">Tamamlanan Batchler</div>
      <div id="batch-history">—</div>
    </div>
  </div>

  <!-- CITY PROGRESS -->
  <div class="card" style="margin-bottom:12px">
    <div class="section-title">Şehir İndirme Durumu — Batch <span id="building-bid">?</span></div>
    <div id="cities-wrap">
      <div style="color:var(--muted);font-size:12px">Batch hazırlanmıyor</div>
    </div>
    <div id="transcode-active" style="margin-top:10px"></div>
  </div>

  <!-- OLAY TİMELINE'I -->
  <div class="card" style="margin-bottom:12px">
    <div class="section-title">Olay Geçmişi (son 30)</div>
    <div class="ev-list" id="event-list">
      <div style="color:var(--muted);font-size:12px">Henüz olay yok</div>
    </div>
  </div>

  <!-- LOGS -->
  <div class="card">
    <div class="section-title" style="display:flex;align-items:center;gap:8px">
      Son Loglar <span class="spin" id="log-spin" style="display:none"></span>
    </div>
    <div class="log-area" id="log-area"></div>
  </div>

</div>

<script>
const STATUS_COLOR = {
  'indiriliyor': 'blue',
  'tamamlandı':  'green',
  'transcode ok':'green',
  'başarısız':   'red',
};

const SPEED_HIST = []; // {x, y}
let lastLogs = [];

function speedColor(s) {
  if (s <= 0)   return 'off';
  if (s >= 0.95) return 'good';
  if (s >= 0.75) return 'warn';
  return 'bad';
}

function fmtDur(s) {
  if (s < 60) return s.toFixed(0) + 's';
  return (s/60).toFixed(1) + 'dk';
}

function fmtSize(mb) { return mb.toFixed(0) + ' MB'; }

function highlightMsg(msg) {
  return msg
    .replace(/speed=([\d.]+)x/g, (_, s) => {
      const c = parseFloat(s) >= 0.95 ? 'green' : parseFloat(s) >= 0.75 ? 'yellow' : 'red';
      return `speed=<span class="${c}">${s}x</span>`;
    })
    .replace(/✓/g, '<span class="green">✓</span>')
    .replace(/⚠/g, '<span class="yellow">⚠</span>')
    .replace(/✗|HATA|ERROR/g, '<span class="red">$&</span>')
    .replace(/▶/g, '<span class="blue">▶</span>')
    .replace(/═+.*═+/g, s => `<span class="hi">${s}</span>`);
}

function renderLog(logs) {
  const el = document.getElementById('log-area');
  const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
  el.innerHTML = logs.map(l => `
    <div class="log-line">
      <span class="log-ts">${l.ts}</span>
      <span class="log-level ${l.level}">${l.level}</span>
      <span class="log-msg">${highlightMsg(l.msg)}</span>
    </div>`).join('');
  if (atBottom) el.scrollTop = el.scrollHeight;
}

function renderCities(cities, bid) {
  document.getElementById('building-bid').textContent = bid !== null ? bid : '?';
  const wrap = document.getElementById('cities-wrap');
  if (!cities || Object.keys(cities).length === 0) {
    wrap.innerHTML = '<div style="color:var(--muted);font-size:12px">Henüz veri yok</div>';
    return;
  }
  wrap.innerHTML = Object.entries(cities).map(([city, d]) => {
    const color = STATUS_COLOR[d.status] || 'gray';
    const pct   = d.pct || 0;
    return `
      <div class="city-row">
        <div class="city-name" title="${city}">${city}</div>
        <div class="city-bar-wrap">
          <div class="progress">
            <div class="progress-fill ${color}" style="width:${pct}%"></div>
          </div>
        </div>
        <div class="city-pct">${pct}%</div>
        <div class="city-dur">${fmtDur(d.dur||0)} / ${fmtDur(d.target||600)}</div>
        <div class="city-status"><span class="badge ${color}" style="font-size:10px">${d.status}</span></div>
      </div>`;
  }).join('');
}

function renderBatchHistory(history) {
  const el = document.getElementById('batch-history');
  if (!history || !history.length) {
    el.innerHTML = '<div style="color:var(--muted);font-size:12px">Henüz tamamlanan batch yok</div>';
    return;
  }
  el.innerHTML = [...history].reverse().slice(0, 6).map(b => {
    const buildSec = b.build_seconds ? ' · ' + fmtDur(b.build_seconds) + ' build' : '';
    const ts = b.ts ? ' · ' + b.ts : '';
    return `
    <div class="batch-item">
      <span style="color:var(--green);font-size:11px">✓</span>
      <span class="bn">${b.name}</span>
      <span class="bs">${fmtSize(b.size_mb)}${buildSec}${ts}</span>
    </div>`;
  }).join('');
}

// ── Speed mini chart ──────────────────────────────────────────────────────
const canvas = document.getElementById('speed-chart');
const ctx    = canvas.getContext('2d');

function drawChart(data) {
  const dpr = window.devicePixelRatio || 1;
  const W   = canvas.offsetWidth;
  const H   = 70;
  canvas.width  = W * dpr;
  canvas.height = H * dpr;
  ctx.scale(dpr, dpr);

  ctx.clearRect(0, 0, W, H);

  // grid lines
  [0.5, 0.75, 1.0].forEach(v => {
    const y = H - (v * H * 0.85) - 4;
    ctx.strokeStyle = v === 1.0 ? '#22c55e33' : '#ffffff0a';
    ctx.lineWidth   = v === 1.0 ? 1.5 : 1;
    ctx.setLineDash(v === 1.0 ? [4,4] : []);
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(W, y); ctx.stroke();
    ctx.setLineDash([]);
    if (v === 1.0) {
      ctx.fillStyle = '#22c55e66';
      ctx.font = '9px monospace';
      ctx.fillText('1.0x', 2, y - 2);
    }
  });

  if (!data || data.length < 2) return;

  const vals = data.map(d => d[1]);
  const step = W / (data.length - 1);

  // fill
  ctx.beginPath();
  ctx.moveTo(0, H);
  data.forEach((d, i) => {
    const x = i * step;
    const y = H - Math.min(d[1], 1.5) / 1.5 * H * 0.85 - 4;
    i === 0 ? ctx.lineTo(x, y) : ctx.lineTo(x, y);
  });
  ctx.lineTo((data.length - 1) * step, H);
  ctx.closePath();
  const grad = ctx.createLinearGradient(0, 0, 0, H);
  grad.addColorStop(0, '#6366f144');
  grad.addColorStop(1, '#6366f100');
  ctx.fillStyle = grad;
  ctx.fill();

  // line
  ctx.beginPath();
  data.forEach((d, i) => {
    const x = i * step;
    const y = H - Math.min(d[1], 1.5) / 1.5 * H * 0.85 - 4;
    const color = d[1] >= 0.95 ? '#22c55e' : d[1] >= 0.75 ? '#f59e0b' : '#ef4444';
    if (i === 0) {
      ctx.strokeStyle = color;
      ctx.lineWidth   = 2;
      ctx.moveTo(x, y);
    } else {
      ctx.lineTo(x, y);
      ctx.stroke();
      ctx.beginPath();
      ctx.strokeStyle = color;
      ctx.moveTo(x, y);
    }
  });
  ctx.stroke();
}

// ── Yardımcılar ────────────────────────────────────────────────────────────
function fmtMMSS(sec) {
  if (sec < 60) return '0:' + String(sec).padStart(2,'0');
  const m = Math.floor(sec/60), s = sec%60;
  return m + ':' + String(s).padStart(2,'0');
}

function healthClass(n) {
  if (!n) return 'zero';
  if (n <= 2) return 'few';
  return 'many';
}

function pipePillClass(s) {
  if (s === 'ready') return 'ok';
  if (s === 'broken') return 'bad';
  if (s === 'connecting' || s === 'reconnecting') return 'warn';
  return 'gray';
}

function fifoPillClass(s) {
  if (s === 'stable') return 'ok';
  if (s === 'reconnecting') return 'warn';
  if (s === 'failed') return 'bad';
  return 'gray';
}

function renderNow(action, phase) {
  const text = document.getElementById('now-text');
  const icon = document.getElementById('now-icon');
  text.textContent = action || '—';
  if (phase === 'yayında') icon.textContent = '▶';
  else if (phase === 'filler') icon.textContent = '⏳';
  else if (phase === 'hazırlanıyor') icon.textContent = '⚙';
  else if (action.includes('💥')) icon.textContent = '💥';
  else if (action.includes('↺')) icon.textContent = '↺';
  else icon.textContent = '·';
}

function renderHealth(h) {
  const map = {
    'h-crash': h.crash_count,
    'h-watchdog': h.watchdog_count,
    'h-trend': h.trend_warning_count,
    'h-fifo': h.fifo_recover_attempts,
  };
  for (const [id, n] of Object.entries(map)) {
    const el = document.getElementById(id);
    el.textContent = n;
    el.className = 'health-num ' + healthClass(n);
  }
  const note = document.getElementById('h-note');
  const total = h.crash_count + h.watchdog_count + h.fifo_recover_attempts;
  if (total === 0) {
    note.textContent = '✓ Sağlıklı çalışıyor — tüm sayaçlar sıfır.';
    note.style.color = 'var(--green)';
  } else {
    const parts = [];
    if (h.crash_count) parts.push(h.crash_count + ' FFmpeg çöküşü');
    if (h.watchdog_count) parts.push(h.watchdog_count + ' watchdog kill');
    if (h.fifo_recover_attempts) parts.push(h.fifo_recover_attempts + ' FIFO reconnect');
    note.textContent = '⚠ ' + parts.join(' · ');
    note.style.color = total > 2 ? 'var(--red)' : 'var(--yellow)';
  }
}

function renderPipeFifo(data) {
  const pp = document.getElementById('pipe-pill');
  const fp = document.getElementById('fifo-pill');
  const pc = pipePillClass(data.pipe_status);
  const fc = fifoPillClass(data.fifo_status);
  pp.className = 'pill ' + pc;
  pp.innerHTML = '<span class="pd"></span>' + (data.pipe_status || '—');
  fp.className = 'pill ' + fc;
  fp.innerHTML = '<span class="pd"></span>' + (data.fifo_status || '—');

  // FFmpeg count
  const fc_el = document.getElementById('ffmpeg-count');
  const r = data.resources || {};
  fc_el.textContent = r.ffmpeg_count
    ? r.ffmpeg_count + ' süreç · toplam %' + (r.ffmpeg_total_cpu || 0).toFixed(0) + ' CPU'
    : '—';

  // Relay
  const relay = data.relay_status || {};
  const dvrs = Object.keys(relay);
  const relayEl = document.getElementById('relay-info');
  if (dvrs.length === 0) {
    relayEl.textContent = 'aktif değil';
    relayEl.style.color = 'var(--muted)';
  } else {
    const d = dvrs[0];
    relayEl.innerHTML = `<span style="color:var(--green)">●</span> DVR ${d.slice(-6)} (son: ${relay[d].last_ok})`;
  }
}

function renderResources(r, ts) {
  const rows = [
    {label: 'Load avg (1m)', val: r.load1, max: 3.0, fmt: v => v.toFixed(2) + ' / 3.0'},
    {label: 'RAM kullanım', val: r.mem_used_mb, max: r.mem_total_mb,
     fmt: v => v + ' / ' + r.mem_total_mb + ' MB'},
    {label: 'Swap kullanım', val: r.swap_used_mb, max: r.swap_total_mb,
     fmt: v => r.swap_total_mb ? (v + ' / ' + r.swap_total_mb + ' MB') : 'yok'},
    {label: 'FFmpeg CPU', val: r.ffmpeg_total_cpu, max: 300,
     fmt: v => '%' + v.toFixed(0) + ' (' + (r.ffmpeg_count||0) + ' süreç)'},
    {label: 'Disk kullanım', val: r.disk_used_pct, max: 100,
     fmt: v => '%' + v},
  ];
  const wrap = document.getElementById('res-rows');
  wrap.innerHTML = rows.map(row => {
    const pct = row.max > 0 ? Math.min(row.val / row.max * 100, 100) : 0;
    const cls = pct > 75 ? 'high' : pct > 50 ? 'mid' : 'low';
    return `
      <div class="res-row">
        <div class="res-label">${row.label}</div>
        <div class="res-bar"><div class="res-fill ${cls}" style="width:${pct}%"></div></div>
        <div class="res-val">${row.fmt(row.val)}</div>
      </div>`;
  }).join('');
  document.getElementById('res-ts').textContent = ts ? 'güncellendi: ' + ts : '';
}

function renderEvents(events) {
  const el = document.getElementById('event-list');
  if (!events || !events.length) {
    el.innerHTML = '<div style="color:var(--muted);font-size:12px">Henüz olay yok</div>';
    return;
  }
  // Yeni olaylar üstte
  const sorted = [...events].reverse();
  el.innerHTML = sorted.map(e => {
    const sev = e.severity || 'info';
    const kindLabel = e.kind.replace(/_/g, ' ').toUpperCase();
    return `
      <div class="ev-item">
        <span class="ev-ts">${e.ts}</span>
        <span class="ev-kind ${sev}">${kindLabel}</span>
        <span class="ev-msg">${e.msg}</span>
      </div>`;
  }).join('');
}

function renderPlayback(data) {
  const bar = document.getElementById('pb-bar');
  const meta = document.getElementById('pb-meta');
  if (data.real_streaming && data.playback && data.playback.sec_total > 0) {
    bar.style.display = '';
    meta.style.display = '';
    document.getElementById('pb-fill').style.width = data.playback.pct + '%';
    document.getElementById('pb-played').textContent = fmtMMSS(data.playback.sec_played);
    document.getElementById('pb-total').textContent  = fmtMMSS(data.playback.sec_total);
  } else {
    bar.style.display = 'none';
    meta.style.display = 'none';
  }
}

function renderTranscodeActive(t) {
  const el = document.getElementById('transcode-active');
  const parts = [];
  if (t.transcode_active) {
    const tc = t.transcode_active;
    parts.push(`<span class="chip">⚙ Transcode: ${tc.city} (${tc.seg_count} seg, başl. ${tc.started_ts})</span>`);
  }
  if (t.concat_active) {
    parts.push(`<span class="chip">🔗 Concat-remux çalışıyor</span>`);
  }
  el.innerHTML = parts.join(' ');
}

function renderHarvester(h) {
  if (!h) return;
  // Üst başlık metadata
  const meta = document.getElementById('harv-meta');
  let metaText = h.active ? '● aktif' : '○ kapalı';
  if (h.active && h.uptime_sec > 0) {
    const hours = Math.floor(h.uptime_sec / 3600);
    const mins = Math.floor((h.uptime_sec % 3600) / 60);
    metaText += `  ·  uptime: ${hours > 0 ? hours + 'sa ' : ''}${mins}dk`;
  }
  // Toplam başarı/girişim
  let totalAttempts = 0, totalSuccess = 0, totalQueued = 0;
  for (const c of ['ankara', 'istanbul', 'corum', 'konya']) {
    if (h.cities && h.cities[c]) {
      totalAttempts += h.cities[c].attempts || 0;
      totalSuccess += h.cities[c].success || 0;
      totalQueued += h.cities[c].queued || 0;
    }
  }
  metaText += `  ·  ${totalSuccess}/${totalAttempts} başarılı`;
  if (totalQueued) metaText += `  ·  ${totalQueued} kuyrukta`;
  meta.textContent = metaText;
  meta.style.color = h.active ? 'var(--green)' : 'var(--muted)';

  // 4 şehir kartı
  const cityInfo = {
    'ankara':   {name: 'Ankara',   flag: '🇹🇷', fmt: '1080×1920 Shorts ⏰ saatlik (Direct EGO)'},
    'istanbul': {name: 'İstanbul', flag: '🌉', fmt: '⛔ upload pasif — sadece stream\'de'},
    'corum':    {name: 'Çorum',    flag: '🏘️', fmt: '⛔ upload pasif — sadece stream\'de'},
    'konya':    {name: 'Konya',    flag: '🕌', fmt: '⛔ upload pasif — sadece stream\'de'},
  };
  const cityKeys = ['ankara', 'istanbul', 'corum', 'konya'];
  const grid = document.getElementById('harv-grid');

  if (!h.cities) {
    grid.innerHTML = '<div style="grid-column:1/-1;color:var(--muted);font-size:12px;text-align:center;padding:20px">Veri yok</div>';
    return;
  }

  grid.innerHTML = cityKeys.map(key => {
    const c = h.cities[key] || {};
    const info = cityInfo[key];
    const attempts = c.attempts || 0;
    const success = c.success || 0;
    const failed = c.failed || 0;
    const queued = c.queued || 0;
    const rate = attempts > 0 ? Math.round(success / attempts * 100) : 0;

    // Status renk
    let statusClass = 'h-status-warn';
    let statusEmoji = '⏳';
    let statusText = c.last_status || '—';
    if (c.last_status === 'uploaded') {
      statusClass = 'h-status-good';
      statusEmoji = '✓';
      statusText = 'yüklendi';
    } else if (c.last_status === 'queued') {
      statusClass = 'h-status-warn';
      statusEmoji = '⏸';
      statusText = 'kuyrukta';
    } else if (c.last_status === 'produced_only') {
      statusClass = 'h-status-good';
      statusEmoji = '✓';
      statusText = 'üretildi';
    } else if (c.last_status === 'no_batch') {
      statusClass = 'h-status-warn';
      statusEmoji = '⚠';
      statusText = 'batch yok';
    } else if (c.last_status === 'produce_fail' || c.last_status === 'upload_fail') {
      statusClass = 'h-status-bad';
      statusEmoji = '✗';
      statusText = c.last_status;
    }

    // Last run zamanı
    let lastRunText = '—';
    if (c.last_run) {
      try {
        const d = new Date(c.last_run);
        lastRunText = d.toLocaleTimeString('tr-TR', {hour: '2-digit', minute: '2-digit'});
      } catch(e) {}
    }

    // YouTube link
    let linkHtml = '';
    if (c.last_youtube_url) {
      linkHtml = `<a class="h-link" href="${c.last_youtube_url}" target="_blank" title="${c.last_youtube_url}">▶ ${c.last_youtube_url.replace(/^https?:\/\//,'')}</a>`;
    }

    // Ankara için ekstra: plaka + son 24h çeşitlilik
    let plateRow = '';
    if (key === 'ankara') {
      const plate = c.last_plate || '';
      const unique = c.unique_plates_24h || 0;
      if (plate || unique) {
        plateRow = `<div style="color:#94a3b8">🚌 ${plate || '—'}` +
                   (unique ? ` · 24h: <span style="color:var(--green)">${unique} farklı plaka</span>` : '') +
                   `</div>`;
      }
    }

    return `
      <div class="harv-cell">
        <div class="h-flag">${info.flag}</div>
        <div class="h-name">${info.name} <span class="h-fmt">${info.fmt}</span></div>
        <div class="h-stats">
          <div><span class="h-success">${success}</span>/${attempts} başarılı (%${rate})</div>
          ${plateRow}
          ${queued > 0 ? `<div style="color:var(--yellow)">${queued} kuyrukta</div>` : ''}
          ${failed > 0 ? `<div style="color:var(--red)">${failed} başarısız</div>` : ''}
        </div>
        <div class="h-last">
          <span class="${statusClass}">${statusEmoji} ${statusText}</span>
          ${c.last_run ? ` · ${lastRunText}` : ''}
          ${c.last_batch ? ` · ${c.last_batch}` : ''}
          ${linkHtml}
        </div>
      </div>`;
  }).join('');
}

// ── Poll ──────────────────────────────────────────────────────────────────
async function refresh() {
  document.getElementById('log-spin').style.display = 'inline-block';
  try {
    const r    = await fetch('/api/status');
    const data = await r.json();

    // Header
    document.getElementById('clock').textContent     = data.server_time;
    document.getElementById('start-time').textContent = data.start_time
      ? 'Başlangıç: ' + data.start_time : '—';

    const dot   = document.getElementById('dot');
    const phase = document.getElementById('phase-badge');
    updateCtrlBtns(data.ok);
    if (data.ok) {
      dot.classList.add('live');
      const phaseColors = {yayında:'green', hazırlanıyor:'blue', filler:'yellow', bekliyor:'gray'};
      phase.className = 'badge ' + (phaseColors[data.phase] || 'gray');
      phase.textContent = data.phase;
    } else {
      dot.classList.remove('live');
      phase.className   = 'badge red';
      phase.textContent = 'kapalı';
    }

    // Now bar (operatöre tek bakışta ne oluyor)
    renderNow(data.current_action || '—', data.phase);

    // Speed
    const sv = document.getElementById('speed-val');
    sv.textContent = data.ok && data.frame > 0 ? data.speed.toFixed(2) + 'x' : '—';
    sv.className   = 'speed-val ' + (data.ok && data.frame > 0 ? speedColor(data.speed) : 'off');
    document.getElementById('fps-val').textContent   = data.ok ? data.fps.toFixed(0) : '—';
    document.getElementById('frame-val').textContent = data.ok ? data.frame.toLocaleString() : '—';

    // Streaming kartı
    const sb = document.getElementById('streaming-val');
    sb.textContent = data.streaming_batch || '—';
    let sub = '';
    if (data.streaming_batch) {
      sub = data.real_streaming ? '· yayın akıyor' : '· filler oynuyor';
      if (data.batch_queued) sub += ' · sırada 1 batch';
    }
    document.getElementById('streaming-sub').textContent = sub;

    // Playback progress
    renderPlayback(data);

    // Building kartı
    const bv = document.getElementById('building-val');
    bv.textContent = data.building_batch_id !== null
      ? 'Batch ' + data.building_batch_id : '—';
    let bSub = '—';
    if (data.building_batch_id !== null) {
      const nc = Object.keys(data.city_progress || {}).length;
      if (data.collector_phase_active) bSub = `${nc} şehir HLS indiriliyor`;
      else if (data.transcode_active)  bSub = `Transcode: ${data.transcode_active.city}`;
      else if (data.concat_active)     bSub = 'Concat-remux';
      else                              bSub = `${nc} şehir hazır`;
    }
    document.getElementById('building-sub').textContent = bSub;

    document.getElementById('total-batches').textContent =
      (data.batch_history || []).length;

    // Sağlık
    renderHealth(data.health || {});

    // Pipe & FIFO + ffmpeg count + relay
    renderPipeFifo(data);

    // Kaynaklar
    renderResources(data.resources || {}, data.resources_ts || '');

    // Harvester (4 şehir)
    renderHarvester(data.harvester || null);

    // Speed chart
    if (data.speed_history && data.speed_history.length > 1) {
      drawChart(data.speed_history);
    }

    // City progress + transcode chip
    if (data.building_batch_id !== null) {
      renderCities(data.city_progress, data.building_batch_id);
    } else {
      document.getElementById('cities-wrap').innerHTML =
        '<div style="color:var(--muted);font-size:12px">Batch hazırlanmıyor (kuyrukta hazır batch oynuyor olabilir)</div>';
    }
    renderTranscodeActive(data);

    // Olay timeline
    renderEvents(data.events || []);

    // Batch history
    renderBatchHistory(data.batch_history || []);

    // Logs
    renderLog(data.logs || []);

  } catch(e) {
    console.error(e);
    document.getElementById('dot').classList.remove('live');
    document.getElementById('phase-badge').className = 'badge red';
    document.getElementById('phase-badge').textContent = 'bağlantı yok';
  }
  document.getElementById('log-spin').style.display = 'none';
}

refresh();
setInterval(refresh, 3000);
window.addEventListener('resize', () => {
  fetch('/api/status').then(r => r.json()).then(d => {
    if (d.speed_history) drawChart(d.speed_history);
  });
});

// ── Stream kontrol ─────────────────────────────────────────────────────────
async function ctrlStream(action) {
  const labels = {stop:'Durduruluyor…', start:'Başlatılıyor…', restart:'Yeniden başlatılıyor…'};
  const btnId  = action === 'stop' ? 'btn-stop' : action === 'start' ? 'btn-start' : 'btn-restart';
  const btn    = document.getElementById(btnId);
  const orig   = btn.textContent;
  btn.disabled = true;
  btn.textContent = labels[action];
  try {
    const r = await fetch('/api/control', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({action})
    });
    const d = await r.json();
    if (!d.ok) alert('Hata: ' + d.error);
    else setTimeout(refresh, 1500);
  } catch(e) {
    alert('İstek başarısız: ' + e);
  } finally {
    btn.disabled = false;
    btn.textContent = orig;
  }
}

// Stop/Start butonlarını servis durumuna göre güncelle
function updateCtrlBtns(running) {
  document.getElementById('btn-stop').style.display    = running ? '' : 'none';
  document.getElementById('btn-start').style.display   = running ? 'none' : '';
  document.getElementById('btn-restart').style.display = running ? '' : 'none';
}
</script>
</body>
</html>
"""

# ─── HTTP Server ─────────────────────────────────────────────────────────────

_state: Optional[StreamState] = None

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/api/status":
            body = json.dumps(_state.snapshot()).encode()
            self._respond(200, "application/json", body)
        elif self.path in ("/", "/index.html"):
            self._respond(200, "text/html; charset=utf-8", HTML.encode())
        else:
            self._respond(404, "text/plain", b"Not found")

    def do_POST(self):
        if self.path == "/api/control":
            length = int(self.headers.get("Content-Length", 0))
            body   = self.rfile.read(length)
            try:
                data   = json.loads(body)
                action = data.get("action", "")
                cmds   = {
                    "stop":    ["systemctl", "stop",    "kamerashorts-live"],
                    "start":   ["systemctl", "start",   "kamerashorts-live"],
                    "restart": ["systemctl", "restart", "kamerashorts-live"],
                }
                if action not in cmds:
                    raise ValueError(f"Geçersiz action: {action}")
                subprocess.run(cmds[action], timeout=15, check=True)
                resp = json.dumps({"ok": True}).encode()
                self._respond(200, "application/json", resp)
            except Exception as e:
                resp = json.dumps({"ok": False, "error": str(e)}).encode()
                self._respond(500, "application/json", resp)
        else:
            self._respond(404, "text/plain", b"Not found")

    def _respond(self, code, ct, body):
        self.send_response(code)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", len(body))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass  # sessiz

# ─── Poll loop ───────────────────────────────────────────────────────────────

def _poll_loop(interval: float = 3.0):
    """Log dosyasını parse et + her 2. iterasyonda kaynak + harvester örnekleme."""
    counter = 0
    while True:
        try:
            _state.poll()
        except Exception as e:
            print(f"[poll] Hata: {e}")
        # Her 2. iterasyon = ~6s'de bir kaynak + harvester sample (CPU minimal)
        if counter % 2 == 0:
            try:
                _state.sample_resources()
            except Exception as e:
                print(f"[poll] Resource hata: {e}")
            try:
                _state.sample_harvester()
            except Exception as e:
                print(f"[poll] Harvester hata: {e}")
        counter += 1
        time.sleep(interval)

# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    global _state
    ap = argparse.ArgumentParser()
    ap.add_argument("--log",  default="/var/log/kamerashorts-live.log")
    ap.add_argument("--port", type=int, default=8080)
    args = ap.parse_args()

    _state = StreamState(args.log)
    _state.poll()  # ilk okuma
    _state.sample_resources()  # ilk kaynak örnekleme

    # Poll thread
    t = threading.Thread(target=_poll_loop, daemon=True)
    t.start()

    # HTTP server
    server = HTTPServer(("0.0.0.0", args.port), Handler)
    print(f"Dashboard: http://0.0.0.0:{args.port}")
    print(f"Log: {args.log}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDurduruldu.")


if __name__ == "__main__":
    main()
