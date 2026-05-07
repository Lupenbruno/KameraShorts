"""Diskte bekleyen klipleri direkt YouTube'a yükle."""
import sys, io, yaml
from pathlib import Path
from datetime import datetime
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

cfg = yaml.safe_load(open('config.yaml', encoding='utf-8'))
from src.youtube_uploader import YouTubeUploader
from src.multilingual_titles import city_localizations

now = datetime(2026, 5, 7, 6, 0)

to_upload = [
    {
        'path': 'data/corum_clips/corum_aksemseddin_20260507_0600.mp4',
        'camera': 'Aksemseddin Camii', 'location': 'Aksemseddin Camii, Corum',
        'city_key': 'Corum', 'playlist': 'PLONbha2ewi1SoNeZ-1pPXLMdHTnf7bkaL',
        'log': 'logs/corum_pipeline.log', 'queue': 'data/queue/corum_upload_queue.json',
    },
    {
        'path': 'data/corum_clips/corum_gazicaddesi_20260506_1632.mp4',
        'camera': 'Gazi Caddesi', 'location': 'Gazi Caddesi, Corum',
        'city_key': 'Corum', 'playlist': 'PLONbha2ewi1SoNeZ-1pPXLMdHTnf7bkaL',
        'log': 'logs/corum_pipeline.log', 'queue': 'data/queue/corum_upload_queue.json',
    },
    {
        'path': 'data/konya_clips/konya_serafettincamii_20260507_0600.mp4',
        'camera': 'Serafettin Camii', 'location': 'Serafettin Camii, Konya',
        'city_key': 'Konya', 'playlist': 'PLONbha2ewi1Rofca5JotriW8yrSWy-3jE',
        'log': 'logs/konya_pipeline.log', 'queue': 'data/queue/konya_upload_queue.json',
    },
    {
        'path': 'data/konya_clips/konya_serafettincamii_20260506_1928.mp4',
        'camera': 'Serafettin Camii', 'location': 'Serafettin Camii, Konya',
        'city_key': 'Konya', 'playlist': 'PLONbha2ewi1Rofca5JotriW8yrSWy-3jE',
        'log': 'logs/konya_pipeline.log', 'queue': 'data/queue/konya_upload_queue.json',
    },
    {
        'path': 'data/istanbul_clips/sultanahmet1_20260507_0600.mp4',
        'camera': 'Sultanahmet 1', 'location': 'Sultanahmet, Istanbul',
        'city_key': 'Istanbul', 'playlist': 'PLONbha2ewi1QYupl2gA6v3Soz_jKbZk8w',
        'log': 'logs/istanbul_pipeline.log', 'queue': 'data/queue/istanbul_upload_queue.json',
    },
]

for item in to_upload:
    p = Path(item['path'])
    if not p.exists():
        print(f'Dosya yok, atlaniyor: {p.name}')
        continue

    uploader_cfg = dict(cfg)
    uploader_cfg['youtube'] = cfg['istanbul_youtube']
    uploader_cfg['paths'] = {'log_path': item['log'], 'queue_path': item['queue']}
    u = YouTubeUploader(uploader_cfg)
    u.daily_limit = 999

    loc = city_localizations(item['camera'], item['location'], item['city_key'], now)
    saat = now.strftime('%H:%M')
    tarih = now.strftime('%d.%m.%Y')
    metadata = {
        'title': f"{item['camera']} Canli Kamera | {saat} | {tarih}",
        'description': f"Canli kamera - {item['location']}",
        'tags': [item['city_key'].lower(), 'canli kamera', 'turkey'],
        'category_id': '19',
        'privacy_status': 'public',
        'localizations': loc,
        'playlist_id': item['playlist'],
    }

    print(f"Yukleniyor: {item['camera']} ({p.name})...")
    try:
        result = u.upload(str(p), metadata)
        print(f"OK: {result['url']}")
    except Exception as e:
        print(f"HATA: {e}")
