#!/usr/bin/env python3
"""KameraShorts v5 — Tani araci.

Tek komutla tum sistem durum raporu uretir. Hata tespiti icin tasarlanmis.

Kullanim:
    python -m v5.diagnose                    # tam rapor
    python -m v5.diagnose --json             # JSON cikti (pars edilebilir)
    python -m v5.diagnose --short            # kisa ozet (sadece sorunlar)
    python -m v5.diagnose --section stream   # tek bolum

Bolümler:
    services    — systemctl is-active
    processes   — pgrep + RSS + CPU + uptime
    tcp         — YouTube/Kick TCP ESTAB + send_q
    mediamtx    — path durum + tracks + bytes
    fifo        — pipe var mi, yazici aktif mi
    segments    — disk + DB sayilari, taze segment yaslar
    mixer       — DB state, ffmpeg progress
    log_errors  — son 100 log satirinda kritik hatalar
    db_health   — SQLite size, tablo satir sayilari
    resources   — CPU/RAM/disk
    shorts      — saatlik upload durumu

Cikti formati: hata bulunan bolümler kirmizi (HATA), uyari sarid (UYARI),
saglikli yesil (OK). Sondaki ozette: bulunan tum sorunlar liste halinde.
"""
import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

try:
    from v5 import db
    DB_AVAILABLE = True
except Exception:
    DB_AVAILABLE = False


# ─── Renkli cikti ─────────────────────────────────────────────────────────────

USE_COLOR = sys.stdout.isatty()

class C:
    RESET = "\033[0m" if USE_COLOR else ""
    BOLD = "\033[1m" if USE_COLOR else ""
    DIM = "\033[2m" if USE_COLOR else ""
    RED = "\033[31m" if USE_COLOR else ""
    GREEN = "\033[32m" if USE_COLOR else ""
    YELLOW = "\033[33m" if USE_COLOR else ""
    BLUE = "\033[34m" if USE_COLOR else ""
    CYAN = "\033[36m" if USE_COLOR else ""


def hdr(text):
    return f"{C.BOLD}{C.CYAN}━━━ {text} ━━━{C.RESET}"


def ok(text): return f"{C.GREEN}✓{C.RESET} {text}"
def warn(text): return f"{C.YELLOW}⚠{C.RESET} {text}"
def err(text): return f"{C.RED}✗{C.RESET} {text}"
def info(text): return f"{C.DIM}  {text}{C.RESET}"


# ─── Yardımcılar ──────────────────────────────────────────────────────────────

