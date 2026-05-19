#!/usr/bin/env python3
"""KameraShorts v5 — Dashboard (zengin).

Veri kaynagi: SQLite (mixer_state, events, segments, upload_log,
service_health, used_plates) + canlı sistem ölçümleri (/proc, ss).

URL'ler:
  /         HTML SPA
  /api      tum snapshot JSON
"""
import argparse
import json
import os
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from v5 import common, db

log = common.setup_logging("dashboard")


# ─── Sistem ölçümleri ────────────────────────────────────────────────────────

def _sys_resources() -> dict:
    out = {}
    try:
        with open("/proc/loadavg") as f:
            parts = f.read().split()
        out["load1"] = float(parts[0])
        out["load5"] = float(parts[1])
        out["load15"] = float(parts[2])
    except Exception:
        out["load1"] = out["load5"] = out["load15"] = 0.0
    try:
        mem = {}
        with open("/proc/meminfo") as f:
            for ln in f:
                k, _, v = ln.partition(":")
                try:
                    mem[k] = int(v.strip().split()[0])
                except Exception:
                    pass
        out["mem_total_mb"] = mem.get("MemTotal", 0) // 1024
        out["mem_avail_mb"] = mem.get("MemAvailable", 0) // 1024
        out["mem_used_mb"] = out["mem_total_mb"] - out["mem_avail_mb"]
        out["swap_used_mb"] = (mem.get("SwapTotal", 0) - mem.get("SwapFree", 0)) // 1024
        out["swap_total_mb"] = mem.get("SwapTotal", 0) // 1024
    except Exception:
        out["mem_total_mb"] = out["mem_used_mb"] = out["mem_avail_mb"] = 0
        out["swap_used_mb"] = out["swap_total_mb"] = 0
    try:
        r = subprocess.run(["pgrep", "-c", "ffmpeg"],
                           capture_output=True, text=True, timeout=2)
        out["ffmpeg_count"] = int(r.stdout.strip() or "0")
    except Exception:
        out["ffmpeg_count"] = 0
    try:
        r = subprocess.run(
            ["ps", "-eo", "comm,pcpu", "--no-headers"],
            capture_output=True, text=True, timeout=2,
        )
        ffmpeg_cpu = 0.0
        for ln in r.stdout.splitlines():
            p = ln.split()
            if len(p) >= 2 and p[0] == "ffmpeg":
                try:
                    ffmpeg_cpu += float(p[1])
                except Exception:
                    pass
        out["ffmpeg_total_cpu"] = round(ffmpeg_cpu, 1)
    except Exception:
        out["ffmpeg_total_cpu"] = 0.0
    try:
        import shutil as _sh
        t = _sh.disk_usage("/")
        out["disk_used_pct"] = round(t.used / t.total * 100)
        out["disk_free_gb"] = round(t.free / 1e9, 1)
    except Exception:
        out["disk_used_pct"] = 0
        out["disk_free_gb"] = 0.0
    return out


def _tcp_status() -> dict:
    """YouTube + Kick aktif RTMP bağlantı sağlığı."""
    out = {"youtube": {"active": False, "send_q": 0, "remote": ""},
           "kick": {"active": False, "send_q": 0, "remote": ""}}
    try:
        r = subprocess.run(
            ["ss", "-tnp"], capture_output=True, text=True, timeout=3,
        )
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
            # YouTube RTMP = 1935, genelde 172.217.x veya 142.250.x
            if remote.endswith(":1935") and "127.0.0.1" not in remote:
                out["youtube"] = {"active": True, "send_q": send_q,
                                  "remote": remote}
            # Kick RTMPS = 443, IP 35.55.x veya benzer
            elif (":443" in remote and (remote.startswith("35.")
                  or "live-video" in remote)):
                out["kick"] = {"active": True, "send_q": send_q,
                               "remote": remote}
    except Exception:
        pass
    return out


def _stream_ffmpeg_info() -> dict:
    """Stream encode eden FFmpeg'in PID, etime, CPU."""
    out = {"pid": None, "uptime_sec": 0, "cpu_pct": 0.0, "rss_mb": 0}
    try:
        r = subprocess.run(
            ["pgrep", "-f", "v5.mixer|live_streamer"],
            capture_output=True, text=True, timeout=2,
        )
        for pid_str in r.stdout.strip().splitlines():
            pid_str = pid_str.strip()
            if not pid_str:
                continue
            try:
                pid = int(pid_str)
            except Exception:
                continue
            try:
                proc_stat = subprocess.run(
                    ["ps", "-p", str(pid), "-o", "etimes=,pcpu=,rss="],
                    capture_output=True, text=True, timeout=2,
                )
                parts = proc_stat.stdout.strip().split()
                if len(parts) >= 3:
                    out["pid"] = pid
                    out["uptime_sec"] = int(parts[0])
                    out["cpu_pct"] = float(parts[1])
                    out["rss_mb"] = int(parts[2]) // 1024
                    break
            except Exception:
                pass
    except Exception:
        pass
    return out


def _next_shorts_timer() -> dict:
    """systemctl list-timers ile sonraki Ankara Shorts tetik zamanı.

    systemctl show çıktısında NextElapseUSecRealtime aslında insan-okunabilir
    tarih string'i (ör "Tue 2026-05-19 13:15:00 +03"). Datetime ile parse.
    """
    out = {"next_run": None, "next_in_seconds": None,
           "last_run": None, "last_result": None}
    try:
        r = subprocess.run(
            ["systemctl", "show", "kshorts-shorts@ankara.timer",
             "--property=NextElapseUSecRealtime",
             "--property=LastTriggerUSec",
             "--property=Result", "--property=ActiveState"],
            capture_output=True, text=True, timeout=3,
        )
        kv = {}
        for ln in r.stdout.splitlines():
            if "=" in ln:
                k, _, v = ln.partition("=")
                kv[k] = v.strip()
        out["last_result"] = kv.get("Result", "")

        # NEXT: "Tue 2026-05-19 13:15:00 +03"
        from datetime import datetime
        nxt = kv.get("NextElapseUSecRealtime", "")
        if nxt and nxt != "n/a":
            try:
                # Strip weekday + timezone, parse
                parts = nxt.split()
                if len(parts) >= 4:
                    # "2026-05-19 13:15:00"
                    dt_str = parts[1] + " " + parts[2]
                    dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
                    ts = dt.timestamp()
                    out["next_in_seconds"] = max(0, int(ts - time.time()))
                    out["next_run"] = dt.strftime("%H:%M")
            except Exception:
                pass

        last = kv.get("LastTriggerUSec", "")
        if last and last != "n/a" and last:
            try:
                parts = last.split()
                if len(parts) >= 3:
                    dt_str = parts[1] + " " + parts[2]
                    dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
                    out["last_run"] = dt.strftime("%H:%M")
            except Exception:
                pass
    except Exception:
        pass
    return out


