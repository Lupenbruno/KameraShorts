"""Mevcut YouTube videolarını doğru playlistlere ekler.

Kullanım:
  python batch_playlist.py --dry-run   # önce test et
  python batch_playlist.py             # gerçek ekle
"""
import argparse
import time
import yaml
from pathlib import Path

PLAYLIST_IDS = {
    "ankara":   "PLONbha2ewi1T8cReTGdqWHIVciq5j0biA",
    "istanbul": "PLONbha2ewi1QYupl2gA6v3Soz_jKbZk8w",
    "corum":    "PLONbha2ewi1SoNeZ-1pPXLMdHTnf7bkaL",
    "konya":    "PLONbha2ewi1Rofca5JotriW8yrSWy-3jE",
}

def build_service(cfg):
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build

    scopes = ["https://www.googleapis.com/auth/youtube.force-ssl"]
    token_path = cfg["youtube"]["token_path"]
    client_secret = cfg["youtube"]["client_secret_path"]
    creds = None

    if Path(token_path).exists():
        creds = Credentials.from_authorized_user_file(token_path, scopes)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(client_secret, scopes)
            creds = flow.run_local_server(port=0)
        Path(token_path).write_text(creds.to_json())

    return build("youtube", "v3", credentials=creds)


def get_all_videos(service):
    """Kanalın tüm videolarını çek."""
    ch = service.channels().list(part="contentDetails", mine=True).execute()
    uploads_id = ch["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]

    videos = []
    page_token = None
    while True:
        resp = service.playlistItems().list(
            part="snippet",
            playlistId=uploads_id,
            maxResults=50,
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


def detect_city(title: str, description: str) -> str | None:
    """Başlık ve açıklamaya bakarak şehri tespit et."""
    title_lower = title.lower()
    desc_lower = description.lower()

    # Ankara EGO Shorts
    if "ego" in desc_lower or ("ankara" in desc_lower and "#shorts" in title_lower):
        return "ankara"
    if "ankara" in desc_lower and "otobüs" in desc_lower:
        return "ankara"

    # Şehir kameraları
    if "çorum" in desc_lower or "corum" in desc_lower:
        return "corum"
    if "konya" in desc_lower or ("mevlana" in desc_lower or "alaeddin" in desc_lower
                                  or "serafettin" in desc_lower or "şerafettin" in desc_lower):
        return "konya"
    if "istanbul" in desc_lower or "i̇stanbul" in desc_lower or "ibb" in desc_lower:
        return "istanbul"

    # Başlıktan da dene
    if "çorum" in title_lower or "corum" in title_lower:
        return "corum"
    if "konya" in title_lower:
        return "konya"
    if "istanbul" in title_lower or "i̇stanbul" in title_lower:
        return "istanbul"
    if "ankara" in title_lower:
        return "ankara"

    return None


def get_existing_playlist_videos(service, playlist_id: str) -> set:
    """Playlistte zaten olan video ID'lerini çek."""
    existing = set()
    page_token = None
    while True:
        try:
            resp = service.playlistItems().list(
                part="snippet",
                playlistId=playlist_id,
                maxResults=50,
                pageToken=page_token,
            ).execute()
            for item in resp.get("items", []):
                existing.add(item["snippet"]["resourceId"]["videoId"])
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        except Exception:
            break
    return existing


def add_to_playlist(service, video_id: str, playlist_id: str):
    service.playlistItems().insert(
        part="snippet",
        body={
            "snippet": {
                "playlistId": playlist_id,
                "resourceId": {"kind": "youtube#video", "videoId": video_id},
            }
        }
    ).execute()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()

    cfg = yaml.safe_load(open(args.config, encoding="utf-8"))
    service = build_service(cfg)
    print("YouTube bağlantısı OK")

    videos = get_all_videos(service)
    print(f"{len(videos)} video bulundu\n")

    # Mevcut playlist içeriklerini çek (tekrar eklemeyi önle)
    print("Mevcut playlist içerikleri kontrol ediliyor...")
    existing = {city: get_existing_playlist_videos(service, pid)
                for city, pid in PLAYLIST_IDS.items()}

    stats = {"eklendi": 0, "zaten_var": 0, "tespit_edilemedi": 0}

    for v in videos:
        city = detect_city(v["title"], v["description"])
        vid_id = v["video_id"]
        title = v["title"][:60].encode("ascii", errors="replace").decode("ascii")

        if city is None:
            print(f"  [?] [{vid_id}] Sehir tespit edilemedi: {title}")
            stats["tespit_edilemedi"] += 1
            continue

        playlist_id = PLAYLIST_IDS[city]

        if vid_id in existing[city]:
            print(f"  [OK] [{city.upper()}] Zaten var: {title}")
            stats["zaten_var"] += 1
            continue

        print(f"  [+] [{city.upper()}] Ekleniyor: {title}")
        if not args.dry_run:
            try:
                add_to_playlist(service, vid_id, playlist_id)
                existing[city].add(vid_id)
                stats["eklendi"] += 1
                time.sleep(0.3)
            except Exception as e:
                print(f"     [HATA] {e}")
        else:
            stats["eklendi"] += 1

    print(f"\n{'[DRY-RUN] ' if args.dry_run else ''}Tamamlandı:")
    print(f"  Eklendi:            {stats['eklendi']}")
    print(f"  Zaten vardı:        {stats['zaten_var']}")
    print(f"  Tespit edilemedi:   {stats['tespit_edilemedi']}")


if __name__ == "__main__":
    main()
