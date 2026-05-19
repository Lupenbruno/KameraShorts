#!/usr/bin/env python3
"""
Gece yarısı çalışır.
1. Queue dışı kalan tüm MP4 dosyaları siler.
2. Disk %80 üzerindeyse queue'daki 2 günden eski dosyaları da siler.
"""
import json
import logging
import shutil
from datetime import datetime, timedelta
from pathlib import Path

LOG = Path('/opt/KameraShorts/logs/cleanup.log')
LOG.parent.mkdir(exist_ok=True)
logging.basicConfig(
    filename=str(LOG), level=logging.INFO,
    format='%(asctime)s %(message)s'
)

BASE = Path('/opt/KameraShorts')
QUEUE_FILES = [
    'data/queue/upload_queue.json',
    'data/queue/corum_upload_queue.json',
    'data/queue/istanbul_upload_queue.json',
    'data/queue/konya_upload_queue.json',
]

def get_queued_paths():
    queued = set()
    for qf in QUEUE_FILES:
        p = BASE / qf
        if not p.exists():
            continue
        try:
            for item in json.loads(p.read_text()):
                vp = item.get('video_path', '')
                queued.add(vp)
                queued.add(str(BASE / vp))
        except Exception:
            pass
    return queued

def disk_usage_pct():
    usage = shutil.disk_usage('/')
    return usage.used / usage.total * 100

def cleanup():
    queued = get_queued_paths()
    pct = disk_usage_pct()
    logging.info(f'Basladi — disk: %{pct:.1f}, queue: {len(queued)} dosya')

    deleted = 0
    freed = 0

    for f in Path(BASE / 'data').rglob('*.mp4'):
        in_queue = str(f) in queued or f.name in {Path(q).name for q in queued}
        age_days = (datetime.now() - datetime.fromtimestamp(f.stat().st_mtime)).days

        should_delete = False
        if not in_queue:
            should_delete = True          # Queue dışı → her zaman sil
        elif pct > 80 and age_days >= 2:
            should_delete = True          # Disk kritik + 2 günden eski queue dosyası
            logging.warning(f'Disk kritik, queue dosyası da siliniyor: {f.name}')

        if should_delete:
            try:
                size = f.stat().st_size
                f.unlink()
                f.with_suffix('.jpg').unlink(missing_ok=True)
                f.with_suffix('.meta.json').unlink(missing_ok=True)
                freed += size
                deleted += 1
            except Exception as e:
                logging.error(f'Silinemedi {f.name}: {e}')

    pct_after = disk_usage_pct()
    logging.info(
        f'Bitti — {deleted} dosya silindi, '
        f'{freed/1024/1024:.0f} MB kurtarildi, '
        f'disk: %{pct_after:.1f}'
    )

if __name__ == '__main__':
    cleanup()