def run(cmd, timeout=5) -> tuple[int, str, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except Exception as e:
        return -1, "", str(e)


def age_str(secs: Optional[int]) -> str:
    if secs is None or secs < 0:
        return "?"
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs//60}dk"
    return f"{secs//3600}sa {(secs%3600)//60}dk"


# ─── Kontroller ───────────────────────────────────────────────────────────────

def check_services():
    """systemctl is-active 6 servis."""
    issues = []
    services = [
        "kshorts-mixer", "mediamtx",
        "kshorts-ingest@ankara", "kshorts-ingest@istanbul",
        "kshorts-ingest@corum", "kshorts-ingest@konya",
        "kshorts-cleaner", "kshorts-dashboard",
    ]
    rows = []
    for s in services:
        _, out, _ = run(["systemctl", "is-active", s])
        state = out.strip()
        rows.append((s, state))
        if state != "active":
            issues.append(f"servis {s} = {state}")
    return rows, issues


def check_processes():
    """Ana ffmpeg ve python process'leri."""
    issues = []
    rows = []
    _, out, _ = run([
        "ps", "-eo", "pid,etimes,pcpu,rss,comm,cmd", "--no-headers",
    ])
    targets = {
        "mixer_ffmpeg": "stream.pipe",
        "tee_ffmpeg": "flv:onfail=ignore",
        "ingest": "v5.ingest",
        "mediamtx": "mediamtx /etc",
    }
    found = {k: [] for k in targets}
    for line in out.splitlines():
        for tag, pat in targets.items():
            if pat in line:
                parts = line.split(None, 5)
                if len(parts) >= 6:
                    pid, etime, cpu, rss, comm, cmd = parts
                    found[tag].append({
                        "pid": pid, "uptime_s": int(etime),
                        "cpu_pct": float(cpu),
                        "rss_mb": int(rss) // 1024,
                    })
    for tag, items in found.items():
        if not items:
            rows.append((tag, None))
            if tag in ("mixer_ffmpeg", "mediamtx"):
                issues.append(f"{tag} process bulunamadi")
        else:
            for it in items:
                rows.append((tag, it))
    return rows, issues


def check_tcp():
    """YouTube/Kick TCP ESTAB."""
    issues = []
    _, out, _ = run(["ss", "-tnp"])
    yt = kick = None
    for line in out.splitlines():
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
            yt = {"remote": remote, "send_q": send_q}
        elif ":443" in remote and (remote.startswith("35.") or "live-video" in remote):
            kick = {"remote": remote, "send_q": send_q}
    if not yt:
        issues.append("YouTube RTMP TCP yok (yayın YouTube'a gitmiyor)")
    if not kick:
        issues.append("Kick RTMPS TCP yok (yayın Kick'e gitmiyor)")
    return {"youtube": yt, "kick": kick}, issues


def check_mediamtx():
    """MediaMTX HTTP API ile path durum."""
    issues = []
    _, out, _ = run(["curl", "-s", "http://127.0.0.1:9997/v3/paths/list"])
    paths = []
    try:
        d = json.loads(out)
        for p in d.get("items", []):
            paths.append({
                "name": p["name"],
                "ready": p["ready"],
                "tracks": len(p.get("tracks", [])),
                "bytes_received": p.get("bytesReceived", 0),
                "bytes_sent": p.get("bytesSent", 0),
            })
        for p in paths:
            if p["name"] == "live/stream" and not p["ready"]:
                issues.append("MediaMTX live/stream path NOT READY (publisher yok)")
    except Exception as e:
        issues.append(f"MediaMTX API erisilemedi: {e}")
    return paths, issues


def check_fifo():
    """FIFO pipe var mi, son yazim zamani."""
    issues = []
    fifo = Path("/var/lib/kamerashorts/work/stream.pipe")
    if not fifo.exists():
        issues.append("FIFO pipe yok (/var/lib/kamerashorts/work/stream.pipe)")
        return {"exists": False}, issues
    st = fifo.stat()
    return {
        "exists": True,
        "is_fifo": True,
        "modified_age_s": int(time.time() - st.st_mtime),
    }, issues


def check_segments():
    """Disk + DB segment durum."""
    issues = []
    summary = {}
    seg_root = Path("/var/lib/kamerashorts/segments")
    for city in ("ankara", "istanbul", "corum", "konya"):
        d = seg_root / city
        disk_count = len(list(d.glob("*.ts"))) if d.exists() else 0
        last_age = None
        if disk_count > 0:
            latest = max(d.glob("*.ts"), key=lambda p: p.stat().st_mtime)
            last_age = int(time.time() - latest.stat().st_mtime)
        db_count = 0
        if DB_AVAILABLE:
            try:
                r = db.conn().execute(
                    "SELECT COUNT(*) FROM segments WHERE city = ? AND expires_at > ?",
                    (city, int(time.time())),
                ).fetchone()
                db_count = r[0] if r else 0
            except Exception:
                pass
        summary[city] = {
            "disk_count": disk_count,
            "db_count": db_count,
            "last_disk_age_s": last_age,
        }
        if disk_count == 0 and city != "konya":
            issues.append(f"{city}: hiç segment yok (disk)")
        elif last_age and last_age > 60:
            issues.append(f"{city}: son segment {age_str(last_age)} once (ingest takilmis?)")
        elif disk_count > 0 and db_count == 0:
            issues.append(f"{city}: diskte var ama DB'de yok (sync sorunu)")
    return summary, issues


def check_mixer_state():
    """DB'den mixer state."""
    issues = []
    state = None
    if DB_AVAILABLE:
        try:
            state = db.get_mixer_state()
        except Exception:
            pass
    if not state:
        issues.append("mixer_state DB'de yok (mixer baslamamis veya kapanmis)")
        return None, issues
    age = int(time.time()) - (state.get("last_update", 0) or 0)
    if age > 10:
        issues.append(f"mixer last_update {age_str(age)} once (DB guncel degil — mixer takilmis?)")
    speed = state.get("last_speed") or 0
    if 0 < speed < 0.85:
        issues.append(f"mixer speed {speed:.2f}x (1.0x olmali — input starve / cpu darbogazi)")
    return {**state, "age_s": age}, issues


def check_log_errors():
    """Mixer log son 100 satirinda kritik anahtar kelimeler."""
    issues = []
    found = []
    _, out, _ = run(["journalctl", "-u", "kshorts-mixer", "-n", "100", "--no-pager"])
    critical = ["Broken pipe", "Exiting normally, received signal",
                "Invalid data found", "Connection refused", "Connection reset",
                "Non-monotonous DTS", "FIFO broken", "ffmpeg cikti rc=",
                "broadcast_down", "starve"]
    for line in out.splitlines():
        for kw in critical:
            if kw in line:
                found.append(line[-120:])
                break
    if found:
        for f in found[-5:]:
            issues.append(f"log: {f[-100:]}")
    return found[-10:], issues


def check_resources():
    """CPU load, RAM, disk."""
    issues = []
    out = {}
    try:
        with open("/proc/loadavg") as f:
            parts = f.read().split()
        out["load1"] = float(parts[0])
        out["load5"] = float(parts[1])
        if out["load1"] > 3.0:
            issues.append(f"load1 yuksek: {out['load1']:.2f}")
    except Exception:
        pass
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
        used_pct = out["mem_used_mb"] / out["mem_total_mb"] * 100 if out["mem_total_mb"] else 0
        if used_pct > 90:
            issues.append(f"RAM dolu: %{used_pct:.0f}")
    except Exception:
        pass
    try:
        import shutil as _sh
        t = _sh.disk_usage("/")
        out["disk_used_pct"] = round(t.used / t.total * 100)
        out["disk_free_gb"] = round(t.free / 1e9, 1)
        if out["disk_used_pct"] > 85:
            issues.append(f"disk dolu: %{out['disk_used_pct']}")
    except Exception:
        pass
    return out, issues


def check_db():
    """SQLite saglik."""
    issues = []
    out = {}
    db_path = Path("/var/lib/kamerashorts/media.db")
    if not db_path.exists():
        issues.append("media.db yok")
        return out, issues
    out["size_mb"] = round(db_path.stat().st_size / 1e6, 2)
    if DB_AVAILABLE:
        try:
            c = db.conn()
            for tbl in ("segments", "used_plates", "service_health",
                        "upload_log", "events", "mixer_state"):
                r = c.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()
                out[f"{tbl}_rows"] = r[0] if r else 0
        except Exception as e:
            issues.append(f"DB query hata: {e}")
    return out, issues


def check_shorts():
    """Saatlik shorts timer ve son upload."""
    issues = []
    out = {}
    _, t_out, _ = run([
        "systemctl", "show", "kshorts-shorts@ankara.timer",
        "--property=NextElapseUSecRealtime",
        "--property=LastTriggerUSec", "--property=Result",
    ])
    for ln in t_out.splitlines():
        if "=" in ln:
            k, _, v = ln.partition("=")
            out[k] = v.strip()
    if DB_AVAILABLE:
        try:
            r = db.conn().execute(
                "SELECT uploaded_at, title FROM upload_log ORDER BY uploaded_at DESC LIMIT 1"
            ).fetchone()
            if r:
                age = int(time.time()) - r[0]
                out["last_upload_age_s"] = age
                out["last_upload_title"] = r[1]
                if age > 7200:
                    issues.append(f"son upload {age_str(age)} once (2 saatten cok — shorts uretmiyor)")
            else:
                issues.append("hic upload yok (henuz shorts uretmedi)")
        except Exception:
            pass
    return out, issues


# ─── Ana akis ────────────────────────────────────────────────────────────────

SECTIONS = [
    ("services", check_services),
    ("processes", check_processes),
    ("tcp", check_tcp),
    ("mediamtx", check_mediamtx),
    ("fifo", check_fifo),
    ("segments", check_segments),
    ("mixer", check_mixer_state),
    ("log_errors", check_log_errors),
    ("resources", check_resources),
    ("db", check_db),
    ("shorts", check_shorts),
]


def print_section(name, data, issues):
    print()
    print(hdr(name.upper()))
    if not issues and data is not None:
        if isinstance(data, dict):
            for k, v in data.items():
                print(info(f"{k}: {v}"))
        elif isinstance(data, list):
            for item in data[:10]:
                print(info(str(item)))
        else:
            print(info(str(data)))
        print(ok(f"OK"))
    else:
        if isinstance(data, dict):
            for k, v in data.items():
                print(info(f"{k}: {v}"))
        elif isinstance(data, list):
            for item in data[:8]:
                print(info(str(item)))
        for i in issues:
            print(err(i))


def main():
    parser = argparse.ArgumentParser(description="KameraShorts v5 diagnose")
    parser.add_argument("--json", action="store_true", help="JSON ciktisi")
    parser.add_argument("--short", action="store_true",
                        help="sadece sorunlar")
    parser.add_argument("--section", choices=[s[0] for s in SECTIONS],
                        help="tek bolüm")
    args = parser.parse_args()

    all_issues = []
    results = {}

    sections = SECTIONS
    if args.section:
        sections = [s for s in SECTIONS if s[0] == args.section]

    for name, fn in sections:
        try:
            data, issues = fn()
        except Exception as e:
            data, issues = None, [f"section {name} hata: {e}"]
        results[name] = {"data": data, "issues": issues}
        all_issues.extend([f"[{name}] {i}" for i in issues])
        if not args.json and not args.short:
            print_section(name, data, issues)

    rc = 0 if not all_issues else 1

    # JSON mode: SADECE JSON, hicbir baska cikti yok (parse temiz)
    if args.json:
        print(json.dumps(results, default=str, ensure_ascii=False))
        sys.exit(rc)

    # Ozet (sadece human-readable mode'larda)
    print()
    print(hdr("OZET"))
    if not all_issues:
        print(ok(f"{C.BOLD}TUM SISTEM SAGLIKLI{C.RESET}"))
    else:
        print(err(f"{C.BOLD}{len(all_issues)} SORUN TESPIT EDILDI:{C.RESET}"))
        for i in all_issues:
            print(f"  {C.RED}•{C.RESET} {i}")
    sys.exit(rc)


if __name__ == "__main__":
    main()