def _service_uptime(unit: str) -> int:
    """systemctl bir unit'in uptime'ını saniye olarak verir."""
    try:
        r = subprocess.run(
            ["systemctl", "show", unit,
             "--property=ActiveEnterTimestampMonotonic"],
            capture_output=True, text=True, timeout=2,
        )
        if "=" in r.stdout:
            val = int(r.stdout.split("=", 1)[1].strip() or 0)
            if val > 0:
                with open("/proc/uptime") as f:
                    sys_up = float(f.read().split()[0])
                return max(0, int(sys_up - val / 1_000_000))
    except Exception:
        pass
    return 0


# ─── Snapshot ─────────────────────────────────────────────────────────────────

CITY_LABEL = {"ankara": "Ankara", "istanbul": "İstanbul",
              "corum": "Çorum", "konya": "Konya"}

# Yayın offline tespiti — son N saniye boyunca YT veya Kick'ten biri yok mu?
_offline_start: dict = {"youtube": None, "kick": None}
_alerted: dict = {"youtube": False, "kick": False}


def _check_tcp_health(tcp: dict):
    """TCP durumu degisirse event'e yaz (her dashboard poll'unde)."""
    now = int(time.time())
    for k in ("youtube", "kick"):
        active = tcp.get(k, {}).get("active", False)
        if not active:
            if _offline_start[k] is None:
                _offline_start[k] = now
            # 10 saniye boyunca offline ise alarm
            elif now - _offline_start[k] >= 10 and not _alerted[k]:
                try:
                    db.add_event(
                        "stream", "broadcast_down",
                        "{} {}s'dir baglanti yok".format(
                            k.upper(), now - _offline_start[k]),
                        "error",
                    )
                except Exception:
                    pass
                _alerted[k] = True
        else:
            # Yine aktif — alarm vardiysa recovered event yaz
            if _alerted[k]:
                try:
                    duration = now - (_offline_start[k] or now)
                    db.add_event(
                        "stream", "broadcast_up",
                        "{} baglanti geri geldi ({}s sonra)".format(
                            k.upper(), duration),
                        "good",
                    )
                except Exception:
                    pass
            _offline_start[k] = None
            _alerted[k] = False


