#!/usr/bin/env python3
"""KameraShorts v4 — Sistem Tani Araci.

Tek komutla tum sistem durumu. Hata tespiti icin tasarlanmistir.

Kullanim:
    python diagnose.py                # tam rapor (renkli)
    python diagnose.py --json         # JSON cikti (parse edilebilir)
    python diagnose.py --short        # sadece sorunlar
    python diagnose.py --section tcp  # tek bolum

Bolümler:
    services    — systemctl is-active (4 servis)
    processes   — pgrep ffmpeg/python + RSS + CPU + uptime
    tcp         — YouTube/Kick TCP ESTAB + send_q
    mediamtx    — path durum + tracks + bytes
    fifo        — stream.pipe var mi, modified yas
    batches     — /tmp/ks_v4/batch_*.ts sayisi, son batch yasi
    stream      — speed/fps/frame (son log satirindan)
    log_errors  — son 200 log satirinda kritik anahtar kelimeler
    resources   — CPU load, RAM, disk, ffmpeg CPU
    harvester   — son upload yasi, attempts/success/failed
"""
import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional


# ─── Renkli cikti ─────────────────────────────────────────────────────────────

USE_COLOR = sys.stdout.isatty()

class C:
    RESET = "\033[0m" if USE_COLOR else ""
    BOLD = "\033[1m" if USE_COLOR else ""
    DIM = "\033[2m" if USE_COLOR else ""
    RED = "\033[31m" if USE_COLOR else ""
    GREEN = "\033[32m" if USE_COLOR else ""
    YELLOW = "\033[33m" if USE_COLOR else ""
    CYAN = "\033[36m" if USE_COLOR else ""


def hdr(text):
    return f"{C.BOLD}{C.CYAN}━━━ {text} ━━━{C.RESET}"


def ok(t): return f"{C.GREEN}✓{C.RESET} {t}"
def warn(t): return f"{C.YELLOW}⚠{C.RESET} {t}"
def err(t): return f"{C.RED}✗{C.RESET} {t}"
def info(t): return f"{C.DIM}  {t}{C.RESET}"


# ─── Yardimcilar ──────────────────────────────────────────────────────────────

def run(cmd, timeout=5):
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
    issues = []
    services = ["kamerashorts-live", "kamerashorts-harvester",
                "kamerashorts-dashboard", "mediamtx"]
    rows = []
    for s in services:
        _, out, _ = run(["systemctl", "is-active", s])
        state = out.strip()
        rows.append({"service": s, "state": state})
        if state != "active":
            issues.append(f"servis {s} = {state}")
    return rows, issues


def check_processes():
    issues = []
    _, out, _ = run(["ps", "-eo", "pid,etimes,pcpu,rss,cmd", "--no-headers"])
    targets = {
        "stream_ffmpeg": "stream.pipe",
        "tee_ffmpeg": "flv:onfail=ignore",
        "mediamtx": "mediamtx /etc",
        "live_streamer": "live_streamer.py",
        "harvester": "harvester.py",
    }
    found = {k: None for k in targets}
    for line in out.splitlines():
        for tag, pat in targets.items():
            if pat in line and found[tag] is None:
                parts = line.split(None, 4)
                if len(parts) >= 5:
                    found[tag] = {
                        "pid": parts[0], "uptime_s": int(parts[1]),
                        "cpu_pct": float(parts[2]),
                        "rss_mb": int(parts[3]) // 1024,
                    }
    for tag, info_d in found.items():
        if info_d is None and tag in ("stream_ffmpeg", "mediamtx",
                                       "live_streamer"):
            issues.append(f"{tag} process bulunamadi")
    return found, issues


def check_tcp():
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
        elif ":443" in remote and (remote.startswith("35.") or
                                    "live-video" in remote):
            kick = {"remote": remote, "send_q": send_q}
    if not yt:
        issues.append("YouTube RTMP TCP yok (YouTube'a yayın gitmiyor)")
    if not kick:
        issues.append("Kick RTMPS TCP yok (Kick'e yayın gitmiyor)")
    return {"youtube": yt, "kick": kick}, issues


def check_mediamtx():
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
            })
        live = next((p for p in paths if p["name"] == "live/stream"), None)
        if not live:
            issues.append("MediaMTX live/stream path tanımlı değil")
        elif not live["ready"]:
            issues.append("MediaMTX live/stream NOT READY (publisher yok)")
    except Exception as e:
        issues.append(f"MediaMTX API erişilemedi: {e}")
    return paths, issues


def check_fifo():
    issues = []
    fifo = Path("/tmp/ks_v4/stream.pipe")
    if not fifo.exists():
        issues.append("FIFO pipe yok (/tmp/ks_v4/stream.pipe)")
        return {"exists": False}, issues
    st = fifo.stat()
    return {
        "exists": True,
        "modified_age_s": int(time.time() - st.st_mtime),
    }, issues


def check_batches():
    issues = []
    work = Path("/tmp/ks_v4")
    if not work.exists():
        issues.append("/tmp/ks_v4 yok")
        return {}, issues
    batches = sorted(work.glob("batch_*.ts"),
                     key=lambda p: p.stat().st_mtime, reverse=True)
    info_d = {"count": len(batches)}
    if batches:
        latest = batches[0]
        age = int(time.time() - latest.stat().st_mtime)
        info_d["last_name"] = latest.name
        info_d["last_age_s"] = age
        info_d["last_size_mb"] = latest.stat().st_size // (1024 * 1024)
        if age > 1200:  # 20 dk
            issues.append(f"Son batch {age_str(age)} önce (BatchBuilder takılı?)")
    else:
        issues.append("/tmp/ks_v4'de batch dosyası yok (henüz oluşmadı?)")
    return info_d, issues


