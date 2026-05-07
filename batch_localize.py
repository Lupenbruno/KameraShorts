"""Mevcut YouTube videolarına sonradan çok dilli başlık/açıklama ekler.

Kullanım:
  python batch_localize.py --city ankara --limit 50
  python batch_localize.py --city corum --limit 20
  python batch_localize.py --all

Nasıl çalışır:
  1. Kanalın son N videosunu çeker (videos().list → playlistItems)
  2. Her videonun mevcut snippet'ine bakarak dili tahmin eder (tr başlık olanlara)
  3. Türkçe snippet'i kullanarak diğer dillere çeviri üretir
  4. videos().update ile lokalizasyonları yazar
"""
import argparse
import time
import yaml
import re
from datetime import datetime
from pathlib import Path


def build_service(token_path: str, client_secret_path: str):
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build

    scopes = ["https://www.googleapis.com/auth/youtube.force-ssl"]
    creds = None

    if Path(token_path).exists():
        creds = Credentials.from_authorized_user_file(token_path, scopes)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(client_secret_path, scopes)
            creds = flow.run_local_server(port=0)
        Path(token_path).write_text(creds.to_json())

    return build("youtube", "v3", credentials=creds)


def get_channel_videos(service, max_results: int = 50) -> list[dict]:
    """Kanalın yüklenen videolarını çek (yeniden eskiye)."""
    # Önce kanalın uploads playlist ID'sini bul
    ch = service.channels().list(part="contentDetails", mine=True).execute()
    uploads_id = ch["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]

    videos = []
    page_token = None
    while len(videos) < max_results:
        resp = service.playlistItems().list(
            part="snippet",
            playlistId=uploads_id,
            maxResults=min(50, max_results - len(videos)),
            pageToken=page_token,
        ).execute()

        for item in resp.get("items", []):
            sn = item["snippet"]
            videos.append({
                "video_id": sn["resourceId"]["videoId"],
                "title": sn["title"],
                "description": sn["description"],
            })

        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    return videos


def guess_localizations_for_video(video_id: str, title: str, description: str) -> dict | None:
    """
    Mevcut Türkçe başlığı parse ederek 6 dilde lokalizasyon üretmeye çalış.
    Tanınamıyorsa None döndür.
    """
    from src.multilingual_titles import ankara_localizations, city_localizations
    now = datetime.now()

    # Ankara EGO kameraları: "#Shorts" içeriyor ve saat pattern var
    if "#Shorts" in title or "Ankara" in description and "EGO" in description:
        # Location: description'ın 2. satırından çek (📍 ...)
        loc_match = re.search(r"📍\s*(.+)", description)
        location = loc_match.group(1).strip() if loc_match else "Ankara"
        return ankara_localizations(location, now)

    # Şehir kameraları: description'da şehir adını bul
    for city_key, city_names_key in [("corum", "Çorum"), ("konya", "Konya"),
                                      ("istanbul", "İstanbul")]:
        if city_names_key in description or city_names_key.lower() in title.lower():
            loc_match = re.search(r"📍\s*(.+)", description)
            location = loc_match.group(1).strip() if loc_match else city_names_key
            cam_match = re.search(r"🎥\s*(.+?)\s*—", description)
            camera_name = cam_match.group(1).strip() if cam_match else title.split("|")[0].strip()
            return city_localizations(camera_name, location, city_names_key, now)

    return None


def set_localizations(service, video_id: str, localizations: dict) -> bool:
    """API çağrısı — lokalizasyonları yaz."""
    loc_body = {}
    for lang, data in localizations.items():
        if lang == "tr":
            continue   # Default snippet zaten Türkçe
        loc_body[lang] = {
            "title": data.get("title", "")[:100],
            "description": data.get("description", "")[:5000],
        }

    if not loc_body:
        return False

    service.videos().update(
        part="localizations",
        body={"id": video_id, "localizations": loc_body},
    ).execute()
    return True


def main():
    parser = argparse.ArgumentParser(description="Mevcut videolara çok dilli başlık ekle")
    parser.add_argument("--city", default="ankara", help="ankara / corum / konya / all")
    parser.add_argument("--limit", type=int, default=50, help="Kaç video işlensin")
    parser.add_argument("--dry-run", action="store_true", help="API çağrısı yapma, sadece göster")
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()

    with open(args.config, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    # Token/secret yolunu belirle (önce global, yoksa şehir config'inden)
    if args.city == "ankara" or args.city == "all":
        token_path = cfg["youtube"]["token_path"]
        client_secret = cfg["youtube"]["client_secret_path"]
    else:
        city_cfg = cfg["cities"][args.city]
        token_path = city_cfg["youtube"]["token_path"]
        client_secret = city_cfg["youtube"]["client_secret_path"]

    service = build_service(token_path, client_secret)
    print(f"✅ YouTube kimlik doğrulama başarılı")

    videos = get_channel_videos(service, max_results=args.limit)
    print(f"📋 {len(videos)} video çekildi")

    ok, skip, fail = 0, 0, 0
    for v in videos:
        vid_id = v["video_id"]
        title  = v["title"]

        loc = guess_localizations_for_video(vid_id, title, v["description"])
        if loc is None:
            print(f"  ⏭  [{vid_id}] Tanınamadı: {title[:60]}")
            skip += 1
            continue

        print(f"  🌍 [{vid_id}] {title[:55]}...")
        if not args.dry_run:
            try:
                set_localizations(service, vid_id, loc)
                ok += 1
                time.sleep(0.5)   # Kota koruması
            except Exception as e:
                print(f"     ❌ Hata: {e}")
                fail += 1
        else:
            langs = [l for l in loc if l != "tr"]
            print(f"     [DRY] Diller: {langs}")
            ok += 1

    print(f"\n✅ Tamamlandı — {ok} eklendi / {skip} atlandı / {fail} hata")


if __name__ == "__main__":
    main()
