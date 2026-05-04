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
        self.queue_path = config["paths"]["queue_path"]
        self.log_path = config["paths"]["log_path"]
        self.service = None

    def authenticate(self):
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from google.auth.transport.requests import Request
        import json

        scopes = ["https://www.googleapis.com/auth/youtube.upload"]
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

        if self.service is None:
            self.authenticate()

        from googleapiclient.http import MediaFileUpload

        body = {
            "snippet": {
                "title": metadata["title"],
                "description": metadata["description"],
                "tags": metadata.get("tags", []),
                "categoryId": metadata.get("category_id", "22"),
            },
            "status": {
                "privacyStatus": metadata.get("privacy_status", "public"),
                "selfDeclaredMadeForKids": False,
            },
        }

        media = MediaFileUpload(video_path, mimetype="video/mp4", resumable=True)
        request = self.service.videos().insert(part="snippet,status", body=body, media_body=media)

        response = None
        while response is None:
            _, response = request.next_chunk()

        video_id = response["id"]
        url = f"https://youtube.com/shorts/{video_id}"
        self._log_upload(video_id, metadata["title"])
        log.info(f"YouTube'a yuklendi: {url}")
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
            queue = json.loads(Path(self.queue_path).read_text())
        queue.append({"video_path": video_path, "metadata": metadata})
        Path(self.queue_path).write_text(json.dumps(queue, indent=2, ensure_ascii=False))

    def upload_queue(self):
        if not Path(self.queue_path).exists():
            return
        queue = json.loads(Path(self.queue_path).read_text())
        remaining = []
        for item in queue:
            if self.check_quota():
                self.upload(item["video_path"], item["metadata"])
            else:
                remaining.append(item)
        Path(self.queue_path).write_text(json.dumps(remaining, indent=2, ensure_ascii=False))

    def _log_upload(self, video_id: str, title: str):
        Path(self.log_path).parent.mkdir(parents=True, exist_ok=True)
        with open(self.log_path, "a", encoding="utf-8") as f:
            from datetime import datetime
            f.write(f"{datetime.now().isoformat()} UPLOADED {video_id} | {title}\n")