def check_stream():
    """Live log'dan son durum (speed/fps/frame)."""
    issues = []
    log_path = Path("/var/log/kamerashorts-live.log")
    info_d = {}
    if not log_path.exists():
        return info_d, issues

    try:
        # Son 50 satırı oku (tail -50)
        with open(log_path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 20000))
            tail = f.read().decode("utf-8", errors="replace")
        durum_re = re.compile(
            r"\[durum\] batch=(\S*) speed=([\d.]+)x fps=([\d.]+) frame=(\d+)"
        )
        last = None
        for line in tail.splitlines():
            m = durum_re.search(line)
            if m:
                last = m
        if last:
            info_d["batch"] = last.group(1) or "(filler)"
            info_d["speed"] = float(last.group(2))
            info_d["fps"] = float(last.group(3))
            info_d["frame"] = int(last.group(4))
            if info_d["speed"] < 0.85:
                issues.append(f"stream speed düşük: {info_d['speed']:.2f}x")
    except Exception as e:
        issues.append(f"stream log okuma: {e}")
    return info_d, issues


def check_log_errors():
    """Son 200 satırda kritik anahtar kelimeler."""
    issues = []
    _, out, _ = run(["journalctl", "-u", "kamerashorts-live",
                     "-n", "200", "--no-pager"])
    critical = ["Broken pipe", "Exiting normally, received signal",
                "Connection refused", "Connection reset",
                "watchdog", "FFmpeg çöktü", "Kırık pipe", "FIFO broken"]
    found = []
    for line in out.splitlines():
        for kw in critical:
            if kw in line:
                found.append(line[-120:])
                break
    if found:
        for f in found[-3:]:
            issues.append(f"log: {f[-100:]}")
    return found[-10:], issues


def check_resources():
    issues = []
    out = {}
    try:
        with open("/proc/loadavg") as f:
            parts = f.read().split()
        out["load1"] = float(parts[0])
        out["load5"] = float(parts[1])
        if out["load1"] > 3.0:
            issues.append(f"load1 yüksek: {out['load1']:.2f}")
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
        pct = out["mem_used_mb"] / out["mem_total_mb"] * 100 if out["mem_total_mb"] else 0
        if pct > 90:
            issues.append(f"RAM dolu: %{pct:.0f}")
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


def check_harvester():
    issues = []
    info_d = {}
    stats_path = Path("/opt/KameraShorts/data/harvester_stats.json")
    if stats_path.exists():
        try:
            stats = json.loads(stats_path.read_text(encoding="utf-8"))
            for city in ("ankara", "istanbul", "corum", "konya"):
                if city in stats:
                    s = stats[city]
                    info_d[city] = {
                        "attempts": s.get("attempts", 0),
                        "success": s.get("success", 0),
                        "failed": s.get("failed", 0),
                        "last_status": s.get("last_status", "?"),
                        "last_run": s.get("last_run"),
                    }
        except Exception as e:
            issues.append(f"harvester_stats.json okunamadi: {e}")
    # Son upload yası
    log_path = Path("/opt/KameraShorts/logs/pipeline.log")
    if log_path.exists():
        try:
            with open(log_path, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - 5000))
                tail = f.read().decode("utf-8", errors="replace")
            last_upload = None
            for line in tail.splitlines():
                if "UPLOADED" in line:
                    last_upload = line
            if last_upload:
                from datetime import datetime
                ts_str = last_upload.split(" ", 1)[0]
                try:
                    dt = datetime.fromisoformat(ts_str.replace("T", " "))
                    age = int((datetime.now() - dt).total_seconds())
                    info_d["last_upload_age_s"] = age
                    info_d["last_upload"] = last_upload[:140]
                    if age > 7200:
                        issues.append(
                            f"son upload {age_str(age)} önce "
                            f"(2sa+: harvester sorun olabilir)")
                except Exception:
                    pass
        except Exception:
            pass
    return info_d, issues


# ─── Ana akis ────────────────────────────────────────────────────────────────

SECTIONS = [
    ("services", check_services),
    ("processes", check_processes),
    ("tcp", check_tcp),
    ("mediamtx", check_mediamtx),
    ("fifo", check_fifo),
    ("batches", check_batches),
    ("stream", check_stream),
    ("log_errors", check_log_errors),
    ("resources", check_resources),
    ("harvester", check_harvester),
]


def print_section(name, data, issues):
    print()
    print(hdr(name.upper()))
    if isinstance(data, dict):
        for k, v in data.items():
            print(info(f"{k}: {v}"))
    elif isinstance(data, list):
        for item in data[:10]:
            print(info(str(item)))
    elif data is not None:
        print(info(str(data)))
    for i in issues:
        print(err(i))
    if not issues:
        print(ok("OK"))


def main():
    parser = argparse.ArgumentParser(description="KameraShorts v4 diagnose")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--short", action="store_true")
    parser.add_argument("--section", choices=[s[0] for s in SECTIONS])
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

    if args.json:
        print(json.dumps(results, default=str, ensure_ascii=False))
        sys.exit(rc)

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
