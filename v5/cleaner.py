#!/usr/bin/env python3
"""KameraShorts v5 — Cleaner.

Periyodik:
- DB'deki TTL'i gecen segment kayitlarini sil
- Dosya sisteminde DB'de olmayan .ts'leri sil (orphan cleanup)
- Eski log dosyalarini sil

systemd timer her 5dk cagrir (veya daemon mode ile sonsuz).
"""
import argparse
import os
import sys
import time
from pathlib import Path

from v5 import common, db

log = common.setup_logging("cleaner")


def cleanup_db_expired() -> int:
    """TTL'i gecen segment kayitlarini DB'den sil."""
    return db.cleanup_expired()


def cleanup_orphan_files(cities: list[str]) -> int:
    """DB'de olmayan disk dosyalarini sil."""
    removed = 0
    for city in cities:
        d = common.SEGMENTS_DIR / city
        if not d.exists():
            continue
        # DB'deki path'leri topla
        valid = set()
        for r in db.conn().execute(
            "SELECT path FROM segments WHERE city = ?", (city,),
        ):
            valid.add(r[0])

        for f in d.glob("*.ts"):
            if str(f) not in valid:
                try:
                    age = time.time() - f.stat().st_mtime
                    if age > 60:  # 1dk'dan eski, race-condition'a karsi
                        f.unlink(missing_ok=True)
                        removed += 1
                except Exception:
                    pass
    return removed


def cleanup_old_files(work_dir: Path, max_age_seconds: int = 3600) -> int:
    """work_dir altinda max_age'den eski tum dosyalari sil."""
    removed = 0
    if not work_dir.exists():
        return 0
    now = time.time()
    for f in work_dir.iterdir():
        try:
            if f.is_file() and (now - f.stat().st_mtime) > max_age_seconds:
                f.unlink(missing_ok=True)
                removed += 1
        except Exception:
            pass
    return removed


def run_once(cfg: dict) -> dict:
    cities = list(cfg.get("ingest", {}).get("cameras", {}).keys())
    stats = {}
    stats["db_expired"] = cleanup_db_expired()
    stats["orphans"] = cleanup_orphan_files(cities)
    stats["work"] = cleanup_old_files(common.WORK_DIR, max_age_seconds=600)
    log.info("temizlik: db=%d orphan=%d work=%d",
             stats["db_expired"], stats["orphans"], stats["work"])
    return stats


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--daemon", action="store_true")
    parser.add_argument("--interval", type=int, default=300)
    args = parser.parse_args()

    cfg = common.load_config()
    if args.daemon:
        shutdown = common.GracefulShutdown()
        common.start_heartbeat("cleaner", interval=30)
        while not shutdown.stopped.is_set():
            run_once(cfg)
            shutdown.wait(args.interval)
    else:
        run_once(cfg)


if __name__ == "__main__":
    main()
