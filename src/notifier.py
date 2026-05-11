"""Telegram bildirimleri — her yükleme, hata ve sistem olayı için."""
import requests
import logging
import time
from datetime import datetime
from pathlib import Path

log = logging.getLogger("notifier")

_START_COOLDOWN = 3600   # saniye — aynı şehir için min. bu kadar bekle
_last_started: dict = {}  # city → epoch


class TelegramNotifier:
    def __init__(self, config: dict):
        tg = config.get("telegram", {})
        self.token   = tg.get("bot_token", "")
        self.chat_id = tg.get("chat_id", "")
        self.enabled = bool(self.token and self.chat_id)

    def _send(self, text: str):
        if not self.enabled:
            return
        try:
            url = f"https://api.telegram.org/bot{self.token}/sendMessage"
            requests.post(url, json={
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": False
            }, timeout=10)
        except Exception as e:
            log.warning(f"Telegram gonderim hatasi: {e}")

    def video_uploaded(self, camera_name: str, title: str, youtube_url: str, city: str):
        emoji = "🚌" if city == "ankara" else "🌉"
        now = datetime.now().strftime("%H:%M")
        self._send(
            f"{emoji} <b>Yeni video yüklendi!</b>\n"
            f"📍 {camera_name}\n"
            f"🎬 {title}\n"
            f"🕐 {now}\n"
            f"▶️ <a href='{youtube_url}'>YouTube'da izle</a>"
        )

    def slot_failed(self, city: str, slot_time: str, reason: str):
        emoji = "🚌" if city == "ankara" else "🌉"
        self._send(
            f"⚠️ <b>{emoji} {city.title()} — {slot_time} slotu başarısız</b>\n"
            f"Neden: {reason}"
        )

    def system_started(self, mode: str):
        key = mode.lower()
        now = time.time()
        if now - _last_started.get(key, 0) < _START_COOLDOWN:
            return  # 1 saat geçmeden tekrar gönderme
        _last_started[key] = now
        self._send(
            f"✅ <b>AsfaltTV başlatıldı</b>\n"
            f"Mod: {mode}\n"
            f"⏰ {datetime.now().strftime('%d/%m/%Y %H:%M')}"
        )

    def system_stopped(self):
        self._send(
            f"🔴 <b>AsfaltTV durduruldu</b>\n"
            f"⏰ {datetime.now().strftime('%d/%m/%Y %H:%M')}"
        )

    def quota_warning(self, city: str):
        self._send(
            f"🚫 <b>YouTube kota doldu — {city.title()}</b>\n"
            f"Bugünkü yükleme limiti aşıldı. Yarın devam edilecek."
        )
