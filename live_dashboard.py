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

        # ── TCP YouTube/Kick durumu + yayın offline tespiti ───────────────
        self._tcp_status_snap: dict = {
            "youtube": {"active": False, "remote": "", "send_q": 0},
            "kick":    {"active": False, "remote": "", "send_q": 0},
        }
        self._broadcast_offline_snap: list = []
        self._offline_since: dict = {"youtube": None, "kick": None}

        # ── Yeni: upload geçmişi, plakalar, sonraki shorts ───────────────
        self._recent_uploads: list = []
        self._ankara_plates: list = []
        self._ankara_plates_24h: int = 0
        self._next_shorts_seconds: Optional[int] = None
        self._next_shorts_time: str = ""
        self._cpu_history: deque = deque(maxlen=120)  # 6 dakika
        self._ram_history: deque = deque(maxlen=120)

        # ── Harvester pipeline detaylı state ──────────────────────────────
        self._harvester_pipeline: dict = {
            "phase": "idle",            # idle / running / finished
            "slot_start": "",
            "elapsed_s": 0,
            "current_attempt": 0,
            "total_candidates": 0,
            "current_plate": "",
            "current_vtype": "",
            "current_speed": 0,
            "current_action": "—",      # tek satır insan-okur
            "current_action_stage": "", # kayit / yolo / audio / upload
            "attempts_history": [],     # [{n, plate, vtype, result}]
            "weather": "",
            "youtube_url": "",
            "last_result": "",          # success / fail
            "last_finish": "",
        }

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

                # TCP YouTube/Kick durumu (alarm bar için)
                "tcp": self._tcp_status_snap,
                "broadcast_offline": self._broadcast_offline_snap,

                # Yeni: upload geçmişi, plakalar, geri sayım, grafikler
                "recent_uploads": self._recent_uploads,
                "ankara_plates": self._ankara_plates,
                "ankara_plates_24h": self._ankara_plates_24h,
                "next_shorts_seconds": self._next_shorts_seconds,
                "next_shorts_time": self._next_shorts_time,
                "cpu_history": list(self._cpu_history),
                "ram_history": list(self._ram_history),
                "harvester_pipeline": self._harvester_pipeline,
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

    def sample_uploads(self):
        """pipeline.log'dan son 15 UPLOADED kaydını oku."""
        try:
            log_path = Path("/opt/KameraShorts/logs/pipeline.log")
            if not log_path.exists():
                return
            with open(log_path, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - 50000))
                tail = f.read().decode("utf-8", errors="replace")
            uploads = []
            from datetime import datetime as _dt
            for line in tail.splitlines():
                if "UPLOADED" not in line:
                    continue
                try:
                    parts = line.split(" UPLOADED ", 1)
                    ts_str = parts[0]
                    rest = parts[1]
                    vid_id, _, title = rest.partition(" | ")
                    dt = _dt.fromisoformat(ts_str)
                    age = int((_dt.now() - dt).total_seconds())
                    uploads.append({
                        "video_id": vid_id.strip(),
                        "title": title.strip()[:80],
                        "time": dt.strftime("%H:%M"),
                        "date": dt.strftime("%d.%m"),
                        "age_sec": age,
                        "url": f"https://youtube.com/watch?v={vid_id.strip()}",
                    })
                except Exception:
                    continue
            uploads.reverse()
            with self._lock:
                self._recent_uploads = uploads[:15]
        except Exception as e:
            print(f"[uploads] Hata: {e}")

    def sample_plates(self):
        """harvester_ankara_plates.json'dan son 24h plakalar."""
        try:
            p = Path("/opt/KameraShorts/data/harvester_ankara_plates.json")
            if not p.exists():
                return
            data = json.loads(p.read_text(encoding="utf-8"))
            from datetime import datetime as _dt, timedelta as _td
            cutoff = _dt.now() - _td(hours=24)
            recent = []
            for plate, ts_str in data.items():
                try:
                    dt = _dt.fromisoformat(ts_str)
                    if dt > cutoff:
                        recent.append({
                            "plate": plate,
                            "time": dt.strftime("%H:%M"),
                            "age_sec": int((_dt.now() - dt).total_seconds()),
                        })
                except Exception:
                    continue
            recent.sort(key=lambda x: x["age_sec"])
            with self._lock:
                self._ankara_plates = recent[:20]
                self._ankara_plates_24h = len(recent)
        except Exception as e:
            print(f"[plates] Hata: {e}")

    def sample_next_shorts(self):
        """Sonraki Ankara Shorts saatini hesapla (her saat :15)."""
        try:
            from datetime import datetime as _dt
            now = _dt.now()
            # Bir sonraki :15
            ankara_min = 15
            if now.minute < ankara_min:
                next_dt = now.replace(minute=ankara_min, second=0,
                                       microsecond=0)
            else:
                next_dt = now.replace(minute=ankara_min, second=0,
                                       microsecond=0)
                next_dt = next_dt.replace(hour=(next_dt.hour + 1) % 24)
                if next_dt.hour == 0 and now.hour == 23:
                    from datetime import timedelta as _td
                    next_dt = next_dt + _td(days=1)
            secs = int((next_dt - now).total_seconds())
            with self._lock:
                self._next_shorts_seconds = max(0, secs)
                self._next_shorts_time = next_dt.strftime("%H:%M")
        except Exception as e:
            print(f"[next_shorts] Hata: {e}")

    def sample_harvester_pipeline(self):
        """harvester.log'u parse et — son slot detaylı state.

        Aşama akışı:
          slot başladı → hava → EGO N araç → 1/N aday seçildi →
          relay aktif → ÖN YOLO → kayıt tamamlandı → ses karıştırıldı →
          YouTube auth → yüklendi → thumbnail → playlist → plaka kayıt →
          slot bitti
        """
        try:
            log_path = Path("/opt/KameraShorts/logs/harvester.log")
            if not log_path.exists():
                return
            with open(log_path, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - 40000))
                tail = f.read().decode("utf-8", errors="replace")

            lines = tail.splitlines()
            # Son "slot başladı" satırını bul
            slot_start_idx = -1
            for i in range(len(lines) - 1, -1, -1):
                if "Ankara Shorts slot başladı" in lines[i]:
                    slot_start_idx = i
                    break

            pipe = {
                "phase": "idle",
                "slot_start": "",
                "elapsed_s": 0,
                "current_attempt": 0,
                "total_candidates": 0,
                "current_plate": "",
                "current_vtype": "",
                "current_speed": 0,
                "current_action": "Sonraki saati bekliyor",
                "current_action_stage": "",
                "attempts_history": [],
                "weather": "",
                "youtube_url": "",
                "last_result": "",
                "last_finish": "",
            }

            if slot_start_idx < 0:
                with self._lock:
                    self._harvester_pipeline = pipe
                return

            slot_lines = lines[slot_start_idx:]
            import re as _re
            from datetime import datetime as _dt

            ts_re = _re.compile(r'^(\d{4}-\d{2}-\d{2} (\d{2}:\d{2}:\d{2}))')
            start_re = _re.compile(r'Ankara Shorts slot başladı (\d{2}:\d{2})')
            weather_re = _re.compile(r'Hava: (.+)')
            ego_re = _re.compile(r"EGO'dan (\d+) aktif araç alındı")
            ego_old_re = _re.compile(r"EGO'dan (\d+) araç alındı")
            attempt_re = _re.compile(
                r"(\d+)/(\d+) → '([^']+)' \(tip: ([^,]+), hız: (\d+) km/h\)"
            )
            yolo_fail_re = _re.compile(r"\[(\S+)\] Ön YOLO: zemin/damper/karanlık")
            timeout_re = _re.compile(r"\[(\S+)\] Timeout")
            kayit_err_re = _re.compile(r"kayıt hata:")
            clip_ready_re = _re.compile(r"✓ (.+) klip hazır: (.+)")
            title_re = _re.compile(r"Başlık: (.+)")
            audio_re = _re.compile(r"Ses karıştırıldı")
            yt_auth_re = _re.compile(r"YouTube kimlik doğrulama")
            yt_upload_re = _re.compile(r"YouTube'a yuklendi: (\S+)")
            yt_thumb_re = _re.compile(r"Thumbnail yuklendi")
            yt_playlist_re = _re.compile(r"Playlist'e eklendi")
            yt_success_re = _re.compile(r"✓ Yüklendi: (\S+)")
            slot_done_re = _re.compile(r"slot bitti")
            slot_fail_re = _re.compile(
                r"Üretim başarısız → slot atlanıyor|aday tükendi"
            )

            last_ts = ""
            current_plate_raw = ""
            current_stage = "Başlıyor"

            for ln in slot_lines:
                tm = ts_re.match(ln)
                if tm:
                    last_ts = tm.group(2)
                m = start_re.search(ln)
                if m:
                    pipe["slot_start"] = m.group(1)
                    pipe["phase"] = "running"
                    pipe["current_action"] = "Slot başladı"
                    current_stage = "init"
                    continue
                m = weather_re.search(ln)
                if m:
                    pipe["weather"] = m.group(1).strip()[:40]
                    continue
                m = ego_re.search(ln) or ego_old_re.search(ln)
                if m:
                    pipe["total_candidates"] = int(m.group(1))
                    pipe["current_action"] = f"{m.group(1)} aday hazır, seçim yapılıyor"
                    current_stage = "selection"
                    continue
                m = attempt_re.search(ln)
                if m:
                    # Önceki aday varsa history'e ekle
                    if pipe["current_plate"]:
                        pipe["attempts_history"].append({
                            "n": pipe["current_attempt"],
                            "plate": pipe["current_plate"],
                            "vtype": pipe["current_vtype"],
                            "result": current_stage,
                        })
                    pipe["current_attempt"] = int(m.group(1))
                    # total_candidates eğer henüz set edilmediyse attempt'tan al
                    if not pipe["total_candidates"]:
                        pipe["total_candidates"] = int(m.group(2))
                    pipe["current_plate"] = m.group(3).strip()
                    current_plate_raw = pipe["current_plate"].replace(" ", "_")
                    pipe["current_vtype"] = m.group(4).strip()
                    try:
                        pipe["current_speed"] = int(m.group(5))
                    except Exception:
                        pipe["current_speed"] = 0
                    pipe["current_action"] = f"Aday {pipe['current_attempt']}/{pipe['total_candidates']}: {pipe['current_plate']} seçildi"
                    pipe["current_action_stage"] = "selected"
                    current_stage = "selected"
                    continue
                if "Relay renewal aktif" in ln:
                    pipe["current_action"] = f"HLS kayıt + relay TTL yenileme ({pipe['current_plate']})"
                    pipe["current_action_stage"] = "kayit"
                    current_stage = "kayit"
                    continue
                m = yolo_fail_re.search(ln)
                if m:
                    current_stage = "yolo_fail"
                    pipe["current_action"] = f"Aday {pipe['current_attempt']} YOLO eledi (zemin/karanlık), sonraki"
                    pipe["current_action_stage"] = "yolo_fail"
                    continue
                m = timeout_re.search(ln)
                if m:
                    current_stage = "timeout"
                    pipe["current_action"] = f"Aday {pipe['current_attempt']} timeout, sonraki"
                    pipe["current_action_stage"] = "timeout"
                    continue
                m = clip_ready_re.search(ln)
                if m:
                    current_stage = "yolo_pass"
                    pipe["current_action"] = f"✓ {pipe['current_plate']} YOLO geçti, klip hazır"
                    pipe["current_action_stage"] = "yolo_pass"
                    continue
                if audio_re.search(ln):
                    current_stage = "audio_done"
                    pipe["current_action"] = "Ses karıştırma tamamlandı (TTS + ambient)"
                    pipe["current_action_stage"] = "audio"
                    continue
                if yt_auth_re.search(ln):
                    current_stage = "yt_auth"
                    pipe["current_action"] = "YouTube auth"
                    pipe["current_action_stage"] = "upload"
                    continue
                m = yt_upload_re.search(ln)
                if m:
                    pipe["youtube_url"] = m.group(1).strip()
                    current_stage = "yt_uploaded"
                    pipe["current_action"] = "YouTube'a yüklendi (thumbnail bekliyor)"
                    pipe["current_action_stage"] = "upload"
                    continue
                if yt_thumb_re.search(ln):
                    pipe["current_action"] = "Thumbnail yüklendi (playlist bekliyor)"
                    pipe["current_action_stage"] = "upload"
                    continue
                if yt_playlist_re.search(ln):
                    pipe["current_action"] = "Playlist'e eklendi (✓ tamamlanıyor)"
                    pipe["current_action_stage"] = "upload"
                    continue
                m = yt_success_re.search(ln)
                if m:
                    pipe["youtube_url"] = m.group(1).strip()
                    pipe["last_result"] = "success"
                    pipe["current_action"] = "✓ BAŞARILI: YouTube'da yayında"
                    pipe["current_action_stage"] = "success"
                    current_stage = "success"
                    continue
                if slot_done_re.search(ln):
                    pipe["phase"] = "finished"
                    pipe["last_finish"] = last_ts
                    if pipe["last_result"] != "success":
                        pipe["current_action"] = "Slot bitti (sonuç: yok)"
                    else:
                        pipe["current_action"] = f"Slot tamamlandı ({pipe['slot_start']})"
                    continue
                if slot_fail_re.search(ln):
                    pipe["phase"] = "finished"
                    pipe["last_result"] = "fail"
                    pipe["current_action"] = "Tüm adaylar başarısız, slot atlandı"
                    pipe["current_action_stage"] = "fail"
                    continue

            # Son denemeyi history'e ekle (eğer bitmemişse current olarak kal)
            if pipe["phase"] == "finished" and pipe["current_plate"]:
                pipe["attempts_history"].append({
                    "n": pipe["current_attempt"],
                    "plate": pipe["current_plate"],
                    "vtype": pipe["current_vtype"],
                    "result": current_stage,
                })

            # Elapsed hesap
            if pipe["slot_start"]:
                try:
                    now = _dt.now()
                    h, m = pipe["slot_start"].split(":")
                    start_dt = now.replace(hour=int(h), minute=int(m),
                                            second=0, microsecond=0)
                    if start_dt > now:  # ertesi gün durumu
                        from datetime import timedelta as _td
                        start_dt -= _td(days=1)
                    pipe["elapsed_s"] = int((now - start_dt).total_seconds())
                except Exception:
                    pass

            # En son 5 attempt history'i tut
            pipe["attempts_history"] = pipe["attempts_history"][-8:]

            with self._lock:
                self._harvester_pipeline = pipe
        except Exception as e:
            print(f"[harv_pipe] Hata: {e}")

    def sample_cpu_ram_history(self):
        """Son resources okumadan grafikler için zaman serisi tut."""
        try:
            ts = time.strftime("%H:%M:%S")
            with self._lock:
                self._cpu_history.append({
                    "t": ts,
                    "load": self.resources.get("load1", 0),
                    "ffmpeg": self.resources.get("ffmpeg_total_cpu", 0),
                })
                self._ram_history.append({
                    "t": ts,
                    "used": self.resources.get("mem_used_mb", 0),
                })
        except Exception as e:
            print(f"[history] Hata: {e}")

    def sample_tcp(self):
        """TCP YouTube/Kick durumu + offline tespiti (alarm bar için)."""
        try:
            r = subprocess.run(
                ["ss", "-tnp"], capture_output=True, text=True, timeout=3,
            )
            yt = None
            kick = None
            for line in r.stdout.splitlines():
                if "ffmpeg" not in line:
                    continue
                parts = line.split()
                if len(parts) < 5:
                    continue
                try:
                    send_q = int(parts[2])
                    remote = parts[4]
                except Exception:
                    continue
                if remote.endswith(":1935") and "127.0.0.1" not in remote:
                    yt = {"active": True, "remote": remote, "send_q": send_q}
                elif ":443" in remote and (remote.startswith("35.") or
                                            "live-video" in remote):
                    kick = {"active": True, "remote": remote, "send_q": send_q}
            now = int(time.time())
            tcp = {
                "youtube": yt or {"active": False, "remote": "", "send_q": 0},
                "kick":    kick or {"active": False, "remote": "", "send_q": 0},
            }
            # Offline tracking
            offline = []
            for k in ("youtube", "kick"):
                if not tcp[k]["active"]:
                    if self._offline_since[k] is None:
                        self._offline_since[k] = now
                    secs = now - self._offline_since[k]
                    offline.append({"name": k.upper(), "since_seconds": secs})
                else:
                    self._offline_since[k] = None
            with self._lock:
                self._tcp_status_snap = tcp
                self._broadcast_offline_snap = offline
        except Exception as e:
            print(f"[tcp] Hata: {e}")

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
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#0a0f1c">
<title>KameraShorts — Canlı Dashboard</title>
<style>
:root{
  --bg:#0a0f1c; --bg2:#0f172a; --card:#111827; --card2:#1e293b; --line:#1f2937;
  --green:#22c55e; --yellow:#f59e0b; --red:#ef4444; --blue:#3b82f6;
  --purple:#8b5cf6; --indigo:#6366f1; --pink:#ec4899; --cyan:#06b6d4;
  --text:#e2e8f0; --text2:#cbd5e1; --muted:#64748b; --muted2:#475569;
  --shadow:0 8px 24px rgba(0,0,0,.4);
  --r-sm:6px; --r-md:10px; --r-lg:14px;
}
*{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}
html,body{background:var(--bg);color:var(--text);font-family:ui-sans-serif,'SF Pro Display',-apple-system,Segoe UI,Inter,sans-serif;font-size:13px;line-height:1.5;min-height:100vh;-webkit-font-smoothing:antialiased}
a{color:#93c5fd;text-decoration:none}
a:hover{color:#bfdbfe;text-decoration:underline}
button{font-family:inherit}
::-webkit-scrollbar{width:8px;height:8px}
::-webkit-scrollbar-thumb{background:#334155;border-radius:4px}
::-webkit-scrollbar-track{background:transparent}

.wrap{max-width:1400px;margin:0 auto;padding:14px}

/* ─── HEADER ─── */
.header{display:flex;align-items:center;gap:12px;padding:12px 16px;
        background:linear-gradient(180deg,#111827,#0a0f1c);border:1px solid var(--line);
        border-radius:var(--r-md);margin-bottom:12px;flex-wrap:wrap}
.brand{display:flex;align-items:center;gap:10px;flex:1;min-width:200px}
.brand h1{font-size:16px;font-weight:700;letter-spacing:0.02em;color:#fff}
.brand h1 .v{color:var(--indigo);font-weight:600;font-size:13px;margin-left:4px}
.status-dot{width:10px;height:10px;border-radius:50%;background:var(--muted);
            box-shadow:0 0 0 0 rgba(100,116,139,.4);transition:.3s}
.status-dot.live{background:var(--green);box-shadow:0 0 0 6px rgba(34,197,94,.2);
                 animation:pulse 2.5s infinite}
.status-dot.offline{background:var(--red);box-shadow:0 0 0 6px rgba(239,68,68,.3)}
@keyframes pulse{0%,100%{box-shadow:0 0 0 0 rgba(34,197,94,.6)}50%{box-shadow:0 0 0 10px rgba(34,197,94,0)}}
.header .meta{color:var(--muted);font-size:11px;font-family:ui-monospace,monospace;margin-left:auto}

/* ─── ALARM BAR ─── */
.alarm-bar{display:none;background:linear-gradient(90deg,#7f1d1d,#450a0a 60%,#7f1d1d);
           background-size:200% 100%;animation:alarm-bg 2s linear infinite;
           border:1px solid var(--red);border-radius:var(--r-md);padding:14px 18px;
           margin-bottom:12px;align-items:center;gap:14px}
.alarm-bar.show{display:flex}
@keyframes alarm-bg{0%{background-position:0% 0%}100%{background-position:200% 0%}}
.alarm-icon{font-size:32px;animation:shake .5s infinite}
@keyframes shake{0%,100%{transform:rotate(-8deg)}50%{transform:rotate(8deg)}}
.alarm-content{flex:1}
.alarm-title{font-size:15px;font-weight:800;color:#fff;letter-spacing:0.02em}
.alarm-detail{font-size:11px;color:#fca5a5;margin-top:3px}

/* ─── GRID ─── */
.grid{display:grid;gap:12px;margin-bottom:12px}
.g2{grid-template-columns:1fr 1fr}
.g3{grid-template-columns:repeat(3,1fr)}
.g4{grid-template-columns:repeat(4,1fr)}
.g6{grid-template-columns:repeat(6,1fr)}
.g21{grid-template-columns:2fr 1fr}
.g12{grid-template-columns:1fr 2fr}
@media(max-width:1100px){.g4{grid-template-columns:repeat(2,1fr)}.g6{grid-template-columns:repeat(3,1fr)}}
@media(max-width:768px){.g2,.g3,.g4,.g21,.g12{grid-template-columns:1fr}.g6{grid-template-columns:repeat(2,1fr)}}

/* ─── CARD ─── */
.card{background:var(--card);border:1px solid var(--line);border-radius:var(--r-md);
      padding:16px;position:relative;overflow:hidden}
.card h2{font-size:10px;font-weight:700;letter-spacing:0.14em;color:var(--muted);
         text-transform:uppercase;margin-bottom:10px;display:flex;align-items:center;gap:8px}
.card h2 .badge{font-size:10px;padding:1px 8px;border-radius:99px;letter-spacing:0.05em}

/* ─── HERO 1: Şu an yayında ─── */
.hero-now{background:linear-gradient(135deg,#1e1b4b 0%,#0a0f1c 100%);
          border:1px solid #312e81;position:relative}
.hero-now::before{content:"";position:absolute;top:-50%;right:-20%;width:300px;height:300px;
                  border-radius:50%;background:radial-gradient(circle,rgba(99,102,241,.25),transparent 70%);pointer-events:none}
.hero-now .city{font-size:36px;font-weight:900;color:#fff;letter-spacing:0.04em;line-height:1}
.hero-now .substatus{display:flex;gap:6px;flex-wrap:wrap;margin-top:10px}
.hero-now .metrics{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-top:14px;position:relative}
@media(max-width:520px){.hero-now .metrics{grid-template-columns:repeat(2,1fr)}}
.metric{padding:8px 0}
.metric .lbl{font-size:9px;color:rgba(255,255,255,.5);text-transform:uppercase;letter-spacing:0.1em}
.metric .val{font-size:24px;font-weight:800;color:#fff;line-height:1.1;margin-top:2px}
.metric .sub{font-size:10px;color:rgba(255,255,255,.4);margin-top:2px}
.progress{height:8px;background:rgba(255,255,255,.08);border-radius:4px;overflow:hidden;margin-top:14px;position:relative}
.progress-fill{height:100%;background:linear-gradient(90deg,var(--blue),var(--green));border-radius:4px;transition:width 1s linear}
.progress-meta{display:flex;justify-content:space-between;font-size:10px;color:rgba(255,255,255,.5);margin-top:6px}

/* ─── HERO 2: Sonraki shorts ─── */
.hero-shorts{background:linear-gradient(135deg,#1e3a8a 0%,#0a0f1c 100%);
             border:1px solid #1d4ed8;position:relative}
.hero-shorts::before{content:"";position:absolute;top:-30%;left:-10%;width:280px;height:280px;
                     border-radius:50%;background:radial-gradient(circle,rgba(59,130,246,.25),transparent 70%);pointer-events:none}
.shorts-countdown{font-size:42px;font-weight:900;color:#fff;text-align:center;line-height:1;margin:8px 0;font-variant-numeric:tabular-nums}
.shorts-countdown.imminent{color:var(--yellow);animation:cd-pulse 1s infinite}
@keyframes cd-pulse{50%{opacity:.6}}
.shorts-meta{text-align:center;color:rgba(255,255,255,.6);font-size:11px;margin-top:6px}
.shorts-last{margin-top:14px;padding:10px;background:rgba(255,255,255,.05);border-radius:var(--r-sm)}
.shorts-last .label{font-size:9px;color:rgba(255,255,255,.5);text-transform:uppercase;letter-spacing:0.1em;margin-bottom:4px}
.shorts-last .title{font-size:11px;color:#cbd5e1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.shorts-last .url{display:inline-block;margin-top:4px;font-size:10px;color:#93c5fd}

/* ─── PILL / BADGES ─── */
.pill{display:inline-flex;align-items:center;gap:6px;padding:3px 10px;border-radius:99px;
      font-size:11px;font-weight:600;font-family:inherit}
.pill .d{width:8px;height:8px;border-radius:50%;flex-shrink:0}
.pill.ok{background:rgba(34,197,94,.15);color:var(--green)}
.pill.ok .d{background:var(--green);box-shadow:0 0 6px var(--green)}
.pill.warn{background:rgba(245,158,11,.15);color:var(--yellow)}
.pill.warn .d{background:var(--yellow)}
.pill.bad{background:rgba(239,68,68,.15);color:var(--red)}
.pill.bad .d{background:var(--red);box-shadow:0 0 6px var(--red)}
.pill.info{background:rgba(99,102,241,.15);color:#a5b4fc}
.pill.info .d{background:var(--indigo)}
.pill.muted{background:rgba(100,116,139,.15);color:var(--muted)}
.pill.muted .d{background:var(--muted)}

/* ─── METRIC CARDS (4 sutun) ─── */
.mcard{background:var(--card);border:1px solid var(--line);border-radius:var(--r-md);
       padding:14px;position:relative;transition:transform .2s,box-shadow .2s}
.mcard:hover{transform:translateY(-2px);box-shadow:var(--shadow)}
.mcard .top{display:flex;align-items:center;justify-content:space-between;margin-bottom:8px}
.mcard .icon{font-size:18px}
.mcard .label{font-size:9px;color:var(--muted);text-transform:uppercase;letter-spacing:0.1em}
.mcard .value{font-size:22px;font-weight:800;color:#fff;line-height:1.1}
.mcard .sub{font-size:10px;color:var(--muted);margin-top:3px;font-family:monospace;
            white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.mcard.good{border-color:rgba(34,197,94,.3)}
.mcard.bad{border-color:rgba(239,68,68,.3)}
.mcard.warn{border-color:rgba(245,158,11,.3)}

/* ─── CITY ROWS (BATCH PROGRESS) ─── */
.city-row{display:flex;align-items:center;gap:10px;padding:9px 12px;background:var(--card2);
          border-radius:var(--r-sm);margin-bottom:5px;font-size:12px}
.city-row .flag{font-size:18px;width:24px;text-align:center;flex-shrink:0}
.city-row .name{width:90px;font-weight:600;color:var(--text);flex-shrink:0}
.city-row .bar{flex:1;height:6px;background:rgba(255,255,255,.06);border-radius:3px;overflow:hidden}
.city-row .bar-fill{height:100%;border-radius:3px;transition:width .5s}
.city-row .bar-fill.green{background:var(--green)}
.city-row .bar-fill.yellow{background:var(--yellow)}
.city-row .bar-fill.blue{background:var(--blue)}
.city-row .bar-fill.red{background:var(--red)}
.city-row .bar-fill.gray{background:var(--muted)}
.city-row .stats{width:120px;text-align:right;color:var(--muted);font-size:10px;font-family:monospace;flex-shrink:0}
.city-row .status{width:80px;text-align:right;flex-shrink:0;font-size:10px}

/* ─── BATCH ITEMS ─── */
.batch-list{max-height:280px;overflow-y:auto}
.batch-item{display:flex;align-items:center;gap:8px;padding:7px 10px;background:var(--card2);
            border-radius:var(--r-sm);margin-bottom:4px;font-size:11px}
.batch-item .id{font-weight:700;color:#a5b4fc;width:40px;flex-shrink:0}
.batch-item .name{flex:1;color:var(--text2);font-family:monospace;font-size:10px;
                  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.batch-item .meta{display:flex;gap:8px;flex-shrink:0;color:var(--muted);font-size:10px}

/* ─── RESOURCE BARS ─── */
.res-card{background:var(--card);border:1px solid var(--line);border-radius:var(--r-md);padding:14px}
.res-card .top{display:flex;align-items:center;justify-content:space-between;margin-bottom:8px}
.res-card .lbl{font-size:9px;color:var(--muted);text-transform:uppercase;letter-spacing:0.1em}
.res-card .val{font-size:18px;font-weight:700;color:#fff;font-variant-numeric:tabular-nums}
.res-bar{height:6px;background:rgba(255,255,255,.06);border-radius:3px;overflow:hidden;margin-top:8px}
.res-fill{height:100%;border-radius:3px;transition:width .5s}
.res-fill.low{background:linear-gradient(90deg,#22c55e,#84cc16)}
.res-fill.mid{background:linear-gradient(90deg,#f59e0b,#fbbf24)}
.res-fill.high{background:linear-gradient(90deg,#ef4444,#f87171)}
.res-foot{font-size:10px;color:var(--muted);margin-top:4px;font-family:monospace}
.spark{margin-top:6px;height:30px}

/* ─── UPLOADS TABLE ─── */
.uploads{width:100%;border-collapse:collapse;font-size:11px}
.uploads th{text-align:left;padding:6px 10px;color:var(--muted);text-transform:uppercase;
            font-size:9px;letter-spacing:0.08em;border-bottom:1px solid var(--line);font-weight:700}
.uploads td{padding:8px 10px;border-bottom:1px solid var(--line);color:var(--text2)}
.uploads tr:hover td{background:rgba(255,255,255,.02)}
.uploads .ok{color:var(--green)}
.uploads .title{max-width:300px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.uploads .watch{font-size:10px;padding:3px 8px;border-radius:4px;background:#1e3a8a;color:#bfdbfe}
.uploads .watch:hover{background:#1d4ed8;color:#fff}

/* ─── PLATE CHIPS ─── */
.plates{display:flex;flex-wrap:wrap;gap:6px}
.plate{padding:5px 10px;background:rgba(99,102,241,.15);border:1px solid rgba(99,102,241,.3);
       border-radius:6px;font-size:11px;color:#c7d2fe;font-family:monospace;
       transition:.2s}
.plate:hover{background:rgba(99,102,241,.3);transform:translateY(-1px)}
.plate .pt{font-size:9px;color:#a5b4fc;margin-left:4px;opacity:.8}

/* ─── EVENTS / LOGS ─── */
.events,.logs{max-height:340px;overflow-y:auto}
.events::-webkit-scrollbar,.logs::-webkit-scrollbar{width:6px}
.ev,.log-line{display:flex;gap:10px;padding:6px 10px;border-radius:var(--r-sm);
              margin-bottom:3px;background:var(--card2);font-size:11px;align-items:center}
.ev .t,.log-line .ts{color:var(--muted2);font-size:10px;width:60px;flex-shrink:0;font-family:monospace}
.ev .tag{flex-shrink:0;padding:1px 7px;border-radius:3px;font-size:9px;font-weight:700;
         letter-spacing:0.04em;width:70px;text-align:center}
.ev .tag.info{background:rgba(99,102,241,.15);color:#a5b4fc}
.ev .tag.warn{background:rgba(245,158,11,.15);color:var(--yellow)}
.ev .tag.error,.ev .tag.bad{background:rgba(239,68,68,.15);color:var(--red)}
.ev .tag.good{background:rgba(34,197,94,.15);color:var(--green)}
.ev .msg,.log-line .msg{flex:1;color:var(--text2);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.log-line .lvl{flex-shrink:0;width:50px;font-weight:700;font-size:9px}
.log-line .lvl.INFO{color:var(--muted)}
.log-line .lvl.WARNING{color:var(--yellow)}
.log-line .lvl.ERROR{color:var(--red)}

/* ─── Stream mesaj radio button ─── */
.sm-dur{display:inline-flex;align-items:center;gap:4px;padding:4px 10px;
        background:var(--card2);border:1px solid var(--line);border-radius:5px;
        font-size:11px;color:var(--text2);cursor:pointer;transition:.15s}
.sm-dur:hover{background:#1f2937;border-color:var(--blue)}
.sm-dur input{accent-color:var(--blue);cursor:pointer}
.sm-dur:has(input:checked){background:rgba(59,130,246,.15);border-color:var(--blue);color:#fff}

/* ─── CONTROL PANEL ─── */
.controls{display:flex;flex-wrap:wrap;gap:8px}
.ctrl-btn{padding:9px 16px;background:var(--card2);border:1px solid var(--line);
          color:var(--text);border-radius:var(--r-sm);cursor:pointer;font-size:12px;
          font-weight:600;transition:.15s}
.ctrl-btn:hover{background:#334155;border-color:var(--blue)}
.ctrl-btn.danger{background:rgba(239,68,68,.1);border-color:rgba(239,68,68,.3);color:#fca5a5}
.ctrl-btn.danger:hover{background:rgba(239,68,68,.2)}
.ctrl-btn.warn{background:rgba(245,158,11,.1);border-color:rgba(245,158,11,.3);color:#fcd34d}
.ctrl-btn.warn:hover{background:rgba(245,158,11,.2)}

/* ─── FAB (sağ alt buttonlar) ─── */
.fab-group{position:fixed;right:20px;bottom:20px;display:flex;flex-direction:column;gap:10px;z-index:99}
.fab{width:48px;height:48px;border-radius:50%;border:none;font-size:22px;font-weight:700;
     cursor:pointer;box-shadow:var(--shadow);transition:transform .2s}
.fab:hover{transform:scale(1.08)}
.fab.help{background:var(--indigo);color:#fff}
.fab.diag{background:#fbbf24;color:#7c2d12}
.fab.diag.loading{animation:spin 1s linear infinite}
.fab.sound{background:var(--card2);color:#fff;font-size:18px}
.fab.sound.muted{opacity:.5}
@keyframes spin{to{transform:rotate(360deg)}}

/* ─── MODAL ─── */
.modal-bg{display:none;position:fixed;inset:0;background:rgba(0,0,0,.8);z-index:100;
          align-items:flex-start;justify-content:center;padding:30px 20px;overflow-y:auto;
          backdrop-filter:blur(4px)}
.modal-bg.open{display:flex}
.modal-x{background:#0f172a;border:1px solid var(--line);border-radius:var(--r-lg);
         max-width:880px;width:100%;padding:24px;position:relative;color:var(--text);
         box-shadow:0 20px 60px rgba(0,0,0,.5)}
.modal-x h2{font-size:20px;color:#fff;margin-bottom:6px}
.modal-x h3{font-size:13px;color:#a5b4fc;margin-top:14px;margin-bottom:6px;
            text-transform:uppercase;letter-spacing:0.06em}
.modal-x p,.modal-x li{font-size:12px;line-height:1.6;color:var(--text2)}
.modal-x ul{padding-left:20px;margin-bottom:8px}
.modal-x code{background:var(--card2);padding:2px 6px;border-radius:3px;color:#fbbf24;
              font-family:monospace;font-size:11px}
.modal-close{position:absolute;top:14px;right:14px;background:none;border:none;
             color:var(--muted);font-size:24px;cursor:pointer;width:32px;height:32px;
             border-radius:50%;display:flex;align-items:center;justify-content:center}
.modal-close:hover{background:var(--card2);color:#fff}

/* ─── DIAGNOSE result ─── */
.diag-row{display:flex;gap:10px;padding:8px 12px;background:var(--card2);border-radius:var(--r-sm);
          margin-bottom:5px;align-items:flex-start}
.diag-sec{flex-shrink:0;padding:2px 8px;background:rgba(239,68,68,.2);color:var(--red);
          border-radius:3px;font-size:10px;font-weight:700;text-transform:uppercase}
.diag-msg{flex:1;font-size:11.5px;color:var(--text2)}
.diag-ok{padding:30px;text-align:center;background:rgba(34,197,94,.1);border-radius:8px;
         color:var(--green);font-weight:600;font-size:15px}

/* ─── EMPTY STATE ─── */
.empty{text-align:center;color:var(--muted);padding:24px 16px;font-size:11px;font-style:italic}

/* ─── SPARK CANVAS ─── */
canvas.spark-canvas{width:100%;height:30px;display:block}

/* ─── ANKARA PIPELINE (Shorts üretim aşaması) ─── */
.ap-card{background:linear-gradient(135deg,#312e81 0%,#0a0f1c 100%);
         border:1px solid #4338ca;position:relative;overflow:hidden}
.ap-card::before{content:"";position:absolute;top:-30%;right:-15%;width:280px;height:280px;
                 border-radius:50%;background:radial-gradient(circle,rgba(99,102,241,.2),transparent 70%);pointer-events:none}
.ap-row{display:flex;gap:14px;flex-wrap:wrap;align-items:center;margin-bottom:14px;
        position:relative;z-index:1}
.ap-current{flex:1;min-width:240px}
.ap-action{font-size:15px;font-weight:600;color:#fff;margin-bottom:8px;line-height:1.4}
.ap-meta{display:flex;gap:8px;font-size:11px;color:var(--muted);flex-wrap:wrap}
.ap-meta > span{padding:4px 10px;background:rgba(255,255,255,.06);border-radius:4px;font-family:monospace}
.ap-meta .ap-attempt-tag{color:#a5b4fc;font-weight:600}
.ap-meta .ap-plate-tag{color:#fbbf24}
.ap-stages{display:flex;gap:6px;flex-wrap:wrap}
.ap-stage{display:flex;flex-direction:column;align-items:center;padding:8px 12px;
          background:rgba(255,255,255,.04);border-radius:6px;opacity:.35;transition:.3s;
          min-width:62px;border:1px solid transparent}
.ap-stage.active{opacity:1;background:rgba(99,102,241,.25);border-color:var(--indigo);
                 box-shadow:0 0 12px rgba(99,102,241,.4);animation:stage-glow 2s infinite}
.ap-stage.done{opacity:.85;background:rgba(34,197,94,.15);border-color:rgba(34,197,94,.4)}
.ap-stage.fail{opacity:.85;background:rgba(239,68,68,.15);border-color:rgba(239,68,68,.4)}
@keyframes stage-glow{50%{box-shadow:0 0 22px rgba(99,102,241,.65)}}
.ap-stage .ap-icon{font-size:18px;margin-bottom:2px}
.ap-stage .ap-lbl{font-size:9px;color:var(--text2);text-transform:uppercase;letter-spacing:0.05em}
.ap-attempts{margin-top:12px;position:relative;z-index:1}
.ap-attempt-list{max-height:220px;overflow-y:auto}
.ap-attempt-row{display:flex;align-items:center;gap:10px;padding:6px 10px;
                background:rgba(255,255,255,.04);border-radius:5px;margin-bottom:3px;font-size:11px}
.ap-attempt-row .n{color:#a5b4fc;font-weight:700;width:48px;flex-shrink:0;font-family:monospace}
.ap-attempt-row .p{color:#fbbf24;font-family:monospace;flex-shrink:0;width:90px}
.ap-attempt-row .t{flex:1;color:var(--muted);font-size:10px;
                   white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.ap-attempt-row .r{padding:2px 8px;border-radius:3px;font-size:9px;font-weight:700;flex-shrink:0}
.ap-attempt-row .r.ok{background:rgba(34,197,94,.2);color:var(--green)}
.ap-attempt-row .r.warn{background:rgba(245,158,11,.2);color:var(--yellow)}
.ap-attempt-row .r.bad{background:rgba(239,68,68,.2);color:var(--red)}
.ap-attempt-row .r.info{background:rgba(99,102,241,.2);color:#a5b4fc}
.ap-progress-wrap{margin-top:14px;position:relative;z-index:1}

.section-title{font-size:11px;font-weight:700;letter-spacing:0.12em;color:var(--muted);
               text-transform:uppercase;margin-bottom:10px;margin-top:4px}

/* Mobile fine-tune */
@media(max-width:600px){
  .wrap{padding:10px}
  .hero-now .city{font-size:28px}
  .shorts-countdown{font-size:34px}
  .mcard .value{font-size:18px}
}
</style>
</head>
<body>

<!-- Floating buttons -->
<div class="fab-group">
  <button class="fab sound" id="fab-sound" title="Sesli alarm aç/kapat (m)">🔔</button>
  <button class="fab diag" id="fab-diag" title="Sistem tanı (d)">🔍</button>
  <button class="fab help" id="fab-help" title="Sistem rehberi (?)">?</button>
</div>

<!-- Help modal -->
<div class="modal-bg" id="modal-help">
  <div class="modal-x">
    <button class="modal-close" id="close-help">×</button>
    <h2>KameraShorts v4 — Sistem Rehberi</h2>
    <p style="color:var(--muted);font-size:11px">Klavye: <code>?</code> yardım, <code>d</code> tanı, <code>m</code> ses, <code>Esc</code> kapat</p>

    <h3>Mimari</h3>
    <p><b>Stream</b> (live_streamer.py): 4 şehir paralel HLS dl → transcode_city normalize → batch_NNNN.ts → tek FFmpeg + FIFO writer → MediaMTX → YouTube + Kick</p>
    <p><b>Ankara Shorts</b> (harvester.py): Stream'den BAĞIMSIZ. Saat :15'te EGO API → 80 araç → YOLO subprocess → 40s dikey kayıt → YouTube upload</p>

    <h3>Servisler</h3>
    <ul>
      <li><code>kamerashorts-live</code> — 4 şehir canlı yayın</li>
      <li><code>kamerashorts-harvester</code> — Ankara Shorts (saatlik :15)</li>
      <li><code>kamerashorts-dashboard</code> — bu sayfa (port 5000)</li>
      <li><code>mediamtx</code> — RTMP tee → YouTube + Kick</li>
    </ul>

    <h3>Sorun çözme</h3>
    <ul>
      <li><b>Kırmızı alarm bar</b>: <code>systemctl restart mediamtx</code> + 10s bekle</li>
      <li><b>Frame counter donmuş</b>: <code>systemctl restart kamerashorts-live</code></li>
      <li><b>Speed &lt;0.85x</b>: CPU bottleneck — batch transcode'da geçici normal</li>
      <li><b>Son upload &gt;2sa önce</b>: <code>journalctl -u kamerashorts-harvester -f</code></li>
      <li><b>Hızlı tanı</b>: 🔍 düğmesi sağ alt — 10 bölüm tarar</li>
    </ul>

    <h3>Veri yolları</h3>
    <ul>
      <li><code>/opt/KameraShorts/</code> kaynak</li>
      <li><code>/tmp/ks_v4/batch_*.ts</code> aktif batch'ler</li>
      <li><code>/var/log/kamerashorts-live.log</code> stream log</li>
      <li><code>/opt/KameraShorts/logs/pipeline.log</code> upload kayıtları</li>
      <li><code>/etc/kamerashorts/secrets.env</code> YT/Kick keys</li>
    </ul>
  </div>
</div>

<!-- Diagnose result modal -->
<div class="modal-bg" id="modal-diag">
  <div class="modal-x">
    <button class="modal-close" id="close-diag">×</button>
    <h2>🔍 Sistem Tanı</h2>
    <p style="color:var(--muted);font-size:11px" id="diag-ts">—</p>
    <div id="diag-body" style="margin-top:14px">Tanı başlatılıyor...</div>
  </div>
</div>

<audio id="alarm-audio" preload="auto">
  <source src="data:audio/wav;base64,UklGRrwBAABXQVZFZm10IBAAAAABAAEARKwAAIhYAQACABAAZGF0YZgBAAAAAAAATg2RGAUiBhqQEPMG7AAAAAAAAAAAAAAAAA" type="audio/wav">
</audio>

<div class="wrap">

  <!-- HEADER -->
  <div class="header">
    <div class="brand">
      <div class="status-dot" id="status-dot"></div>
      <h1>🎥 KameraShorts <span class="v">v4</span></h1>
    </div>
    <div class="meta" id="header-meta">—</div>
  </div>

  <!-- ALARM BAR -->
  <div class="alarm-bar" id="alarm-bar">
    <div class="alarm-icon">🚨</div>
    <div class="alarm-content">
      <div class="alarm-title" id="alarm-title">YAYIN OFFLINE</div>
      <div class="alarm-detail" id="alarm-detail"></div>
    </div>
  </div>

  <!-- HERO 1: Şu an yayında + Sonraki shorts -->
  <div class="grid g2">
    <div class="card hero-now">
      <h2>🎬 Şu Anda Yayında</h2>
      <div class="city" id="now-city">—</div>
      <div class="substatus" id="now-pills"></div>
      <div class="metrics">
        <div class="metric"><div class="lbl">Speed</div><div class="val" id="now-speed">—</div><div class="sub">hedef 1.00x</div></div>
        <div class="metric"><div class="lbl">FPS</div><div class="val" id="now-fps">—</div><div class="sub" id="now-frame">frame —</div></div>
        <div class="metric"><div class="lbl">Phase</div><div class="val" id="now-phase">—</div><div class="sub" id="now-action">—</div></div>
        <div class="metric"><div class="lbl">Aşama</div><div class="val" id="now-bn">—</div><div class="sub" id="now-bn-sub">—</div></div>
      </div>
      <div class="progress"><div class="progress-fill" id="now-prog" style="width:0%"></div></div>
      <div class="progress-meta">
        <span id="now-prog-played">—</span>
        <span id="now-prog-total">—</span>
      </div>
    </div>

    <div class="card hero-shorts">
      <h2>📱 Sonraki Ankara Shorts</h2>
      <div class="shorts-countdown" id="shorts-cd">—</div>
      <div class="shorts-meta" id="shorts-time">timer hazır değil</div>
      <div class="shorts-last">
        <div class="label">Son Upload</div>
        <div class="title" id="shorts-last-title">—</div>
        <a class="url" id="shorts-last-url" href="#" target="_blank">—</a>
      </div>
    </div>
  </div>

  <!-- METRIC ROW (4 kolon) -->
  <div class="grid g4">
    <div class="mcard" id="mc-yt">
      <div class="top"><span class="label">YouTube RTMP</span><span class="icon">📺</span></div>
      <div class="value" id="yt-status">—</div>
      <div class="sub" id="yt-detail">—</div>
    </div>
    <div class="mcard" id="mc-kick">
      <div class="top"><span class="label">Kick RTMPS</span><span class="icon">🎮</span></div>
      <div class="value" id="kick-status">—</div>
      <div class="sub" id="kick-detail">—</div>
    </div>
    <div class="mcard">
      <div class="top"><span class="label">FFmpeg Toplam</span><span class="icon">⚡</span></div>
      <div class="value" id="ff-cpu">— %</div>
      <div class="sub" id="ff-count">— adet</div>
    </div>
    <div class="mcard">
      <div class="top"><span class="label">Bugün Upload</span><span class="icon">📤</span></div>
      <div class="value" id="up-today">— </div>
      <div class="sub" id="up-today-sub">Ankara</div>
    </div>
  </div>

  <!-- 4 ŞEHİR BATCH PROGRESS -->
  <div class="card">
    <h2>🌆 4 Şehir Batch İlerlemesi <span class="badge pill info"><span class="d"></span>aktif</span></h2>
    <div id="cities"></div>
  </div>

  <!-- ANKARA PIPELINE (Shorts üretim aşaması) -->
  <div class="card ap-card">
    <h2>🚌 Ankara Shorts Pipeline <span class="badge pill muted" id="ap-phase"><span class="d"></span>—</span></h2>
    <div class="ap-row">
      <div class="ap-current">
        <div class="ap-action" id="ap-action">Sonraki saati bekliyor</div>
        <div class="ap-meta">
          <span class="ap-attempt-tag" id="ap-attempt">—/—</span>
          <span class="ap-plate-tag" id="ap-plate">—</span>
          <span id="ap-vtype">—</span>
          <span id="ap-weather">—</span>
        </div>
      </div>
      <div class="ap-stages">
        <div class="ap-stage" data-stage="select"><span class="ap-icon">🔍</span><span class="ap-lbl">Seçim</span></div>
        <div class="ap-stage" data-stage="kayit"><span class="ap-icon">📹</span><span class="ap-lbl">Kayıt</span></div>
        <div class="ap-stage" data-stage="yolo"><span class="ap-icon">🧠</span><span class="ap-lbl">YOLO</span></div>
        <div class="ap-stage" data-stage="audio"><span class="ap-icon">🎵</span><span class="ap-lbl">Ses</span></div>
        <div class="ap-stage" data-stage="upload"><span class="ap-icon">📤</span><span class="ap-lbl">Upload</span></div>
        <div class="ap-stage" data-stage="success"><span class="ap-icon">✓</span><span class="ap-lbl">OK</span></div>
      </div>
    </div>
    <div class="ap-progress-wrap">
      <div class="progress"><div class="progress-fill" id="ap-prog-fill" style="width:0%"></div></div>
      <div class="progress-meta">
        <span id="ap-elapsed">slot bekliyor</span>
        <span id="ap-slot-start">—</span>
      </div>
    </div>
    <div class="ap-attempts">
      <div style="font-size:10px;color:rgba(255,255,255,.5);text-transform:uppercase;letter-spacing:0.1em;margin-bottom:6px">Bu slotta denemeler</div>
      <div class="ap-attempt-list" id="ap-attempt-list"></div>
    </div>
  </div>

  <!-- RESOURCES (3 kolon, sparkline ile) -->
  <div class="grid g3">
    <div class="res-card">
      <div class="top"><span class="lbl">CPU Load (1dk)</span><span class="val" id="r-load">—</span></div>
      <div class="res-bar"><div class="res-fill" id="r-load-fill" style="width:0%"></div></div>
      <div class="res-foot" id="r-load-foot">— / —</div>
      <canvas class="spark-canvas" id="spark-cpu"></canvas>
    </div>
    <div class="res-card">
      <div class="top"><span class="lbl">RAM</span><span class="val" id="r-ram">— MB</span></div>
      <div class="res-bar"><div class="res-fill" id="r-ram-fill" style="width:0%"></div></div>
      <div class="res-foot" id="r-ram-foot">— / — MB</div>
      <canvas class="spark-canvas" id="spark-ram"></canvas>
    </div>
    <div class="res-card">
      <div class="top"><span class="lbl">Disk</span><span class="val" id="r-disk">— %</span></div>
      <div class="res-bar"><div class="res-fill" id="r-disk-fill" style="width:0%"></div></div>
      <div class="res-foot" id="r-disk-foot">— GB boş</div>
      <div class="res-foot" style="margin-top:8px"><span id="r-batch">—</span> batch, <span id="r-batch-mb">—</span> MB</div>
    </div>
  </div>

  <!-- BATCH HISTORY + UPLOADS -->
  <div class="grid g12">
    <div class="card">
      <h2>📦 Son Batch'ler</h2>
      <div class="batch-list" id="batch-list"></div>
    </div>
    <div class="card">
      <h2>📤 Son YouTube Uploadları</h2>
      <div style="max-height:280px;overflow-y:auto">
        <table class="uploads" id="upload-table">
          <thead><tr><th>Tarih</th><th>Saat</th><th>Başlık</th><th></th></tr></thead>
          <tbody></tbody>
        </table>
      </div>
    </div>
  </div>

  <!-- PLATE CHIPS + EVENTS + LOGS -->
  <div class="card">
    <h2>🚌 Ankara Plakaları — Son 24h (<span id="plate-count">0</span> farklı)</h2>
    <div class="plates" id="plates"></div>
  </div>

  <div class="grid g2">
    <div class="card">
      <h2>📜 Olay Zaman Çizelgesi</h2>
      <div class="events" id="events"></div>
    </div>
    <div class="card">
      <h2>📋 Canlı Loglar</h2>
      <div class="logs" id="logs"></div>
    </div>
  </div>

  <!-- STREAM MESAJ PANELI -->
  <div class="card" style="border-color:rgba(239,68,68,.3)">
    <h2>📢 Stream'e Anık Mesaj <span class="badge pill info"><span class="d"></span>canlı yayına yazı gönder</span></h2>
    <div style="display:flex;flex-direction:column;gap:10px;max-width:680px">
      <textarea id="sm-text" maxlength="200" rows="2" placeholder="Mesajını yaz (max 200 karakter)..."
        style="background:var(--card2);border:1px solid var(--line);color:var(--text);
               padding:10px 12px;border-radius:6px;font-family:inherit;font-size:13px;
               resize:vertical;line-height:1.4"></textarea>
      <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
        <span style="font-size:11px;color:var(--muted)">Süre:</span>
        <label class="sm-dur"><input type="radio" name="sm-dur" value="15"> 15s</label>
        <label class="sm-dur"><input type="radio" name="sm-dur" value="30" checked> 30s</label>
        <label class="sm-dur"><input type="radio" name="sm-dur" value="60"> 60s</label>
        <label class="sm-dur"><input type="radio" name="sm-dur" value="0"> Kalıcı</label>
        <span style="flex:1"></span>
        <span style="font-size:10px;color:var(--muted)" id="sm-char">0/200</span>
      </div>
      <div style="display:flex;gap:8px">
        <button class="ctrl-btn" id="sm-send" style="background:#dc2626;border-color:#7f1d1d;color:#fff;font-weight:700">
          📤 GÖNDER
        </button>
        <button class="ctrl-btn warn" id="sm-clear">🗑 Temizle (ekrandan kaldır)</button>
        <span style="flex:1"></span>
        <span id="sm-status" style="font-size:11px;color:var(--muted);align-self:center"></span>
      </div>
      <div style="font-size:10px;color:var(--muted);padding:8px 12px;background:rgba(99,102,241,.08);border-radius:6px">
        💡 İpucu: Mesaj YouTube + Kick yayınında ekranın alt-orta kısmında kırmızı banner ile 1-2 saniye gecikme ile görünür. Süre dolunca otomatik kaybolur.
      </div>
    </div>
  </div>

  <!-- KONTROL PANELI -->
  <div class="card">
    <h2>🎛️ Servis Kontrolleri</h2>
    <div class="controls">
      <button class="ctrl-btn" data-action="restart-live">🔄 Stream Yeniden Başlat</button>
      <button class="ctrl-btn" data-action="restart-harvester">🔄 Harvester Yeniden Başlat</button>
      <button class="ctrl-btn warn" data-action="restart-mediamtx">🔄 MediaMTX Yeniden Başlat</button>
      <button class="ctrl-btn warn" data-action="restart-all">🔄 Tümünü Yeniden Başlat</button>
      <button class="ctrl-btn danger" data-action="stop-live">⏹ Stream Durdur</button>
      <button class="ctrl-btn" data-action="start-live">▶ Stream Başlat</button>
    </div>
  </div>

</div>

<script>
const $ = id => document.getElementById(id);
const fmt = n => (n||0).toLocaleString('tr-TR');
function ageStr(s){if(s==null)return"—";if(s<60)return s+"s";if(s<3600)return Math.floor(s/60)+"dk";return Math.floor(s/3600)+"sa "+Math.floor((s%3600)/60)+"dk"}
function pillHTML(text, kind){return `<span class="pill ${kind}"><span class="d"></span>${text}</span>`}
function speedClass(s){if(!s)return"muted";if(s>=0.97&&s<=1.05)return"ok";if(s>=0.85)return"warn";return"bad"}

const CITY_FLAG = {ankara:"🌍",istanbul:"🌉",corum:"🏛️",konya:"🕌"};
const CITY_LABEL = {ankara:"Ankara",istanbul:"İstanbul",corum:"Çorum",konya:"Konya"};

let soundEnabled = localStorage.getItem('soundEnabled') !== 'false';
let lastAlarmTs = 0;
const alarmAudio = $('alarm-audio');

function updateSoundBtn(){
  const btn = $('fab-sound');
  btn.textContent = soundEnabled ? '🔔' : '🔕';
  btn.classList.toggle('muted', !soundEnabled);
  btn.title = soundEnabled ? 'Ses açık (m ile kapat)' : 'Ses kapalı (m ile aç)';
}
$('fab-sound').addEventListener('click', () => {
  soundEnabled = !soundEnabled;
  localStorage.setItem('soundEnabled', soundEnabled);
  updateSoundBtn();
});
updateSoundBtn();

function playAlarm(){
  if(!soundEnabled) return;
  // 5 saniyede bir alarm sesi (spam önleme)
  const now = Date.now();
  if(now - lastAlarmTs < 5000) return;
  lastAlarmTs = now;
  // Web Audio API ile programatik beep
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const o = ctx.createOscillator();
    const g = ctx.createGain();
    o.connect(g); g.connect(ctx.destination);
    o.type='square'; o.frequency.value=880;
    g.gain.setValueAtTime(.15, ctx.currentTime);
    g.gain.exponentialRampToValueAtTime(.001, ctx.currentTime+.3);
    o.start(); o.stop(ctx.currentTime+.3);
  } catch(e){}
}

// ─── SPARKLINE ─────────────────────────────────────────
function drawSpark(canvasId, values, max, color){
  const c = $(canvasId);
  if(!c) return;
  const dpr = window.devicePixelRatio || 1;
  const w = c.offsetWidth, h = c.offsetHeight;
  c.width = w * dpr; c.height = h * dpr;
  const ctx = c.getContext('2d');
  ctx.scale(dpr, dpr);
  ctx.clearRect(0,0,w,h);
  if(!values || values.length < 2) return;
  const m = max || Math.max(...values, 1);
  const dx = w / (values.length - 1);
  // Area
  ctx.beginPath();
  ctx.moveTo(0, h);
  values.forEach((v, i) => {
    const y = h - (v / m) * h * 0.9 - h * 0.05;
    ctx.lineTo(i * dx, y);
  });
  ctx.lineTo(w, h); ctx.closePath();
  const grad = ctx.createLinearGradient(0, 0, 0, h);
  grad.addColorStop(0, color + '60');
  grad.addColorStop(1, color + '00');
  ctx.fillStyle = grad; ctx.fill();
  // Line
  ctx.beginPath();
  values.forEach((v, i) => {
    const y = h - (v / m) * h * 0.9 - h * 0.05;
    if(i === 0) ctx.moveTo(0, y); else ctx.lineTo(i * dx, y);
  });
  ctx.strokeStyle = color; ctx.lineWidth = 1.5; ctx.stroke();
}

// ─── REFRESH ─────────────────────────────────────────
async function refresh(){
  let d;
  try { const r = await fetch('/api/status'); d = await r.json(); }
  catch(e) {
    $('status-dot').className = 'status-dot offline';
    $('header-meta').textContent = 'Bağlantı yok';
    return;
  }

  // HEADER
  const ok = d.ok;
  $('status-dot').className = 'status-dot ' + (ok ? 'live' : 'offline');
  $('header-meta').textContent = `${d.server_time||'—'}`;

  // ALARM
  const alarm = $('alarm-bar');
  if (d.broadcast_offline && d.broadcast_offline.length > 0) {
    alarm.classList.add('show');
    const list = d.broadcast_offline.map(o => `${o.name} (${ageStr(o.since_seconds)})`).join(' + ');
    $('alarm-title').textContent = `YAYIN OFFLINE: ${list}`;
    $('alarm-detail').textContent = 'MediaMTX tee bağlantısı koptu. systemctl restart mediamtx ile manuel müdahale veya 🔍 ile tanı.';
    playAlarm();
  } else {
    alarm.classList.remove('show');
  }

  // HERO NOW
  const phase = d.phase || 'bekliyor';
  const sb = d.streaming_batch || '';
  let cityFromBatch = '—';
  if (d.real_streaming && d.transcode_active && d.transcode_active.city) {
    cityFromBatch = d.transcode_active.city;
  } else if (sb) {
    cityFromBatch = 'Batch ' + sb.replace(/[^0-9]/g,'');
  }
  $('now-city').textContent = phase === 'yayında' ? (sb || '4 ŞEHİR') : phase.toUpperCase();
  const sc = speedClass(d.speed);
  $('now-pills').innerHTML = [
    pillHTML(phase, phase==='yayında'?'ok':phase==='filler'?'warn':'info'),
    pillHTML(`speed ${(d.speed||0).toFixed(2)}x`, sc),
    d.fps>15 ? pillHTML(`fps ${(d.fps||0).toFixed(0)}`, 'ok') : pillHTML(`fps ${(d.fps||0).toFixed(0)}`, 'warn'),
    d.real_streaming ? pillHTML('canlı içerik','ok') : pillHTML('filler','warn'),
  ].join('');
  $('now-speed').textContent = (d.speed||0).toFixed(2) + 'x';
  $('now-fps').textContent = (d.fps||0).toFixed(0);
  $('now-frame').textContent = 'frame ' + fmt(d.frame||0);
  $('now-phase').textContent = phase;
  $('now-action').textContent = (d.current_action || '—').substring(0, 40);
  $('now-bn').textContent = d.building_batch_id != null ? `#${d.building_batch_id}` : (sb||'—');
  $('now-bn-sub').textContent = d.batch_queued ? `${d.batch_queued} kuyrukta` : '—';

  // Playback progress
  const pb = d.playback || {};
  $('now-prog').style.width = (pb.pct||0) + '%';
  $('now-prog-played').textContent = pb.sec_played ? `${pb.sec_played}s oynandı` : 'henüz başlamadı';
  $('now-prog-total').textContent = pb.sec_total ? `${pb.sec_total}s toplam` : '—';

  // HERO SHORTS countdown
  const nss = d.next_shorts_seconds;
  if (nss != null) {
    const mm = Math.floor(nss/60), ss = nss%60;
    $('shorts-cd').textContent = `${String(mm).padStart(2,'0')}:${String(ss).padStart(2,'0')}`;
    $('shorts-cd').classList.toggle('imminent', nss < 120);
    $('shorts-time').textContent = `${d.next_shorts_time || '—'}'te tetiklenecek`;
  } else {
    $('shorts-cd').textContent = '—';
  }
  const ru = (d.recent_uploads||[])[0];
  if (ru) {
    $('shorts-last-title').textContent = ru.title;
    $('shorts-last-url').href = ru.url;
    $('shorts-last-url').textContent = `▶ izle (${ru.time}, ${ageStr(ru.age_sec)} önce)`;
  }

  // METRIC CARDS
  const yt = (d.tcp || {}).youtube || {};
  const kick = (d.tcp || {}).kick || {};
  $('mc-yt').classList.toggle('good', yt.active);
  $('mc-yt').classList.toggle('bad', !yt.active);
  $('yt-status').textContent = yt.active ? 'AKTİF' : 'YOK';
  $('yt-detail').textContent = yt.active ? `${yt.remote} • q=${fmt(yt.send_q||0)}` : 'TCP bağlantı yok';
  $('mc-kick').classList.toggle('good', kick.active);
  $('mc-kick').classList.toggle('bad', !kick.active);
  $('kick-status').textContent = kick.active ? 'AKTİF' : 'YOK';
  $('kick-detail').textContent = kick.active ? `${kick.remote} • q=${fmt(kick.send_q||0)}` : 'TCP bağlantı yok';
  const r = d.resources || {};
  $('ff-cpu').textContent = (r.ffmpeg_total_cpu||0).toFixed(0) + '%';
  $('ff-count').textContent = (r.ffmpeg_count||0) + ' süreç';
  const hCities = (d.harvester||{}).cities || {};
  const today = hCities.ankara ? hCities.ankara.success || 0 : 0;
  $('up-today').textContent = today;
  $('up-today-sub').textContent = `Ankara • toplam denemeler ${hCities.ankara ? hCities.ankara.attempts : 0}`;

  // CITIES
  const cp = d.city_progress || {};
  const cityOrder = ['Ankara','İstanbul','Çorum','Konya'];
  let chtml = '';
  for (const cn of cityOrder) {
    const ci = cp[cn] || {dur:0, target:240, status:'bekliyor', pct:0};
    let cls = 'gray';
    if (ci.status === 'tamamlandı' || ci.status === 'transcode ok') cls = 'green';
    else if (ci.status === 'indiriliyor') cls = 'blue';
    else if (ci.status === 'başarısız' || ci.status === 'transcode hata') cls = 'red';
    else cls = 'gray';
    const flag = CITY_FLAG[cn.toLowerCase().replace('i̇','i')] || '📍';
    chtml += `<div class="city-row">
      <div class="flag">${flag}</div>
      <div class="name">${cn}</div>
      <div class="bar"><div class="bar-fill ${cls}" style="width:${ci.pct||0}%"></div></div>
      <div class="stats">${(ci.dur||0).toFixed(0)}/${(ci.target||240).toFixed(0)}s</div>
      <div class="status">${pillHTML(ci.status||'—', cls==='green'?'ok':cls==='red'?'bad':cls==='blue'?'info':'muted')}</div>
    </div>`;
  }
  $('cities').innerHTML = chtml || '<div class=empty>henüz batch oluşturulmadı</div>';

  // ── ANKARA PIPELINE ──────────────────────────────────────
  const hp = d.harvester_pipeline || {};
  const phaseTag = $('ap-phase');
  let phaseClass = 'muted', phaseTxt = hp.phase || '—';
  if (hp.phase === 'running') { phaseClass = 'info'; phaseTxt = '▶ aktif'; }
  else if (hp.phase === 'finished' && hp.last_result === 'success') { phaseClass = 'ok'; phaseTxt = '✓ tamamlandı'; }
  else if (hp.phase === 'finished' && hp.last_result === 'fail') { phaseClass = 'bad'; phaseTxt = '✗ başarısız'; }
  else if (hp.phase === 'finished') { phaseClass = 'warn'; phaseTxt = 'bitti'; }
  else if (hp.phase === 'idle') { phaseClass = 'muted'; phaseTxt = 'bekliyor'; }
  phaseTag.className = 'badge pill ' + phaseClass;
  phaseTag.innerHTML = `<span class="d"></span>${phaseTxt}`;

  $('ap-action').textContent = hp.current_action || '—';
  $('ap-attempt').textContent = `${hp.current_attempt || 0}/${hp.total_candidates || 0}`;
  $('ap-plate').textContent = hp.current_plate || '—';
  $('ap-vtype').textContent = hp.current_vtype
    ? `${hp.current_vtype} ${hp.current_speed||0}km/h` : '—';
  $('ap-weather').textContent = hp.weather || '—';

  // Aşamalar (stages)
  const stageOrder = ['select', 'kayit', 'yolo', 'audio', 'upload', 'success'];
  const stageMap = {
    'init': 'select', 'selection': 'select', 'selected': 'select',
    'kayit': 'kayit', 'timeout': 'kayit',
    'yolo_fail': 'yolo', 'yolo_pass': 'yolo',
    'audio': 'audio', 'audio_done': 'audio',
    'upload': 'upload', 'yt_auth': 'upload', 'yt_uploaded': 'upload',
    'success': 'success', 'fail': null,
  };
  const stageNow = stageMap[hp.current_action_stage] || null;
  const stageIdx = stageOrder.indexOf(stageNow);
  const isFailStage = hp.current_action_stage === 'yolo_fail' ||
                      hp.current_action_stage === 'timeout' ||
                      hp.current_action_stage === 'fail';

  document.querySelectorAll('.ap-stage').forEach(el => {
    el.classList.remove('active', 'done', 'fail');
  });

  if (hp.current_action_stage === 'success' || hp.last_result === 'success') {
    // Tüm aşamalar done
    document.querySelectorAll('.ap-stage').forEach(el => el.classList.add('done'));
  } else if (isFailStage && stageIdx >= 0) {
    // Önceki aşamalar done, bu aşama fail
    stageOrder.forEach((s, i) => {
      const el = document.querySelector(`.ap-stage[data-stage="${s}"]`);
      if (!el) return;
      if (i < stageIdx) el.classList.add('done');
      else if (i === stageIdx) el.classList.add('fail');
    });
  } else if (stageIdx >= 0) {
    stageOrder.forEach((s, i) => {
      const el = document.querySelector(`.ap-stage[data-stage="${s}"]`);
      if (!el) return;
      if (i < stageIdx) el.classList.add('done');
      else if (i === stageIdx) el.classList.add('active');
    });
  }

  // Progress + elapsed
  const apProg = hp.total_candidates
    ? Math.min(100, Math.round((hp.current_attempt / hp.total_candidates) * 100))
    : 0;
  $('ap-prog-fill').style.width = apProg + '%';
  $('ap-elapsed').textContent = hp.elapsed_s
    ? `${ageStr(hp.elapsed_s)} geçti` : (hp.phase === 'idle' ? 'slot bekliyor' : '—');
  $('ap-slot-start').textContent = hp.slot_start
    ? `başladı: ${hp.slot_start}` : '—';

  // Attempts history
  let ah = '';
  for (const a of (hp.attempts_history || []).slice().reverse()) {
    let badge = 'info', resTxt = a.result || '—';
    if (a.result === 'success') { badge = 'ok'; resTxt = '✓ başarılı'; }
    else if (a.result === 'yolo_fail') { badge = 'warn'; resTxt = 'YOLO eledi'; }
    else if (a.result === 'yolo_pass') { badge = 'ok'; resTxt = 'YOLO ok'; }
    else if (a.result === 'timeout') { badge = 'bad'; resTxt = 'timeout'; }
    else if (a.result === 'audio_done' || a.result === 'audio') { badge = 'info'; resTxt = 'ses ok'; }
    else if (a.result === 'upload' || a.result === 'yt_uploaded') { badge = 'info'; resTxt = 'upload'; }
    ah += `<div class="ap-attempt-row">
      <span class="n">${a.n}/${hp.total_candidates||'?'}</span>
      <span class="p">${a.plate}</span>
      <span class="t">${a.vtype}</span>
      <span class="r ${badge}">${resTxt}</span>
    </div>`;
  }
  $('ap-attempt-list').innerHTML = ah
    || '<div class="empty">henüz deneme yok</div>';

  // RESOURCES
  $('r-load').textContent = (r.load1||0).toFixed(2);
  $('r-load-foot').textContent = `1m: ${(r.load1||0).toFixed(2)} / 5m: ${(r.load5||0).toFixed(2)}`;
  const loadPct = Math.min(100, (r.load1||0)/3*100);
  $('r-load-fill').style.width = loadPct + '%';
  $('r-load-fill').className = 'res-fill ' + (loadPct<60?'low':loadPct<85?'mid':'high');
  $('r-ram').textContent = fmt(r.mem_used_mb||0) + ' MB';
  $('r-ram-foot').textContent = `${fmt(r.mem_used_mb||0)} / ${fmt(r.mem_total_mb||0)} MB`;
  const ramPct = r.mem_total_mb ? Math.round((r.mem_used_mb/r.mem_total_mb)*100) : 0;
  $('r-ram-fill').style.width = ramPct + '%';
  $('r-ram-fill').className = 'res-fill ' + (ramPct<60?'low':ramPct<85?'mid':'high');
  $('r-disk').textContent = (r.disk_used_pct||0) + '%';
  $('r-disk-foot').textContent = `${(r.disk_free_gb||0).toFixed(1)} GB boş`;
  $('r-disk-fill').style.width = (r.disk_used_pct||0) + '%';
  $('r-disk-fill').className = 'res-fill ' + ((r.disk_used_pct||0)<60?'low':(r.disk_used_pct||0)<85?'mid':'high');
  $('r-batch').textContent = r.batch_count || 0;
  $('r-batch-mb').textContent = fmt(r.batch_total_mb || 0);

  // Sparklines
  if (d.cpu_history) drawSpark('spark-cpu', d.cpu_history.map(h => h.load), 3, '#22c55e');
  if (d.ram_history) drawSpark('spark-ram', d.ram_history.map(h => h.used), r.mem_total_mb || 4000, '#3b82f6');

  // BATCH HISTORY
  let bh = '';
  for (const b of (d.batch_history||[]).slice().reverse()) {
    bh += `<div class="batch-item">
      <div class="id">#${b.id}</div>
      <div class="name">${b.name}</div>
      <div class="meta">${b.size_mb}MB · ${b.build_seconds}s · ${b.ts}</div>
    </div>`;
  }
  $('batch-list').innerHTML = bh || '<div class=empty>henüz batch yok</div>';

  // UPLOADS TABLE
  const tbody = $('upload-table').querySelector('tbody');
  let uh = '';
  for (const u of (d.recent_uploads||[])) {
    uh += `<tr>
      <td>${u.date}</td>
      <td>${u.time}</td>
      <td class=title>${u.title}</td>
      <td><a class="watch" href="${u.url}" target=_blank>▶ izle</a></td>
    </tr>`;
  }
  tbody.innerHTML = uh || '<tr><td colspan=4 class=empty>henüz upload yok</td></tr>';

  // PLATES
  $('plate-count').textContent = d.ankara_plates_24h || 0;
  let ph = '';
  for (const p of (d.ankara_plates||[]).slice(0, 30)) {
    ph += `<span class="plate">${p.plate}<span class="pt">${p.time}</span></span>`;
  }
  $('plates').innerHTML = ph || '<div class=empty>henüz plaka kullanılmamış</div>';

  // EVENTS
  let eh = '';
  for (const e of (d.events||[]).slice().reverse().slice(0, 30)) {
    const sev = e.severity || 'info';
    eh += `<div class=ev>
      <div class=t>${e.ts}</div>
      <div class="tag ${sev}">${e.kind||''}</div>
      <div class=msg>${e.msg||''}</div>
    </div>`;
  }
  $('events').innerHTML = eh || '<div class=empty>henüz olay yok</div>';

  // LOGS
  let lh = '';
  for (const l of (d.logs||[]).slice().reverse().slice(0, 30)) {
    lh += `<div class=log-line>
      <div class=ts>${l.ts}</div>
      <div class="lvl ${l.level}">${l.level}</div>
      <div class=msg>${(l.msg||'').substring(0,160)}</div>
    </div>`;
  }
  $('logs').innerHTML = lh || '<div class=empty>henüz log yok</div>';
}

refresh();
setInterval(refresh, 2000);

// ─── Control buttons ─────────────────────────────────
document.querySelectorAll('.ctrl-btn[data-action]').forEach(btn => {
  btn.addEventListener('click', async () => {
    const action = btn.dataset.action;
    if (!confirm(`Onayla: ${action}`)) return;
    btn.disabled = true;
    btn.textContent = '⏳ ' + btn.textContent;
    try {
      const r = await fetch('/api/control', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({action})
      });
      const j = await r.json();
      alert(j.ok ? '✓ Başarılı' : ('✗ Hata: ' + (j.error||'unknown')));
    } catch (e) { alert('Hata: ' + e); }
    finally { setTimeout(() => location.reload(), 800); }
  });
});

// ─── Stream Mesaj Paneli ──────────────────────────────
const smText = $('sm-text');
const smChar = $('sm-char');
const smSend = $('sm-send');
const smClear = $('sm-clear');
const smStatus = $('sm-status');

if (smText) {
  smText.addEventListener('input', () => {
    smChar.textContent = `${smText.value.length}/200`;
  });
}

async function sendStreamMessage(message, duration) {
  smStatus.textContent = '⏳ gönderiliyor...';
  smStatus.style.color = 'var(--muted)';
  try {
    const r = await fetch('/api/stream-message', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({message, duration}),
    });
    const j = await r.json();
    if (j.ok) {
      if (message) {
        smStatus.innerHTML = `<span style="color:var(--green)">✓ Yayında: "${j.message.substring(0,40)}"${duration?` (${duration}s)`:' (kalıcı)'}</span>`;
      } else {
        smStatus.innerHTML = `<span style="color:var(--yellow)">🗑 Ekrandan kaldırıldı</span>`;
      }
    } else {
      smStatus.innerHTML = `<span style="color:var(--red)">✗ Hata: ${j.error||''}</span>`;
    }
  } catch (e) {
    smStatus.innerHTML = `<span style="color:var(--red)">✗ Bağlantı hatası</span>`;
  }
}

if (smSend) {
  smSend.addEventListener('click', () => {
    const msg = smText.value.trim();
    if (!msg) { alert('Mesaj boş!'); return; }
    const dur = parseInt(document.querySelector('input[name="sm-dur"]:checked').value);
    sendStreamMessage(msg, dur);
  });
}
if (smClear) {
  smClear.addEventListener('click', () => {
    smText.value = '';
    smChar.textContent = '0/200';
    sendStreamMessage('', 0);  // boş mesaj = temizle
  });
}

// ─── Modals ───────────────────────────────────────────
function bindModal(btnId, modalId, closeId) {
  const btn = $(btnId), modal = $(modalId), close = $(closeId);
  if (!btn || !modal) return;
  btn.addEventListener('click', () => modal.classList.add('open'));
  if (close) close.addEventListener('click', () => modal.classList.remove('open'));
  modal.addEventListener('click', e => { if (e.target === modal) modal.classList.remove('open'); });
}
bindModal('fab-help', 'modal-help', 'close-help');
bindModal('fab-diag', 'modal-diag', 'close-diag');

document.addEventListener('keydown', e => {
  if (['INPUT','TEXTAREA'].includes(document.activeElement.tagName)) return;
  if (e.key === 'Escape') document.querySelectorAll('.modal-bg.open').forEach(m => m.classList.remove('open'));
  else if (e.key === '?' || e.key === '/') { e.preventDefault(); $('modal-help').classList.toggle('open'); }
  else if (e.key === 'd' || e.key === 'D') { e.preventDefault(); runDiagnose(); }
  else if (e.key === 'm' || e.key === 'M') { e.preventDefault(); $('fab-sound').click(); }
});

async function runDiagnose() {
  const modal = $('modal-diag'), body = $('diag-body'), ts = $('diag-ts'), btn = $('fab-diag');
  modal.classList.add('open');
  btn.classList.add('loading');
  body.innerHTML = '<div style="text-align:center;padding:30px;color:var(--muted)">Tanı çalışıyor... 5-15s sürebilir</div>';
  ts.textContent = '—';
  try {
    const r = await fetch('/api/diagnose');
    const d = await r.json();
    ts.textContent = `Tamamlandı: ${d.ts} • ${d.issue_count} sorun`;
    if (d.issue_count === 0) {
      body.innerHTML = '<div class=diag-ok>✓ TÜM SİSTEM SAĞLIKLI<br><span style="font-size:11px;font-weight:400;color:var(--muted);margin-top:6px;display:block">10 bölüm kontrol edildi, hata yok</span></div>';
    } else {
      let html = '';
      for (const i of d.issues) {
        html += `<div class=diag-row><div class=diag-sec>${i.section}</div><div class=diag-msg>${i.issue}</div></div>`;
      }
      body.innerHTML = html;
    }
  } catch (e) {
    body.innerHTML = `<div class=diag-ok style="background:rgba(239,68,68,.1);color:var(--red)">Tanı başarısız: ${e}</div>`;
  } finally { btn.classList.remove('loading'); }
}
$('fab-diag').addEventListener('click', runDiagnose);
</script>
</body>
</html>"""

# ─── HTTP Server ─────────────────────────────────────────────────────────────

_state: Optional[StreamState] = None

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/api/status":
            body = json.dumps(_state.snapshot()).encode()
            self._respond(200, "application/json", body)
        elif self.path == "/api/diagnose":
            # diagnose.py'i JSON modunda subprocess olarak çalıştır
            try:
                import sys as _sys
                r = subprocess.run(
                    [_sys.executable, "/opt/KameraShorts/diagnose.py", "--json"],
                    capture_output=True, timeout=30,
                    cwd="/opt/KameraShorts",
                )
                out = r.stdout.decode("utf-8", errors="replace")
                try:
                    diag = json.loads(out.strip())
                except Exception:
                    diag = {"error": "diagnose parse failed",
                            "raw": out[-1000:]}
                issues = []
                for sect, info in diag.items():
                    if isinstance(info, dict):
                        for i in info.get("issues", []):
                            issues.append({"section": sect, "issue": i})
                payload = {
                    "ts": time.strftime("%H:%M:%S"),
                    "issue_count": len(issues),
                    "issues": issues,
                    "sections": diag,
                    "ok": r.returncode == 0,
                }
                self._respond(200, "application/json",
                              json.dumps(payload, ensure_ascii=False).encode("utf-8"))
            except Exception as e:
                self._respond(500, "application/json",
                              json.dumps({"error": str(e)}).encode())
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
        elif self.path == "/api/stream-message":
            # Anık mesaj: dashboard'dan yazı → stream FFmpeg drawtext'e
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            try:
                data = json.loads(body)
                msg = data.get("message", "")[:200]
                duration = int(data.get("duration", 30))
                msg_file = "/var/lib/kamerashorts/stream_message.txt"
                # FFmpeg drawtext özel karakter escape (single quote, colon, backslash)
                escaped = (msg.replace("\\", "\\\\")
                              .replace("'", "’")  # tek tirnak → düz tek tırnak
                              .replace(":", " "))      # : drawtext separator
                # Atomic write
                from pathlib import Path as _P
                _P(msg_file).parent.mkdir(parents=True, exist_ok=True)
                tmp = msg_file + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    f.write(escaped)
                import os as _os
                _os.replace(tmp, msg_file)
                # Süre bitince temizle (timer)
                if duration > 0 and msg:
                    def _clear():
                        try:
                            with open(msg_file, "w", encoding="utf-8") as f:
                                f.write("")
                        except Exception:
                            pass
                    threading.Timer(duration, _clear).start()
                resp = json.dumps({
                    "ok": True, "message": msg,
                    "duration": duration if msg else 0,
                }).encode()
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
            try:
                _state.sample_tcp()
            except Exception as e:
                print(f"[poll] TCP hata: {e}")
            try:
                _state.sample_uploads()
            except Exception as e:
                print(f"[poll] uploads hata: {e}")
            try:
                _state.sample_plates()
            except Exception as e:
                print(f"[poll] plates hata: {e}")
            try:
                _state.sample_next_shorts()
            except Exception as e:
                print(f"[poll] next_shorts hata: {e}")
            try:
                _state.sample_cpu_ram_history()
            except Exception as e:
                print(f"[poll] history hata: {e}")
            try:
                _state.sample_harvester_pipeline()
            except Exception as e:
                print(f"[poll] harv_pipe hata: {e}")
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
