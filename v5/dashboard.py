#!/usr/bin/env python3
"""KameraShorts v5 — Dashboard.

SQLite read-only. JSON API + HTML SPA.
Eski live_dashboard.py'nin log-tail-parse'i yerine DB query'leri.
"""
import argparse
import json
import os
import subprocess
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from v5 import common, db

log = common.setup_logging("dashboard")


def snapshot() -> dict:
    """Tum dashboard datasi."""
    c = db.conn()
    # Servis sagligi
    health = {}
    for r in c.execute(
        "SELECT service_name, last_heartbeat, status FROM service_health"
    ):
        age = int(time.time()) - r[1]
        health[r[0]] = {
            "status": r[2],
            "last_seen_seconds": age,
            "alive": age < 30,
        }

    # Sehir bazli son segment + counts
    cities = {}
    for city in ("ankara", "istanbul", "corum", "konya"):
        rows = list(c.execute(
            """SELECT COUNT(*), MAX(start_ts), AVG(brightness), AVG(motion)
               FROM segments WHERE city = ? AND expires_at > ?""",
            (city, int(time.time())),
        ))
        n, last_ts, bright, motion = rows[0] if rows else (0, 0, 0, 0)
        cities[city] = {
            "segment_count": n,
            "last_segment_age": (int(time.time()) - last_ts) if last_ts else None,
            "avg_brightness": round(bright or 0, 1),
            "avg_motion": round(motion or 0, 1),
        }

    # Bugunku upload sayisi
    uploads = {}
    for city in ("ankara", "istanbul", "corum", "konya"):
        uploads[city] = db.uploads_today(city)

    # Son 10 upload
    recent_uploads = [
        {"video_id": r[0], "city": r[1], "title": r[2],
         "url": r[3], "uploaded_at": r[4]}
        for r in c.execute(
            """SELECT video_id, city, title, youtube_url, uploaded_at
               FROM upload_log ORDER BY uploaded_at DESC LIMIT 10"""
        )
    ]

    # Sistem kaynaklari
    resources = _sys_resources()

    return {
        "ok": all(h.get("alive") for h in health.values()) if health else False,
        "server_time": time.strftime("%H:%M:%S"),
        "services": health,
        "cities": cities,
        "uploads_today": uploads,
        "recent_uploads": recent_uploads,
        "resources": resources,
    }


def _sys_resources() -> dict:
    """RAM/CPU/disk + ffmpeg sayisi."""
    out = {}
    try:
        with open("/proc/loadavg") as f:
            parts = f.read().split()
        out["load1"] = float(parts[0])
        out["load5"] = float(parts[1])
    except Exception:
        out["load1"] = 0
        out["load5"] = 0
    try:
        with open("/proc/meminfo") as f:
            mem = {}
            for ln in f:
                k, _, v = ln.partition(":")
                mem[k] = int(v.strip().split()[0])
        out["mem_total_mb"] = mem.get("MemTotal", 0) // 1024
        out["mem_avail_mb"] = mem.get("MemAvailable", 0) // 1024
    except Exception:
        out["mem_total_mb"] = 0
        out["mem_avail_mb"] = 0
    try:
        r = subprocess.run(["pgrep", "-c", "ffmpeg"],
                           capture_output=True, text=True, timeout=2)
        out["ffmpeg_count"] = int(r.stdout.strip() or "0")
    except Exception:
        out["ffmpeg_count"] = 0
    try:
        import shutil as _sh
        t = _sh.disk_usage("/")
        out["disk_used_pct"] = round(t.used / t.total * 100)
    except Exception:
        out["disk_used_pct"] = 0
    return out


HTML = """<!DOCTYPE html>
<html lang="tr"><head><meta charset="UTF-8">
<title>KameraShorts v5</title>
<style>
body{background:#0b1120;color:#e2e8f0;font-family:monospace;padding:20px;margin:0}
h1{font-size:18px;color:#fff}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:12px;margin-top:16px}
.card{background:#111827;border:1px solid #1e293b;border-radius:8px;padding:14px}
.lbl{font-size:10px;color:#64748b;text-transform:uppercase;letter-spacing:0.08em}
.val{font-size:20px;color:#fff;margin-top:4px}
.ok{color:#22c55e}.warn{color:#f59e0b}.bad{color:#ef4444}
table{width:100%;border-collapse:collapse;margin-top:8px;font-size:12px}
td{padding:4px 8px;border-bottom:1px solid #1e293b}
a{color:#3b82f6}
</style></head><body>
<h1>KameraShorts v5 — Mikroservis Dashboard</h1>
<div id="content">Yukleniyor...</div>
<script>
async function refresh(){
  const r = await fetch("/api");
  const d = await r.json();
  let html = "<div class=grid>";
  // Servisler
  html += "<div class=card><div class=lbl>Servisler</div><table>";
  for (const [name, info] of Object.entries(d.services || {})) {
    const cls = info.alive ? "ok" : "bad";
    html += `<tr><td>${name}</td><td class=${cls}>${info.alive?"ok":"OLU"}</td><td>${info.last_seen_seconds}s</td></tr>`;
  }
  html += "</table></div>";
  // Sehirler
  html += "<div class=card><div class=lbl>Segment Tamponlari</div><table>";
  for (const [c, info] of Object.entries(d.cities)) {
    const age = info.last_segment_age;
    const cls = age==null?"bad":(age<30?"ok":(age<120?"warn":"bad"));
    html += `<tr><td>${c}</td><td>${info.segment_count} seg</td><td class=${cls}>${age==null?"-":age+"s"}</td><td>bright ${info.avg_brightness}</td></tr>`;
  }
  html += "</table></div>";
  // Uploads
  html += "<div class=card><div class=lbl>Bugun Upload</div><table>";
  for (const [c, n] of Object.entries(d.uploads_today)) {
    html += `<tr><td>${c}</td><td>${n}</td></tr>`;
  }
  html += "</table></div>";
  // Resources
  const res = d.resources;
  html += `<div class=card><div class=lbl>Sistem</div>
    <table>
      <tr><td>Load</td><td>${res.load1} / ${res.load5}</td></tr>
      <tr><td>RAM</td><td>${res.mem_total_mb-res.mem_avail_mb}/${res.mem_total_mb} MB</td></tr>
      <tr><td>Disk</td><td>${res.disk_used_pct}%</td></tr>
      <tr><td>FFmpeg</td><td>${res.ffmpeg_count}</td></tr>
    </table></div>`;
  // Recent uploads
  html += "<div class=card style='grid-column:1/-1'><div class=lbl>Son Upload'lar</div><table>";
  for (const u of d.recent_uploads) {
    html += `<tr><td>${u.city}</td><td>${u.title}</td><td><a href="${u.url}" target=_blank>izle</a></td></tr>`;
  }
  html += "</table></div>";
  html += "</div>";
  html += `<div style='margin-top:14px;color:#64748b;font-size:11px'>Son guncelleme: ${d.server_time}</div>`;
  document.getElementById("content").innerHTML = html;
}
refresh();
setInterval(refresh, 2000);
</script></body></html>"""


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
                self.end_headers()
                self.wfile.write(body)
            elif self.path.startswith("/api"):
                body = json.dumps(snapshot(), default=str).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()
        except Exception as e:
            log.warning("HTTP hata: %s", e)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=5001)
    parser.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args()

    common.start_heartbeat("dashboard", interval=15)
    log.info("dashboard %s:%d", args.host, args.port)
    server = HTTPServer((args.host, args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    server.server_close()


if __name__ == "__main__":
    main()
