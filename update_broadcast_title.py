#!/usr/bin/env python3
"""YouTube canlı yayın başlığını bugünün tarihiyle günceller.

Her gün çalışır (systemd timer 00:05) + stream başlangıcında.
Aktif broadcast'i bulur, başlığı "{tarih} | Türkiye Canlı..." yapar.

Kullanım:
    python update_broadcast_title.py
    python update_broadcast_title.py --title "özel başlık"
"""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

AYLAR = ["Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran",
         "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık"]
GUNLER = ["Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma",
          "Cumartesi", "Pazar"]

TOKEN_PATH = "/opt/KameraShorts/credentials/token.json"

# Başlık şablonu — {date} bugünün tarihi ile doldurulur (max 100 char)
TITLE_TEMPLATE = "🔴 {date} | Türkiye Canlı Şehir Kameraları - Sokak & Trafik 7/24"


def build_title() -> str:
    now = datetime.now()
    date_str = f"{now.day} {AYLAR[now.month - 1]} {now.year} {GUNLER[now.weekday()]}"
    return TITLE_TEMPLATE.format(date=date_str)[:100]


def update_title(custom_title: str = None) -> bool:
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    tok = json.load(open(TOKEN_PATH))
    creds = Credentials(
        token=tok.get("token"),
        refresh_token=tok.get("refresh_token"),
        token_uri=tok.get("token_uri", "https://oauth2.googleapis.com/token"),
        client_id=tok.get("client_id"),
        client_secret=tok.get("client_secret"),
        scopes=tok.get("scopes"),
    )
    yt = build("youtube", "v3", credentials=creds)

    # Aktif broadcast bul (yoksa upcoming)
    broadcast = None
    for status in ("active", "upcoming"):
        resp = yt.liveBroadcasts().list(
            part="snippet,status", broadcastStatus=status, maxResults=10,
        ).execute()
        items = resp.get("items", [])
        if items:
            broadcast = items[0]
            break

    if not broadcast:
        print("Aktif/upcoming broadcast bulunamadı", file=sys.stderr)
        return False

    bid = broadcast["id"]
    snippet = broadcast["snippet"]
    old_title = snippet.get("title", "")
    new_title = custom_title or build_title()

    if old_title == new_title:
        print(f"Başlık zaten güncel: {new_title}")
        return True

    # snippet'i koru, sadece title değiştir
    snippet["title"] = new_title
    # scheduledStartTime ISO formatında değilse update reddeder — koru
    yt.liveBroadcasts().update(
        part="snippet",
        body={"id": bid, "snippet": snippet},
    ).execute()
    print(f"✓ Başlık güncellendi:\n  Eski: {old_title}\n  Yeni: {new_title}")
    return True


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--title", help="Özel başlık (verilmezse tarih şablonu)")
    args = p.parse_args()
    try:
        ok = update_title(args.title)
        sys.exit(0 if ok else 1)
    except Exception as e:
        print(f"Hata: {e}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
