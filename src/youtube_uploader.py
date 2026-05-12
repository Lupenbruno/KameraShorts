"""YouTube Data API v3 upload — 6 video/day quota limit."""
import json
import logging
from datetime import date
from pathlib import Path

log = logging.getLogger("kamerashorts")


class YouTubeUploader:
    def __init__(self, config: dict):
        yt = config["youtube"]
        self.client_secret_path = yt["client_secret_path"]
        self.token_path = yt["token_path"]
        self.daily_limit = yt.get("daily_quota_limit", 6)
        self.playlist_id = yt.get("playlist_id")
        self.queue_path = config["paths"]["queue_path"]
        self.log_path = config["paths"]["log_path"]
        self.service = None

    def authenticate(self):
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from google.auth.transport.requests import Request
        import json

        scopes = [
            "https://www.googleapis.com/auth/youtube.upload",
            "https://www.googleapis.com/auth/youtube.readonly",
        ]
        creds = None

        if Path(self.token_path).exists():
            creds = Credentials.from_authorized_user_file(self.token_path, scopes)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(
                    self.client_secret_path, scopes
                )
                creds = flow.run_local_server(port=0)
            Path(self.token_path).write_text(creds.to_json())

        from googleapiclient.discovery import build
        self.service = build("youtube", "v3", credentials=creds)
        log.info("YouTube kimlik doğrulama basarili")

    def upload(self, video_path: str, metadata: dict) -> dict:
        if not self.check_quota():
            raise RuntimeError("Gunluk YouTube kotasi doldu (6 video)")

        # Eşzamanlı upload çakışmasını önle — rastgele 0-60 sn bekle
        import random as _random
        _delay = _random.randint(0, 60)
        if _delay > 0:
            import time as _t2; _t2.sleep(_delay)

        if self.service is None:
            self.authenticate()

        from googleapiclient.http import MediaFileUpload

        body = {
            "snippet": {
                "title": metadata["title"],
                "description": metadata["description"],
                "tags": metadata.get("tags", []),
                "categoryId": metadata.get("category_id", "22"),
                "defaultLanguage": "tr",
            },
            "status": {
                "privacyStatus": metadata.get("privacy_status", "public"),
                "selfDeclaredMadeForKids": False,
            },
        }

        media = MediaFileUpload(video_path, mimetype="video/mp4", resumable=True)
        request = self.service.videos().insert(part="snippet,status", body=body, media_body=media)

        response = None
        retries = 0
        while response is None:
            try:
                _, response = request.next_chunk()
            except Exception as e:
                retries += 1
                if retries > 5:
                    raise RuntimeError(f"YouTube upload 5 denemede basarisiz: {e}")
                log.warning(f"Upload chunk hatasi ({retries}/5): {e}, yeniden deneniyor...")
                import time as _t; _t.sleep(3)

        video_id = response["id"]
        url = f"https://youtube.com/watch?v={video_id}"
        self._log_upload(video_id, metadata["title"])
        log.info(f"YouTube'a yuklendi: {url}")

        # Telegram bildirimi
        try:
            from src.telegram_notifier import get_notifier
            notifier = get_notifier()
            if notifier:
                thumb = str(Path(video_path).with_suffix(".jpg"))
                city = metadata.get("city", "")
                notifier.notify_upload(city, metadata["title"], url, thumb)
        except Exception:
            pass

        # MediaFileUpload handle'ını kapat (Windows dosya kilidi için)
        try:
            media._fd.close()
        except Exception:
            pass

        # Çok dilli başlık/açıklama ekle
        localizations = metadata.get("localizations")
        if localizations:
            try:
                self._set_localizations(video_id, localizations)
                log.info(f"Lokalizasyonlar eklendi: {list(localizations.keys())}")
            except Exception as e:
                log.warning(f"Lokalizasyon eklenemedi: {e}")

        # Thumbnail yükle
        thumb_path = Path(video_path).with_suffix(".jpg")
        if thumb_path.exists():
            try:
                self._set_thumbnail(video_id, str(thumb_path))
                log.info(f"Thumbnail yuklendi: {thumb_path.name}")
            except Exception as e:
                log.warning(f"Thumbnail yuklenemedi: {e}")

        # Playlist'e ekle
        playlist_id = metadata.get("playlist_id") or self.playlist_id
        if playlist_id:
            try:
                self._add_to_playlist(video_id, playlist_id)
                log.info(f"Playlist'e eklendi: {playlist_id}")
            except Exception as e:
                log.warning(f"Playlist eklenemedi: {e}")

        # Yükleme başarılı — lokal dosyayı, thumbnail'i ve meta.json'ı sil
        import time as _time
        for attempt in range(5):
            try:
                p = Path(video_path)
                meta = p.with_suffix(".meta.json")
                thumb = p.with_suffix(".jpg")
                if p.exists():
                    p.unlink()
                    log.info(f"Lokal klip silindi: {p.name}")
                if meta.exists():
                    meta.unlink()
                if thumb.exists():
                    thumb.unlink()
                break
            except Exception as e:
                if attempt < 4:
                    _time.sleep(2)
                else:
                    log.warning(f"Lokal silme hatasi: {e}")

        return {"video_id": video_id, "url": url}

    def check_quota(self) -> bool:
        if not Path(self.log_path).exists():
            return True
        today = date.today().isoformat()
        count = sum(
            1 for line in open(self.log_path, encoding="utf-8", errors="replace")
            if today in line and "UPLOADED" in line
        )
        return count < self.daily_limit

    def add_to_queue(self, video_path: str, metadata: dict):
        Path(self.queue_path).parent.mkdir(parents=True, exist_ok=True)
        queue = []
        if Path(self.queue_path).exists():
            try:
                text = Path(self.queue_path).read_text(encoding="utf-8").strip()
                if text:
                    queue = json.loads(text)
            except Exception:
                queue = []
        queue.append({"video_path": video_path, "metadata": metadata})
        Path(self.queue_path).write_text(json.dumps(queue, indent=2, ensure_ascii=False))

    def upload_queue(self):
        if not Path(self.queue_path).exists():
            return
        import time as _time
        queue = json.loads(Path(self.queue_path).read_text())
        remaining = []
        uploaded = 0
        for item in queue:
            vpath = item.get("video_path", "")
            if not Path(vpath).exists():
                log.warning(f"Kuyruk: dosya bulunamadı, atlanıyor: {vpath}")
                continue
            if not self.check_quota():
                remaining.append(item)
                continue
            try:
                self.upload(vpath, item["metadata"])
                uploaded += 1
                # Videolar arası 3 dakika bekle — YouTube rate limit önlemi
                if uploaded < len([x for x in queue if Path(x.get("video_path","")).exists()]):
                    log.info(f"Sonraki upload için 3 dakika bekleniyor...")
                    _time.sleep(180)
            except Exception as e:
                log.error(f"Kuyruk upload hatası: {e}")
                remaining.append(item)
        Path(self.queue_path).write_text(json.dumps(remaining, indent=2, ensure_ascii=False))

    def _set_thumbnail(self, video_id: str, thumb_path: str):
        """Video için özel thumbnail yükle."""
        if self.service is None:
            self.authenticate()
        from googleapiclient.http import MediaFileUpload
        media = MediaFileUpload(thumb_path, mimetype="image/jpeg")
        self.service.thumbnails().set(
            videoId=video_id,
            media_body=media
        ).execute()
        try:
            media._fd.close()
        except Exception:
            pass

    def _add_to_playlist(self, video_id: str, playlist_id: str):
        """Videoyu belirtilen playlist'e ekle."""
        if self.service is None:
            self.authenticate()
        self.service.playlistItems().insert(
            part="snippet",
            body={
                "snippet": {
                    "playlistId": playlist_id,
                    "resourceId": {
                        "kind": "youtube#video",
                        "videoId": video_id,
                    }
                }
            }
        ).execute()

    def _set_localizations(self, video_id: str, localizations: dict):
        """YouTube lokalizasyon API'siyle video başlık/açıklamalarını dil bazında güncelle.

        localizations: {"tr": {"title": "...", "description": "..."}, "en": {...}, ...}
        """
        if self.service is None:
            self.authenticate()

        # YouTube API lokalizasyon formatı: {"tr": {"title": "...", "description": "..."}}
        loc_body = {}
        for lang, data in localizations.items():
            if lang == "tr":
                continue   # Default snippet zaten Türkçe
            loc_body[lang] = {
                "title": data.get("title", "")[:100],
                "description": data.get("description", "")[:5000],
            }

        if not loc_body:
            return

        body = {
            "id": video_id,
            "localizations": loc_body,
        }
        self.service.videos().update(
            part="localizations",
            body=body,
        ).execute()

    def _log_upload(self, video_id: str, title: str):
        Path(self.log_path).parent.mkdir(parents=True, exist_ok=True)
        with open(self.log_path, "a", encoding="utf-8") as f:
            from datetime import datetime
            f.write(f"{datetime.now().isoformat()} UPLOADED {video_id} | {title}\n")
