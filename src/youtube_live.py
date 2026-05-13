"""YouTube Live Streaming API — broadcast olustur, superchat dinle."""
import logging
import time
from pathlib import Path

log = logging.getLogger("kamerashorts")

SCOPES = ["https://www.googleapis.com/auth/youtube"]


class YouTubeLive:
    def __init__(self, config: dict):
        yt = config["youtube"]
        self.client_secret = yt["client_secret_path"]
        self.token_path    = yt["token_path"]
        self.service       = None

    def _auth(self):
        if self.service:
            return
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build

        creds = None
        if Path(self.token_path).exists():
            creds = Credentials.from_authorized_user_file(self.token_path, SCOPES)
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            Path(self.token_path).write_text(creds.to_json())
        self.service = build("youtube", "v3", credentials=creds)
        log.info("YouTube Live kimlik dogrulama basarili")

    # ------------------------------------------------------------------
    def create_or_resume(self, title: str, description: str) -> dict:
        """Aktif broadcast varsa onu kullan, yoksa yeni olustur.
        Returns: {broadcast_id, stream_id, rtmp_url, chat_id}
        """
        self._auth()

        # Aktif/hazir broadcast var mi kontrol et
        resp = self.service.liveBroadcasts().list(
            part="id,snippet,contentDetails,status",
            broadcastStatus="active",
            maxResults=5
        ).execute()

        for item in resp.get("items", []):
            if "KameraShorts" in item["snippet"].get("title", ""):
                bid = item["id"]
                chat_id = item["snippet"].get("liveChatId", "")
                log.info(f"Aktif broadcast bulundu: {bid}")
                stream_info = self._get_stream_rtmp(bid)
                return {"broadcast_id": bid, "chat_id": chat_id, **stream_info}

        # Yeni broadcast olustur
        return self._create_new(title, description)

    def _create_new(self, title: str, description: str) -> dict:
        self._auth()

        # 1. LiveStream olustur (RTMP endpoint)
        stream_resp = self.service.liveStreams().insert(
            part="snippet,cdn,contentDetails",
            body={
                "snippet": {"title": title},
                "cdn": {
                    "frameRate": "30fps",
                    "ingestionType": "rtmp",
                    "resolution": "1080p",
                },
                "contentDetails": {"isReusable": True},
            }
        ).execute()

        stream_id  = stream_resp["id"]
        rtmp_url   = stream_resp["cdn"]["ingestionInfo"]["ingestionAddress"]
        stream_key = stream_resp["cdn"]["ingestionInfo"]["streamName"]
        full_rtmp  = f"{rtmp_url}/{stream_key}"
        log.info(f"LiveStream olusturuldu: {stream_id}")

        # 2. LiveBroadcast olustur
        from datetime import datetime, timezone, timedelta
        start = (datetime.now(timezone.utc) + timedelta(seconds=30)).strftime("%Y-%m-%dT%H:%M:%S.000Z")

        broadcast_resp = self.service.liveBroadcasts().insert(
            part="snippet,status,contentDetails",
            body={
                "snippet": {
                    "title": title,
                    "description": description,
                    "scheduledStartTime": start,
                },
                "status": {
                    "privacyStatus": "public",
                    "selfDeclaredMadeForKids": False,
                },
                "contentDetails": {
                    "enableAutoStart": True,
                    "enableAutoStop": False,
                    "enableDvr": True,
                    "latencyPreference": "ultraLow",
                },
            }
        ).execute()

        broadcast_id = broadcast_resp["id"]
        chat_id      = broadcast_resp["snippet"].get("liveChatId", "")
        log.info(f"LiveBroadcast olusturuldu: {broadcast_id}")

        # 3. Bind
        self.service.liveBroadcasts().bind(
            part="id,contentDetails",
            id=broadcast_id,
            streamId=stream_id,
        ).execute()
        log.info("Stream broadcast'e baglandi")

        return {
            "broadcast_id": broadcast_id,
            "stream_id":    stream_id,
            "rtmp_url":     full_rtmp,
            "chat_id":      chat_id,
        }

    def _get_stream_rtmp(self, broadcast_id: str) -> dict:
        """Mevcut broadcast'in stream'ini bul, RTMP URL dondur."""
        self._auth()
        resp = self.service.liveBroadcasts().list(
            part="contentDetails", id=broadcast_id
        ).execute()
        items = resp.get("items", [])
        if not items:
            return {"stream_id": "", "rtmp_url": ""}
        stream_id = items[0]["contentDetails"].get("boundStreamId", "")
        if not stream_id:
            return {"stream_id": "", "rtmp_url": ""}
        sr = self.service.liveStreams().list(
            part="cdn", id=stream_id
        ).execute()
        info = sr["items"][0]["cdn"]["ingestionInfo"]
        full_rtmp = f"{info['ingestionAddress']}/{info['streamName']}"
        return {"stream_id": stream_id, "rtmp_url": full_rtmp}

    def go_live(self, broadcast_id: str):
        """Broadcast'i testing -> live gecir."""
        self._auth()
        try:
            self.service.liveBroadcasts().transition(
                broadcastStatus="testing", id=broadcast_id, part="status"
            ).execute()
            log.info("Broadcast testing moduna gecti, stream bekleniyor...")
            time.sleep(10)
        except Exception as e:
            log.warning(f"Testing gecisi: {e}")
        try:
            self.service.liveBroadcasts().transition(
                broadcastStatus="live", id=broadcast_id, part="status"
            ).execute()
            log.info("Broadcast CANLI!")
        except Exception as e:
            log.warning(f"Live gecisi: {e} (autoStart aktifse normal)")

    def end_broadcast(self, broadcast_id: str):
        self._auth()
        try:
            self.service.liveBroadcasts().transition(
                broadcastStatus="complete", id=broadcast_id, part="status"
            ).execute()
            log.info("Broadcast sonlandirildi")
        except Exception as e:
            log.warning(f"Broadcast sonlandirma: {e}")

    # ------------------------------------------------------------------
    def poll_superchat(self, chat_id: str, page_token: str = None) -> tuple[list, str]:
        """Live Chat'i sorgula, superchat + mesajlari dondur.
        Returns: (mesajlar_listesi, sonraki_page_token)
        """
        if not chat_id:
            return [], page_token
        self._auth()
        try:
            params = {
                "part": "snippet,authorDetails",
                "liveChatId": chat_id,
                "maxResults": 200,
            }
            if page_token:
                params["pageToken"] = page_token

            resp = self.service.liveChatMessages().list(**params).execute()
            messages = []
            for item in resp.get("items", []):
                snippet = item.get("snippet", {})
                msg_type = snippet.get("type", "")
                text = ""
                if msg_type == "superChatEvent":
                    text = snippet.get("superChatDetails", {}).get("userComment", "")
                elif msg_type == "textMessageEvent":
                    text = snippet.get("displayMessage", "")
                if text:
                    messages.append({
                        "type":   msg_type,
                        "text":   text,
                        "author": item.get("authorDetails", {}).get("displayName", ""),
                    })
            return messages, resp.get("nextPageToken", page_token)
        except Exception as e:
            log.debug(f"Chat poll hatasi: {e}")
            return [], page_token
