"""KameraShorts v5 — paylaşılan yardımcılar.

Tasarım kuralları:
- Logging: structured JSON (parse'lanabilir) + stderr
- Config: YAML + ENV (secrets ENV'de, publik YAML'da)
- Subprocess: process-group leader (start_new_session=True)
- Cleanup: signal handler + atexit
"""
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

import yaml

# ─── Sabitler ─────────────────────────────────────────────────────────────────

CONFIG_PATH = Path("/opt/KameraShorts/config-v5.yaml")
SECRETS_PATH = Path("/etc/kamerashorts/secrets.env")
DATA_ROOT = Path("/var/lib/kamerashorts")
SEGMENTS_DIR = DATA_ROOT / "segments"
WORK_DIR = DATA_ROOT / "work"


# ─── Logging ──────────────────────────────────────────────────────────────────

def setup_logging(service: str, level: int = logging.INFO) -> logging.Logger:
    """journald-uyumlu structured log (key=value)."""
    fmt = f"[{service:<12}] %(levelname)-7s %(message)s"
    logging.basicConfig(
        level=level, format=fmt, stream=sys.stderr,
        # journald zaten timestamp ekler, çift yazma
    )
    # Gürültüyü azalt
    for noisy in ("urllib3", "requests", "charset_normalizer"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return logging.getLogger(service)


# ─── Config + Secrets ─────────────────────────────────────────────────────────

_config_cache: Optional[dict] = None


def load_config() -> dict:
    """config-v5.yaml + secrets.env birleştirilmiş okuma."""
    global _config_cache
    if _config_cache is not None:
        return _config_cache

    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"Config yok: {CONFIG_PATH}")

    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    # secrets.env varsa env vars'a yükle (systemd zaten yapıyor olabilir)
    if SECRETS_PATH.exists():
        for line in SECRETS_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

    # Secret'ları config'e gömme: config'te ${VAR_NAME} placeholder olabilir
    cfg = _expand_env(cfg)
    _config_cache = cfg
    return cfg


def _expand_env(obj: Any) -> Any:
    """Recursive ${VAR_NAME} expansion."""
    if isinstance(obj, str) and "${" in obj:
        import re
        def _sub(m):
            return os.environ.get(m.group(1), m.group(0))
        return re.sub(r"\$\{(\w+)\}", _sub, obj)
    if isinstance(obj, dict):
        return {k: _expand_env(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_expand_env(v) for v in obj]
    return obj


def secret(key: str, default: str = "") -> str:
    """Tek secret okuma (env var)."""
    load_config()  # secrets.env'i yükler (idempotent)
    return os.environ.get(key, default)


# ─── Subprocess utils ─────────────────────────────────────────────────────────

def run_proc(
    cmd: list[str], timeout: Optional[int] = None,
    capture: bool = True, **kwargs,
) -> subprocess.CompletedProcess:
    """Process-group leader olarak çalıştır (temiz kill için)."""
    return subprocess.run(
        cmd, capture_output=capture, timeout=timeout,
        start_new_session=True, **kwargs,
    )


def popen_proc(cmd: list[str], **kwargs) -> subprocess.Popen:
    """Long-running: stop için killpg gerekli."""
    return subprocess.Popen(
        cmd, start_new_session=True, **kwargs,
    )


def kill_proc_group(proc: subprocess.Popen, timeout: float = 5.0) -> bool:
    """SIGTERM → wait → SIGKILL."""
    if proc.poll() is not None:
        return True
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return True
    try:
        proc.wait(timeout=timeout)
        return True
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        return False


# ─── Signal handling ──────────────────────────────────────────────────────────

class GracefulShutdown:
    """SIGTERM/SIGINT yakalar, .stopped event'ini set eder."""

    def __init__(self):
        import threading
        self.stopped = threading.Event()
        signal.signal(signal.SIGTERM, self._handle)
        signal.signal(signal.SIGINT, self._handle)

    def _handle(self, signum, _frame):
        self.stopped.set()

    def wait(self, timeout: Optional[float] = None) -> bool:
        return self.stopped.wait(timeout)


# ─── Heartbeat helper ─────────────────────────────────────────────────────────

def start_heartbeat(service: str, interval: int = 10):
    """Background thread: her N sn DB'ye heartbeat yazar."""
    import threading
    from v5 import db as _db

    def _loop():
        while True:
            try:
                _db.heartbeat(service, "ok")
            except Exception as e:
                logging.warning(f"heartbeat fail: {e}")
            time.sleep(interval)

    t = threading.Thread(target=_loop, daemon=True, name=f"hb-{service}")
    t.start()
    return t


# ─── FFmpeg path discovery ────────────────────────────────────────────────────

def ffmpeg_path() -> str:
    """Önce config, sonra PATH'ten."""
    cfg = load_config()
    cand = cfg.get("ffmpeg_path", "") or "/usr/bin/ffmpeg"
    if Path(cand).exists():
        return cand
    import shutil
    return shutil.which("ffmpeg") or "ffmpeg"