def snapshot() -> dict:
    c = db.conn()

    # Servis sağlığı
    services = {}
    for r in c.execute(
        "SELECT service_name, last_heartbeat, status FROM service_health"
    ):
        age = int(time.time()) - r[1]
        services[r[0]] = {
            "status": r[2],
            "last_seen": age,
            "alive": age < 30,
        }

    # Şehir buffer durumu
    cities = {}
    for city in ("ankara", "istanbul", "corum", "konya"):
        row = c.execute(
            """SELECT COUNT(*), MAX(start_ts), AVG(brightness), AVG(motion),
                      SUM(size_bytes)
               FROM segments WHERE city=? AND expires_at>?""",
            (city, int(time.time())),
        ).fetchone()
        n, last_ts, bright, motion, total_b = row if row else (0, 0, 0, 0, 0)
        cities[city] = {
            "label": CITY_LABEL[city],
            "segment_count": n or 0,
            "last_segment_age": (int(time.time()) - last_ts) if last_ts else None,
            "avg_brightness": round(bright or 0, 1),
            "avg_motion": round(motion or 0, 1),
            "total_mb": round((total_b or 0) / 1e6, 1),
        }

    # Mixer state
    mixer = db.get_mixer_state() or {}
    if mixer:
        bs = mixer.get("block_started", 0) or 0
        bd = mixer.get("block_duration", 0) or 0
        elapsed = max(0, int(time.time()) - bs)
        mixer["block_elapsed"] = min(elapsed, bd)
        mixer["block_pct"] = round(min(elapsed / bd, 1.0) * 100) if bd else 0
        mixer["last_age"] = int(time.time()) - (mixer.get("last_update", 0) or 0)
        mixer["active_label"] = CITY_LABEL.get(
            mixer.get("active_city", ""), mixer.get("active_city", "?"))

    # Bugünkü upload
    uploads = {c: db.uploads_today(c) for c in
               ("ankara", "istanbul", "corum", "konya")}

    # Son uploads
    recent = [
        {"video_id": r[0], "city": r[1], "title": r[2],
         "url": r[3], "uploaded_at": r[4],
         "age_min": (int(time.time()) - r[4]) // 60}
        for r in c.execute(
            """SELECT video_id, city, title, youtube_url, uploaded_at
               FROM upload_log ORDER BY uploaded_at DESC LIMIT 15"""
        )
    ]

    # Eventler
    events = []
    for ev in db.recent_events(limit=30):
        events.append({
            "ts": ev["ts"],
            "time": time.strftime("%H:%M:%S", time.localtime(ev["ts"])),
            "service": ev["service"],
            "kind": ev["kind"],
            "severity": ev["severity"],
            "message": ev["message"],
            "age_sec": int(time.time()) - ev["ts"],
        })

    # Ankara plaka dedup
    plates_24h = len(db.recent_plates(hours=24))
    plates_recent = []
    for r in c.execute(
        """SELECT plate, used_at, youtube_url FROM used_plates
           ORDER BY used_at DESC LIMIT 10"""
    ):
        plates_recent.append({
            "plate": r[0],
            "time": time.strftime("%H:%M", time.localtime(r[1])),
            "url": r[2],
        })

    # TCP sağlığı + offline detection (event'e otomatik yazar)
    tcp = _tcp_status()
    _check_tcp_health(tcp)

    # Yayın offline alarm bar için
    broadcast_offline = []
    now_ts = int(time.time())
    for k in ("youtube", "kick"):
        if not tcp.get(k, {}).get("active"):
            since = _offline_start.get(k)
            secs = (now_ts - since) if since else 0
            broadcast_offline.append({"name": k.upper(), "since_seconds": secs})

    return {
        "server_time": time.strftime("%H:%M:%S"),
        "uptime_sec": _service_uptime("kshorts-mixer.service"),
        "broadcast_offline": broadcast_offline,
        "services": services,
        "cities": cities,
        "mixer": mixer,
        "tcp": tcp,
        "stream_ffmpeg": _stream_ffmpeg_info(),
        "next_shorts": _next_shorts_timer(),
        "uploads_today": uploads,
        "recent_uploads": recent,
        "events": events,
        "ankara_plates_24h": plates_24h,
        "plates_recent": plates_recent,
        "resources": _sys_resources(),
    }


# ─── HTML ─────────────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="tr"><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>KameraShorts v5 Dashboard</title>
<style>
:root{
  --bg:#0a0f1c; --card:#111827; --card2:#1e293b; --line:#1e293b;
  --green:#22c55e; --yellow:#f59e0b; --red:#ef4444; --blue:#3b82f6;
  --text:#e2e8f0; --muted:#64748b; --accent:#818cf8;
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:linear-gradient(180deg,#0a0f1c,#070b15);color:var(--text);
     font-family:ui-monospace,'SF Mono',Menlo,monospace;font-size:13px;min-height:100vh}
a{color:#93c5fd;text-decoration:none}
a:hover{text-decoration:underline}

.wrap{max-width:1280px;margin:0 auto;padding:14px}
.header{display:flex;align-items:baseline;gap:14px;margin-bottom:14px;
        padding-bottom:12px;border-bottom:1px solid var(--line)}
.header h1{font-size:16px;font-weight:700;color:#fff;letter-spacing:0.04em}
.header .v{color:var(--accent);font-weight:600}
.header .right{margin-left:auto;color:var(--muted);font-size:11px}

.grid{display:grid;gap:10px;margin-bottom:10px}
.g4{grid-template-columns:repeat(4,1fr)}
.g3{grid-template-columns:repeat(3,1fr)}
.g2{grid-template-columns:1fr 1fr}
.g21{grid-template-columns:2fr 1fr}
@media(max-width:980px){.g4{grid-template-columns:repeat(2,1fr)}
  .g3,.g2,.g21{grid-template-columns:1fr}}

.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px}
.card h2{font-size:10px;font-weight:700;letter-spacing:0.12em;color:var(--muted);
         text-transform:uppercase;margin-bottom:10px}

.pill{display:inline-flex;align-items:center;gap:6px;padding:3px 10px;
      border-radius:99px;font-size:11px;font-weight:600}
.pill .pd{width:8px;height:8px;border-radius:50%;flex-shrink:0}
.pill.ok{background:rgba(34,197,94,.12);color:var(--green)}
.pill.ok .pd{background:var(--green);box-shadow:0 0 6px var(--green)}
.pill.warn{background:rgba(245,158,11,.12);color:var(--yellow)}
.pill.warn .pd{background:var(--yellow)}
.pill.bad{background:rgba(239,68,68,.12);color:var(--red)}
.pill.bad .pd{background:var(--red);box-shadow:0 0 6px var(--red)}
.pill.gray{background:rgba(100,116,139,.12);color:var(--muted)}
.pill.gray .pd{background:var(--muted)}

/* HERO — şu an yayında olan */
.hero{background:linear-gradient(120deg,#1e1b4b 0%,#0a0f1c 100%);
      border:1px solid #312e81;border-radius:14px;padding:20px;
      display:flex;flex-direction:column;gap:14px;margin-bottom:12px;
      position:relative;overflow:hidden}
.hero::before{content:"";position:absolute;top:-30%;right:-10%;width:300px;
              height:300px;border-radius:50%;
              background:radial-gradient(circle,rgba(99,102,241,.18),transparent 70%)}
.hero-row1{display:flex;align-items:center;gap:14px;flex-wrap:wrap}
.hero-city{font-size:30px;font-weight:800;color:#fff;letter-spacing:0.04em}
.hero-pill-group{display:flex;gap:6px;flex-wrap:wrap}
.hero-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;
           position:relative;z-index:1}
@media(max-width:700px){.hero-grid{grid-template-columns:repeat(2,1fr)}}
.hero-stat{padding:8px 0}
.hero-stat .lbl{font-size:9px;color:rgba(255,255,255,.55);
                text-transform:uppercase;letter-spacing:0.1em}
.hero-stat .val{font-size:22px;font-weight:700;color:#fff;margin-top:2px;line-height:1}
.hero-stat .sub{font-size:10px;color:rgba(255,255,255,.4);margin-top:2px}

.progress{height:6px;background:rgba(255,255,255,.08);border-radius:3px;
          overflow:hidden;margin-top:8px}
.progress-fill{height:100%;background:linear-gradient(90deg,#3b82f6,#22c55e);
               border-radius:3px;transition:width 1s linear}

.gauge{font-size:34px;font-weight:900;line-height:1}
.gauge.good{color:var(--green)}
.gauge.warn{color:var(--yellow)}
.gauge.bad{color:var(--red)}
.gauge.idle{color:var(--muted)}

/* Quick stats grid */
.qstat{display:flex;align-items:center;gap:10px;padding:8px 12px;
       background:var(--card2);border-radius:7px}
.qstat .icon{font-size:18px;width:24px;text-align:center}
.qstat .body{flex:1;min-width:0}
.qstat .label{font-size:9px;color:var(--muted);text-transform:uppercase;letter-spacing:0.08em}
.qstat .value{font-size:14px;color:#fff;font-weight:600;
              white-space:nowrap;overflow:hidden;text-overflow:ellipsis}

/* Service health rows */
.srow{display:flex;align-items:center;gap:8px;padding:6px 10px;
      background:var(--card2);border-radius:6px;margin-bottom:4px}
.srow .name{flex:1;font-size:12px;color:#cbd5e1;
            white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.srow .age{color:var(--muted);font-size:10px;width:50px;text-align:right}

/* City buffer rows */
.city-row{display:flex;align-items:center;gap:10px;padding:8px 10px;
          background:var(--card2);border-radius:6px;margin-bottom:5px}
.city-row .flag{font-size:18px;width:24px;text-align:center}
.city-row .name{width:100px;font-size:12px;font-weight:600;color:#e2e8f0}
.city-row .bar-wrap{flex:1;height:4px;background:rgba(255,255,255,.06);
                    border-radius:2px;overflow:hidden}
.city-row .bar{height:100%;background:var(--blue);border-radius:2px;transition:width .5s}
.city-row .stats{font-size:10px;color:var(--muted);width:160px;text-align:right;
                 font-family:monospace}
.city-row .age-pill{flex-shrink:0;font-size:10px;width:48px;text-align:center;
                    padding:2px 6px;border-radius:3px;font-weight:700}
.city-row .age-pill.ok{background:rgba(34,197,94,.15);color:var(--green)}
.city-row .age-pill.warn{background:rgba(245,158,11,.15);color:var(--yellow)}
.city-row .age-pill.bad{background:rgba(239,68,68,.15);color:var(--red)}

/* Events timeline */
.ev-list{max-height:340px;overflow-y:auto}
.ev-list::-webkit-scrollbar{width:6px}
.ev-list::-webkit-scrollbar-thumb{background:#334155;border-radius:3px}
.ev{display:flex;gap:10px;padding:6px 10px;border-radius:6px;
    margin-bottom:3px;align-items:center;background:var(--card2)}
.ev-time{color:#475569;font-size:10px;width:62px;flex-shrink:0;font-family:monospace}
.ev-tag{flex-shrink:0;padding:1px 6px;border-radius:3px;font-size:9px;
        font-weight:700;letter-spacing:0.04em;width:68px;text-align:center}
.ev-tag.info{background:rgba(99,102,241,.15);color:#a5b4fc}
.ev-tag.warn{background:rgba(245,158,11,.15);color:var(--yellow)}
.ev-tag.error{background:rgba(239,68,68,.15);color:var(--red)}
.ev-tag.good{background:rgba(34,197,94,.15);color:var(--green)}
.ev-svc{font-size:10px;color:var(--muted);width:60px;flex-shrink:0}
.ev-msg{flex:1;color:#cbd5e1;font-size:11px;
        white-space:nowrap;overflow:hidden;text-overflow:ellipsis}

/* Uploads table */
.uptable{width:100%;border-collapse:collapse;font-size:11px}
.uptable th{text-align:left;color:var(--muted);font-size:9px;
            text-transform:uppercase;letter-spacing:0.08em;
            padding:6px 8px;border-bottom:1px solid var(--line);font-weight:700}
.uptable td{padding:7px 8px;border-bottom:1px solid var(--line);color:#cbd5e1}
.uptable tr:hover{background:rgba(255,255,255,.02)}

/* Resource bars */
.res{margin-bottom:8px}
.res-h{display:flex;justify-content:space-between;font-size:11px;margin-bottom:3px}
.res-h .lbl{color:var(--muted)}
.res-h .val{color:#cbd5e1;font-family:monospace}
.res-bar{height:5px;background:rgba(255,255,255,.05);border-radius:3px;overflow:hidden}
.res-fill{height:100%;border-radius:3px;transition:width .5s}
.res-fill.low{background:var(--green)}
.res-fill.mid{background:var(--yellow)}
.res-fill.high{background:var(--red)}

/* Plate chips */
.plate-row{display:flex;flex-wrap:wrap;gap:6px;margin-top:6px}
.plate{padding:3px 8px;background:rgba(99,102,241,.12);border:1px solid rgba(99,102,241,.25);
       border-radius:4px;font-size:10px;color:#a5b4fc;font-family:monospace}

/* Countdown */
.countdown{font-size:36px;font-weight:900;color:#fff;line-height:1;text-align:center}
.countdown.imminent{color:var(--yellow);animation:pulse 1.5s infinite}
@keyframes pulse{50%{opacity:.5}}

.empty{text-align:center;color:var(--muted);padding:20px;font-size:11px;font-style:italic}

#status-indicator{display:inline-block;width:8px;height:8px;border-radius:50%;
                  background:var(--muted);margin-right:6px;vertical-align:middle}
#status-indicator.live{background:var(--green);box-shadow:0 0 8px var(--green);
                       animation:pulse 2s infinite}

/* ─── Diagnose button ────────────────────────────── */
.diag-btn{position:fixed;bottom:80px;right:20px;width:48px;height:48px;
          border-radius:50%;background:#fbbf24;color:#7c2d12;border:none;
          font-size:22px;cursor:pointer;box-shadow:0 4px 12px rgba(251,191,36,.4);
          z-index:99;transition:transform .2s}
.diag-btn:hover{transform:scale(1.1)}
.diag-btn.loading{animation:diag-spin 1s linear infinite}
@keyframes diag-spin{to{transform:rotate(360deg)}}

#diag-modal .issue-row{padding:8px 12px;background:var(--card2);border-radius:6px;
                       margin-bottom:5px;display:flex;gap:10px;align-items:flex-start}
#diag-modal .issue-section{flex-shrink:0;padding:2px 8px;background:rgba(239,68,68,.2);
                            color:var(--red);border-radius:3px;font-size:10px;font-weight:700;
                            text-transform:uppercase;letter-spacing:0.05em}
#diag-modal .issue-text{flex:1;color:#cbd5e1;font-size:11.5px;line-height:1.4}
#diag-modal .all-ok{padding:30px;text-align:center;background:rgba(34,197,94,.1);
                    border-radius:8px;color:var(--green);font-size:14px;font-weight:600}

/* ─── Yayın offline alarm bar ────────────────────────────── */
.alarm-bar{display:none;background:linear-gradient(90deg,#7f1d1d,#450a0a);
           border:1px solid var(--red);border-radius:10px;padding:14px 18px;
           margin-bottom:12px;align-items:center;gap:14px;
           animation:alarm-pulse 1.5s infinite}
.alarm-bar.show{display:flex}
.alarm-icon{font-size:28px}
.alarm-content{flex:1}
.alarm-title{font-size:14px;font-weight:800;color:#fff;letter-spacing:0.04em}
.alarm-detail{font-size:11px;color:#fca5a5;margin-top:2px}
@keyframes alarm-pulse{
  0%,100%{box-shadow:0 0 0 0 rgba(239,68,68,.5)}
  50%{box-shadow:0 0 0 12px rgba(239,68,68,0)}
}

/* ─── Help button & modal ───────────────────────────────── */
.help-btn{position:fixed;bottom:20px;right:20px;width:48px;height:48px;
          border-radius:50%;background:var(--accent);color:#fff;border:none;
          font-size:24px;font-weight:700;cursor:pointer;box-shadow:0 4px 12px rgba(99,102,241,.4);
          z-index:99;transition:transform .2s}
.help-btn:hover{transform:scale(1.1)}
.help-btn::after{content:"";position:absolute;top:50%;left:50%;
                 transform:translate(-50%,-50%);}

.modal-backdrop{display:none;position:fixed;inset:0;background:rgba(0,0,0,.7);
                z-index:100;align-items:flex-start;justify-content:center;
                padding:30px 20px;overflow-y:auto}
.modal-backdrop.open{display:flex}
.modal{background:var(--card);border:1px solid var(--line);border-radius:14px;
       max-width:880px;width:100%;padding:24px;position:relative;
       box-shadow:0 20px 60px rgba(0,0,0,.5)}
.modal h2{font-size:20px;color:#fff;margin-bottom:6px;letter-spacing:0.02em;text-transform:none}
.modal h3{font-size:14px;color:var(--accent);margin-top:18px;margin-bottom:8px;
          letter-spacing:0.04em;text-transform:uppercase;font-weight:700}
.modal h3:first-of-type{margin-top:0}
.modal p,.modal li{font-size:12.5px;color:#cbd5e1;line-height:1.65;margin-bottom:6px}
.modal ul{padding-left:18px;margin-bottom:10px}
.modal code{background:var(--card2);padding:1px 6px;border-radius:3px;
            font-family:ui-monospace,monospace;font-size:11px;color:#fbbf24}
.modal .close-btn{position:absolute;top:14px;right:14px;background:none;border:none;
                  color:var(--muted);font-size:24px;cursor:pointer;
                  width:32px;height:32px;display:flex;align-items:center;justify-content:center;
                  border-radius:50%;transition:background .15s}
.modal .close-btn:hover{background:var(--card2);color:#fff}
.modal .arch-diagram{background:#060d1a;border:1px solid var(--line);border-radius:8px;
                     padding:14px;margin:10px 0;font-family:ui-monospace,monospace;
                     font-size:11px;line-height:1.4;color:#94a3b8;
                     white-space:pre;overflow-x:auto}
.modal table{width:100%;border-collapse:collapse;font-size:11.5px;margin:8px 0}
.modal table th{text-align:left;padding:6px 10px;background:var(--card2);
                color:var(--muted);font-weight:700;text-transform:uppercase;
                font-size:9px;letter-spacing:0.08em;border-bottom:1px solid var(--line)}
.modal table td{padding:6px 10px;border-bottom:1px solid var(--line);color:#cbd5e1}
.modal kbd{background:var(--card2);border:1px solid var(--line);border-radius:3px;
           padding:1px 6px;font-size:10px;color:#a5b4fc;font-family:monospace}
</style>
</head><body>

<!-- Help button (sağ alt) -->
<button class="help-btn" id="help-btn" title="Sistemi tanı (? — yardım)">?</button>
<button class="diag-btn" id="diag-btn" title="Tanı çalıştır — sistemi tara">🔍</button>

<!-- Diagnose result modal -->
<div class="modal-backdrop" id="diag-modal">
  <div class="modal">
    <button class="close-btn" id="diag-close">×</button>
    <h2>🔍 Sistem Tanı Raporu</h2>
    <p style="color:var(--muted);font-size:11px" id="diag-ts">—</p>
    <div id="diag-body" style="margin-top:12px">Tanı çalıştırılıyor...</div>
  </div>
</div>

<!-- Help modal -->
<div class="modal-backdrop" id="help-modal">
  <div class="modal">
    <button class="close-btn" id="help-close">×</button>
    <h2>KameraShorts v5 — Sistem Rehberi</h2>
    <p style="color:var(--muted);font-size:11px">Tıkla kapat: <kbd>×</kbd> veya <kbd>Esc</kbd></p>

    <h3>📐 Genel Mimari</h3>
    <p>v5 sistemi <b>7 küçük process</b>'ten oluşur. Her servis bağımsız çalışır; biri çökerse diğerleri etkilenmez. SQLite paylaşımlı state.</p>
    <div class="arch-diagram">İNTERNET
  ↓ HLS
[Ankara EGO]  [İBB Turistik]  [Çorum Bld.]  [Konya OVH]
  ↓             ↓                ↓             ↓
[ingest-ank] [ingest-ist]   [ingest-cor]  [ingest-kny]
       └─────────┬─────────────────┘
                 ▼
   /var/lib/kamerashorts/  +  media.db (SQLite-WAL)
        ↑                     ↑
        │ oku                 │ oku
  ┌─────┴────┐          ┌────┴─────┐
  │ mixer    │          │ shorts   │ (saatlik :15)
  │ tek FF.  │          │ YOLO →   │
  │ 4 şehir  │          │ encode → │
  │ overlay  │          │ upload   │
  │ + müzik  │          └──────────┘
  └────┬─────┘
       ▼ RTMP
   MediaMTX :1935
   tee onfail=ignore
    ├→ YouTube
    └→ Kick</div>

    <h3>🎛️ Bileşenler</h3>
    <table>
      <tr><th>Servis</th><th>Görev</th><th>RAM</th></tr>
      <tr><td><code>kshorts-ingest@&lt;city&gt;</code></td><td>HLS segmentlerini diske kopyalar, transcode YOK</td><td>~30 MB</td></tr>
      <tr><td><code>kshorts-mixer</code></td><td>4 şehir tek FFmpeg + drawtext + müzik → MediaMTX</td><td>~150 MB</td></tr>
      <tr><td><code>kshorts-shorts@ankara</code></td><td>Saat :15 → DB'den iyi segment seç → YOLO → upload</td><td>~400 MB peak</td></tr>
      <tr><td><code>kshorts-cleaner</code></td><td>TTL geçen kayıt + orphan dosya temizliği (5dk)</td><td>~15 MB</td></tr>
      <tr><td><code>kshorts-dashboard</code></td><td>Bu sayfa — DB read-only + sistem ölçümleri</td><td>~50 MB</td></tr>
      <tr><td><code>mediamtx</code></td><td>RTMP relay, runOnReady ile tee → YouTube + Kick</td><td>~30 MB</td></tr>
    </table>

    <h3>📊 Dashboard Kartları</h3>
    <ul>
      <li><b>HERO kart (mor)</b> — Şu an yayında olan şehir. Speed 1.00x ideal; 0.85x altında uyarı.</li>
      <li><b>YouTube → Kick</b> — RTMP/RTMPS bağlantı sağlığı. Remote IP görünür.</li>
      <li><b>Sonraki Ankara Shorts</b> — systemd timer'dan geri sayım. <2dk olunca turuncu pulse.</li>
      <li><b>Bugün Uploads</b> — Şehir bazlı YouTube upload sayısı (24h).</li>
      <li><b>Stream FFmpeg</b> — Mixer process'inin PID/uptime/CPU/RAM.</li>
      <li><b>Şehir Tamponları</b> — Her şehir için segment sayısı, brightness, motion, son segment yaşı.</li>
      <li><b>Servis Sağlığı</b> — 7 servisin DB heartbeat'i. <30s = canlı.</li>
      <li><b>Olay Zaman Çizelgesi</b> — block_start, camera_change, errors — kronolojik.</li>
      <li><b>Son Uploads</b> — YouTube linkleri tıklanabilir.</li>
      <li><b>Sistem Kaynakları</b> — CPU load, RAM, Disk, FFmpeg, Swap. Renkli bar'lar (yeşil/sarı/kırmızı).</li>
      <li><b>Ankara Plakaları</b> — Son 24h'de Shorts'a alınmış plakalar (dedup).</li>
    </ul>

    <h3>🎬 Yayın Akışı</h3>
    <p>Mixer <b>tek-FFmpeg seamless</b> modunda çalışır:</p>
    <ul>
      <li>Her ~12 dakikada 4 şehri tek concat dosyasında topluyor.</li>
      <li>Drawtext (şehir adı + hava) <code>enable='between(t,start,end)'</code> ile zaman bazlı.</li>
      <li>Tek FFmpeg = MediaMTX publisher sürekli aktif = Tee FFmpeg ölmüyor = YouTube/Kick reconnect derdi yok.</li>
      <li>Sadece her tam tur sonunda 1-2 saniye gap olur.</li>
    </ul>

    <h3>📱 Saatlik Ankara Shorts</h3>
    <ul>
      <li>systemd timer her saat <code>:15</code>'te <code>kshorts-shorts@ankara.service</code> tetikler.</li>
      <li>Servis: DB'den son 30dk'nın <b>parlaklık ≥50, motion ≥5</b> ve <b>kullanılmamış plakalı</b> segmentleri çeker.</li>
      <li>Top 10 adayda YOLO subprocess (ana process'i kirletmez) → ilk geçen kullanılır.</li>
      <li>40s kırpıp 1080×1920 dikey + blurred bg + drawtext → audio mix + TTS → YouTube upload.</li>
      <li>Subprocess çıkışta RAM tamamen serbest kalır (v4'te 772 MB sürekli yüklüydü, v5'te sıfır).</li>
    </ul>

    <h3>🔧 Sorun çözme</h3>
    <ul>
      <li><b>Yayın gitti</b> — TCP YouTube/Kick "yok" gözüküyor → <code>systemctl restart mediamtx</code> + bekle 10s.</li>
      <li><b>Şehir tamponu 0</b> — ingest @ city ölmüş → <code>systemctl restart kshorts-ingest@&lt;city&gt;</code></li>
      <li><b>Mixer speed 0</b> — Yeni başladı (15s bekle) veya CPU bottleneck (load &gt;3 ise).</li>
      <li><b>Shorts başarısız</b> — YOLO max_tries 10 geçemedi (gece, kötü açı). Sonraki saat tekrar denenir.</li>
      <li><b>Tüm rollback</b> — <code>git reset --hard v4-stable-2026-05-19 &amp;&amp; systemctl restart kamerashorts-*</code></li>
    </ul>

    <h3>🗂️ Önemli yollar</h3>
    <ul>
      <li><code>/opt/KameraShorts/</code> — kaynak kod (master + v5 branch)</li>
      <li><code>/var/lib/kamerashorts/segments/&lt;city&gt;/</code> — HLS segmentleri</li>
      <li><code>/var/lib/kamerashorts/media.db</code> — SQLite paylaşımlı state</li>
      <li><code>/etc/kamerashorts/secrets.env</code> — Sırlar (chmod 600)</li>
      <li><code>/etc/systemd/system/kshorts-*</code> — Unit dosyaları</li>
      <li><code>journalctl -u kshorts-mixer -f</code> — Canlı log takibi</li>
    </ul>
  </div>
</div>

<div class="wrap">
  <div class="header">
    <h1><span id="status-indicator"></span>KameraShorts <span class="v">v5</span></h1>
    <span id="header-uptime" class="right"></span>
  </div>

  <!-- ALARM: yayın offline -->
  <div class="alarm-bar" id="alarm-bar">
    <div class="alarm-icon">🚨</div>
    <div class="alarm-content">
      <div class="alarm-title" id="alarm-title">YAYIN OFFLINE</div>
      <div class="alarm-detail" id="alarm-detail"></div>
    </div>
  </div>

  <!-- HERO: şu an yayında -->
  <div id="hero" class="hero">
    <div class="hero-row1">
      <div class="hero-city" id="hero-city">—</div>
      <div class="hero-pill-group" id="hero-pills"></div>
    </div>
    <div class="progress"><div class="progress-fill" id="hero-progress" style="width:0%"></div></div>
    <div class="hero-grid">
      <div class="hero-stat"><div class="lbl">Speed</div><div class="val" id="hero-speed">–</div><div class="sub">target 1.00x</div></div>
      <div class="hero-stat"><div class="lbl">FPS</div><div class="val" id="hero-fps">–</div><div class="sub" id="hero-frame"></div></div>
      <div class="hero-stat"><div class="lbl">Bitrate</div><div class="val" id="hero-bitrate">–</div><div class="sub">target 2500</div></div>
      <div class="hero-stat"><div class="lbl">Blok ilerleme</div><div class="val" id="hero-elapsed">–</div><div class="sub" id="hero-block-total"></div></div>
    </div>
  </div>

  <!-- Quick stats -->
  <div class="grid g4">
    <div class="card">
      <h2>YouTube → Kick</h2>
      <div id="qs-tcp"></div>
    </div>
    <div class="card">
      <h2>Sonraki Ankara Shorts</h2>
      <div class="countdown" id="qs-countdown">—</div>
      <div style="text-align:center;color:var(--muted);font-size:11px;margin-top:8px" id="qs-next-info"></div>
    </div>
    <div class="card">
      <h2>Bugün Uploads</h2>
      <div id="qs-uploads"></div>
    </div>
    <div class="card">
      <h2>Stream FFmpeg</h2>
      <div id="qs-stream"></div>
    </div>
  </div>

  <!-- Mid panels -->
  <div class="grid g21">
    <div class="card">
      <h2>Şehir Segment Tamponları</h2>
      <div id="cities-list"></div>
    </div>
    <div class="card">
      <h2>Servis Sağlığı</h2>
      <div id="services-list"></div>
    </div>
  </div>

  <div class="grid g2">
    <div class="card">
      <h2>Olay Zaman Çizelgesi</h2>
      <div class="ev-list" id="events-list"></div>
    </div>
    <div class="card">
      <h2>Son YouTube Upload'ları</h2>
      <table class="uptable" id="uploads-table"></table>
    </div>
  </div>

  <!-- Bottom -->
  <div class="grid g2">
    <div class="card">
      <h2>Sistem Kaynakları</h2>
      <div id="resources"></div>
    </div>
    <div class="card">
      <h2>Ankara — Son Plakalar (24h: <span id="plates-24h">0</span>)</h2>
      <div id="plates-list" class="plate-row"></div>
    </div>
  </div>

</div>

<script>
const $ = id => document.getElementById(id);
function ageStr(s){if(s==null)return"–";if(s<60)return s+"s";if(s<3600)return Math.floor(s/60)+"dk";return Math.floor(s/3600)+"sa "+Math.floor((s%3600)/60)+"dk"}
function pill(text, kind){return `<span class="pill ${kind}"><span class="pd"></span>${text}</span>`}
function speedClass(s){if(!s)return"idle";if(s>=0.97&&s<=1.05)return"good";if(s>=0.85)return"warn";return"bad"}

const CITY_FLAG = {ankara:"🌍",istanbul:"🌉",corum:"🏛️",konya:"🕌"};
const CITY_LABEL = {ankara:"Ankara",istanbul:"İstanbul",corum:"Çorum",konya:"Konya"};

async function refresh(){
  let d;
  try{
    const r = await fetch("/api");
    d = await r.json();
  } catch(e){
    $("status-indicator").classList.remove("live");
    return;
  }

  // Header
  $("status-indicator").classList.add("live");
  $("header-uptime").textContent = `${d.server_time} · mixer uptime ${ageStr(d.uptime_sec)}`;

  // ── ALARM BAR (yayın offline)
  const alarm = $("alarm-bar");
  if (d.broadcast_offline && d.broadcast_offline.length > 0) {
    alarm.classList.add("show");
    const offline = d.broadcast_offline.map(o => `${o.name} (${o.since_seconds}s)`).join(" + ");
    $("alarm-title").textContent = `YAYIN OFFLINE: ${offline}`;
    $("alarm-detail").textContent = "MediaMTX tee bağlantısı koptu. Mixer log + Olay Zaman Çizelgesi'nde detayları gör. systemctl restart mediamtx ile manuel müdahale.";
  } else {
    alarm.classList.remove("show");
  }

  // ── HERO
  const m = d.mixer || {};
  $("hero-city").textContent = m.active_label || "—";
  const speedC = speedClass(m.last_speed);
  $("hero-pills").innerHTML = [
    pill(`speed ${(m.last_speed||0).toFixed(2)}x`, speedC=="good"?"ok":speedC=="warn"?"warn":"bad"),
    pill(`son ${m.last_age||0}s once`, (m.last_age||999)<5?"ok":"warn"),
    d.tcp.youtube.active ? pill("YouTube ✓","ok") : pill("YouTube ✗","bad"),
    d.tcp.kick.active ? pill("Kick ✓","ok") : pill("Kick ✗","bad"),
  ].join("");
  $("hero-speed").textContent = (m.last_speed||0).toFixed(2) + "x";
  $("hero-fps").textContent = (m.last_fps||0).toFixed(0);
  $("hero-frame").textContent = "frame " + (m.last_frame || 0);
  $("hero-bitrate").textContent = (m.last_bitrate_k||0) + "k";
  $("hero-elapsed").textContent = (m.block_elapsed||0) + "s";
  $("hero-block-total").textContent = "/ " + (m.block_duration||0) + "s · %" + (m.block_pct||0);
  $("hero-progress").style.width = (m.block_pct||0) + "%";

  // ── TCP YT+Kick card
  let tcpHtml = "";
  const yt = d.tcp.youtube, ki = d.tcp.kick;
  tcpHtml += `<div class="qstat"><div class="icon">📺</div><div class="body">
    <div class="label">YouTube RTMP</div>
    <div class="value">${yt.active?yt.remote:"YOK"}</div></div></div>`;
  tcpHtml += `<div class="qstat" style="margin-top:6px"><div class="icon">🎮</div><div class="body">
    <div class="label">Kick RTMPS</div>
    <div class="value">${ki.active?ki.remote:"YOK"}</div></div></div>`;
  $("qs-tcp").innerHTML = tcpHtml;

  // ── Countdown
  const ns = d.next_shorts;
  if(ns.next_in_seconds != null){
    const min = Math.floor(ns.next_in_seconds/60), sec = ns.next_in_seconds%60;
    $("qs-countdown").textContent = `${String(min).padStart(2,"0")}:${String(sec).padStart(2,"0")}`;
    $("qs-countdown").classList.toggle("imminent", ns.next_in_seconds<120);
    $("qs-next-info").innerHTML = `${ns.next_run||"–"} · son: ${ns.last_run||"–"} (${ns.last_result||"?"})`;
  } else {
    $("qs-countdown").textContent = "–";
    $("qs-next-info").textContent = "timer hazır değil";
  }

  // ── Uploads today
  let upHtml = "";
  for(const c of ["ankara","istanbul","corum","konya"]){
    const n = d.uploads_today[c]||0;
    upHtml += `<div class="qstat" style="margin-bottom:4px">
      <div class="icon">${CITY_FLAG[c]}</div>
      <div class="body"><div class="label">${CITY_LABEL[c]}</div>
      <div class="value">${n} video</div></div></div>`;
  }
  $("qs-uploads").innerHTML = upHtml;

  // ── Stream FFmpeg
  const sf = d.stream_ffmpeg;
  let sfHtml = "";
  if(sf.pid){
    sfHtml = `<div class="qstat"><div class="icon">⚡</div><div class="body">
      <div class="label">PID ${sf.pid} · ${ageStr(sf.uptime_sec)}</div>
      <div class="value">${sf.cpu_pct.toFixed(1)}% CPU · ${sf.rss_mb}MB RAM</div></div></div>`;
  } else {
    sfHtml = `<div class="empty">FFmpeg bulunamadı</div>`;
  }
  $("qs-stream").innerHTML = sfHtml;

  // ── Cities
  let cHtml = "";
  let maxSeg = 1;
  for(const k in d.cities) maxSeg = Math.max(maxSeg, d.cities[k].segment_count);
  for(const ckey of ["ankara","istanbul","corum","konya"]){
    const ci = d.cities[ckey];
    if(!ci) continue;
    const age = ci.last_segment_age;
    const ageCls = age==null?"bad":age<10?"ok":age<60?"warn":"bad";
    const ageTxt = age==null?"–":age+"s";
    const pct = Math.round((ci.segment_count/maxSeg)*100);
    cHtml += `<div class="city-row">
      <div class="flag">${CITY_FLAG[ckey]}</div>
      <div class="name">${ci.label}</div>
      <div class="bar-wrap"><div class="bar" style="width:${pct}%"></div></div>
      <div class="stats">${ci.segment_count}seg · ${ci.total_mb}MB · b${ci.avg_brightness} m${ci.avg_motion}</div>
      <div class="age-pill ${ageCls}">${ageTxt}</div>
    </div>`;
  }
  $("cities-list").innerHTML = cHtml;

  // ── Services
  let svcHtml = "";
  const svcOrder = ["mixer","ingest-ankara","ingest-istanbul","ingest-corum","ingest-konya","cleaner","dashboard"];
  for(const name of svcOrder){
    const s = d.services[name];
    if(!s){
      svcHtml += `<div class="srow"><span class="pill gray"><span class="pd"></span>?</span>
        <div class="name">${name}</div><div class="age">—</div></div>`;
      continue;
    }
    const p = s.alive ? pill("aktif","ok") : pill("ölü","bad");
    svcHtml += `<div class="srow">${p}<div class="name">${name}</div>
      <div class="age">${ageStr(s.last_seen)}</div></div>`;
  }
  $("services-list").innerHTML = svcHtml;

  // ── Events
  let evHtml = "";
  if(d.events.length===0){
    evHtml = `<div class="empty">henüz olay yok</div>`;
  } else {
    for(const ev of d.events){
      const sev = ev.severity || "info";
      evHtml += `<div class="ev">
        <div class="ev-time">${ev.time}</div>
        <div class="ev-tag ${sev}">${ev.kind}</div>
        <div class="ev-svc">${ev.service}</div>
        <div class="ev-msg">${ev.message}</div>
      </div>`;
    }
  }
  $("events-list").innerHTML = evHtml;

  // ── Uploads table
  let utHtml = "<thead><tr><th>Saat</th><th>Şehir</th><th>Başlık</th><th></th></tr></thead><tbody>";
  if(d.recent_uploads.length===0){
    utHtml += "<tr><td colspan=4><div class=empty>henüz upload yok</div></td></tr>";
  } else {
    for(const u of d.recent_uploads){
      const t = new Date(u.uploaded_at*1000);
      const hm = String(t.getHours()).padStart(2,"0")+":"+String(t.getMinutes()).padStart(2,"0");
      const titleShort = (u.title||"").length>50?(u.title||"").substring(0,50)+"…":(u.title||"");
      utHtml += `<tr><td>${hm}</td><td>${CITY_FLAG[u.city]||"📍"} ${CITY_LABEL[u.city]||u.city}</td>
        <td>${titleShort}</td><td><a href="${u.url}" target=_blank>▶ izle</a></td></tr>`;
    }
  }
  utHtml += "</tbody>";
  $("uploads-table").innerHTML = utHtml;

  // ── Resources
  const r = d.resources;
  const memPct = r.mem_total_mb?Math.round((r.mem_used_mb/r.mem_total_mb)*100):0;
  const memCls = memPct<60?"low":memPct<85?"mid":"high";
  const loadPct = Math.min(100, Math.round(r.load1/3*100));
  const loadCls = loadPct<60?"low":loadPct<85?"mid":"high";
  const diskCls = r.disk_used_pct<70?"low":r.disk_used_pct<90?"mid":"high";
  $("resources").innerHTML = `
    <div class="res"><div class="res-h"><span class="lbl">CPU Load</span>
      <span class="val">${r.load1.toFixed(2)} / ${r.load5.toFixed(2)} / ${r.load15.toFixed(2)}</span></div>
      <div class="res-bar"><div class="res-fill ${loadCls}" style="width:${loadPct}%"></div></div></div>
    <div class="res"><div class="res-h"><span class="lbl">RAM</span>
      <span class="val">${r.mem_used_mb} / ${r.mem_total_mb} MB</span></div>
      <div class="res-bar"><div class="res-fill ${memCls}" style="width:${memPct}%"></div></div></div>
    <div class="res"><div class="res-h"><span class="lbl">Disk</span>
      <span class="val">${r.disk_used_pct}% (${r.disk_free_gb}GB boş)</span></div>
      <div class="res-bar"><div class="res-fill ${diskCls}" style="width:${r.disk_used_pct}%"></div></div></div>
    <div class="res"><div class="res-h"><span class="lbl">FFmpeg toplam</span>
      <span class="val">${r.ffmpeg_count} adet · ${r.ffmpeg_total_cpu.toFixed(1)}% CPU</span></div></div>
    <div class="res"><div class="res-h"><span class="lbl">Swap</span>
      <span class="val">${r.swap_used_mb} / ${r.swap_total_mb} MB</span></div></div>
  `;

  // ── Plates
  $("plates-24h").textContent = d.ankara_plates_24h;
  let pHtml = "";
  for(const p of d.plates_recent){
    pHtml += `<span class="plate" title="${p.time}">${p.plate}</span>`;
  }
  $("plates-list").innerHTML = pHtml || `<div class="empty">henüz plaka kullanılmamış</div>`;
}

refresh();
setInterval(refresh, 2000);

// Help modal
const helpBtn = $("help-btn");
const helpModal = $("help-modal");
const helpClose = $("help-close");
helpBtn.addEventListener("click", () => helpModal.classList.add("open"));
helpClose.addEventListener("click", () => helpModal.classList.remove("open"));
helpModal.addEventListener("click", e => {
  if(e.target === helpModal) helpModal.classList.remove("open");
});

// Diagnose modal
const diagBtn = $("diag-btn");
const diagModal = $("diag-modal");
const diagClose = $("diag-close");
diagClose.addEventListener("click", () => diagModal.classList.remove("open"));
diagModal.addEventListener("click", e => {
  if(e.target === diagModal) diagModal.classList.remove("open");
});

async function runDiagnose(){
  diagModal.classList.add("open");
  diagBtn.classList.add("loading");
  $("diag-body").innerHTML = '<div style="text-align:center;padding:30px;color:var(--muted)">Tanı çalışıyor... 5-15s sürebilir</div>';
  $("diag-ts").textContent = "—";
  try {
    const r = await fetch("/api/diagnose");
    const d = await r.json();
    $("diag-ts").textContent = `Tamamlandı: ${d.ts} • ${d.issue_count} sorun`;
    if (d.issue_count === 0) {
      $("diag-body").innerHTML = '<div class=all-ok>✓ TÜM SİSTEM SAĞLIKLI<br><span style="font-size:11px;color:var(--muted);font-weight:400">11 bölüm kontrol edildi, hata yok</span></div>';
    } else {
      let html = '';
      for (const i of d.issues) {
        html += `<div class=issue-row><div class=issue-section>${i.section}</div><div class=issue-text>${i.issue}</div></div>`;
      }
      $("diag-body").innerHTML = html;
    }
  } catch(e) {
    $("diag-body").innerHTML = `<div class=all-ok style="background:rgba(239,68,68,.1);color:var(--red)">Tanı başarısız: ${e}</div>`;
  } finally {
    diagBtn.classList.remove("loading");
  }
}
diagBtn.addEventListener("click", runDiagnose);
document.addEventListener("keydown", e => {
  if(e.key === "Escape") helpModal.classList.remove("open");
  if(e.key === "?" || e.key === "/") {
    if(!["INPUT","TEXTAREA"].includes(document.activeElement.tagName)){
      helpModal.classList.toggle("open");
    }
  }
});
</script>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        try:
            if self.path == "/" or self.path.startswith("/index"):
                body = HTML.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            elif self.path.startswith("/api/diagnose"):
                # Diagnose tool'u JSON formatinda calistir
                r = subprocess.run(
                    [sys.executable, "-m", "v5.diagnose", "--json"],
                    capture_output=True, text=True, timeout=30,
                    cwd="/opt/KameraShorts",
                )
                # JSON ciktisi son blok, ondan once human text var
                out = r.stdout
                json_start = out.rfind("{")
                payload = out[json_start:] if json_start > 0 else "{}"
                try:
                    diag = json.loads(payload)
                except Exception:
                    diag = {"error": "diagnose parse failed",
                            "stdout": out[-1000:]}
                # Issues listesi
                issues = []
                for sect, info_d in diag.items():
                    if isinstance(info_d, dict):
                        for i in info_d.get("issues", []):
                            issues.append({"section": sect, "issue": i})
                body = json.dumps({
                    "ts": time.strftime("%H:%M:%S"),
                    "issue_count": len(issues),
                    "issues": issues,
                    "sections": diag,
                    "ok": r.returncode == 0,
                }, default=str, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type",
                                 "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body)
            elif self.path.startswith("/api"):
                body = json.dumps(snapshot(), default=str,
                                  ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type",
                                 "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            log.warning("HTTP hata: %s", e)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args()

    common.start_heartbeat("dashboard", interval=10)
    log.info("dashboard %s:%d", args.host, args.port)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    server.server_close()


if __name__ == "__main__":
    main()
