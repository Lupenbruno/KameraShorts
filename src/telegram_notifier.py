"""Telegram bot — KameraShorts telefon kontrol paneli.

Komutlar:
  /durum   — Pipeline durumu + bugünkü yükleme sayısı
  /bugun   — Bugün yüklenen videolar (şehre göre)
  /son     — Son 5 yükleme (YouTube linkleriyle)
  /yardim  — Komut listesi

Otomatik bildirimler:
  ✅ Her başarılı YouTube yüklemesinde (thumbnail + link)
  ⚠️ Pipeline durduğunda (3 slot boyunca yükleme yoksa)
"""
import logging
import threading
import time
from datetime import date, datetime
from pathlib import Path

import requests

log = logging.getLogger("kamerashorts")

# Singleton — dashboard.py'de init edilir, uploader tarafından kullanılır
_notifier = None


def get_notifier() -> "TelegramNotifier | None":
    return _notifier


def init_notifier(token: str, chat_id: str, log_paths: dict = None,
                  pipelines_ref: dict = None) -> "TelegramNotifier":
    global _notifier
    _notifier = TelegramNotifier(token, chat_id, log_paths, pipelines_ref)
    return _notifier


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str,
                 log_paths: dict = None, pipelines_ref: dict = None):
        self.token = token
        self.chat_id = str(chat_id)
        self.log_paths = log_paths or {}   # {"ankara": Path(...), "istanbul": Path(...)}
        self.pipelines_ref = pipelines_ref or {}
        self.base = f"https://api.telegram.org/bot{token}"
        self._offset = 0
        self._running = False

    # ------------------------------------------------------------------ #
    #  Mesaj gönderme                                                      #
    # ------------------------------------------------------------------ #

    def send(self, text: str, parse_mode: str = "HTML") -> bool:
        try:
            r = requests.post(
                f"{self.base}/sendMessage",
                json={"chat_id": self.chat_id, "text": text,
                      "parse_mode": parse_mode, "disable_web_page_preview": True},
                timeout=10,
            )
            return r.ok
        except Exception as e:
            log.debug(f"Telegram gönderim hatası: {e}")
            return False

    def send_photo(self, photo_path: str, caption: str = "") -> bool:
        try:
            with open(photo_path, "rb") as f:
                r = requests.post(
                    f"{self.base}/sendPhoto",
                    data={"chat_id": self.chat_id, "caption": caption,
                          "parse_mode": "HTML"},
                    files={"photo": f},
                    timeout=20,
                )
            return r.ok
        except Exception as e:
            log.debug(f"Telegram foto hatası: {e}")
            return False

    # ------------------------------------------------------------------ #
    #  Bildirimler (pipeline'dan çağrılır)                                 #
    # ------------------------------------------------------------------ #

    def notify_upload(self, city: str, title: str, url: str,
                      thumb_path: str = None):
        """Başarılı YouTube yüklemesi bildirimi."""
        caption = (
            f"✅ <b>{city}</b>\n"
            f"{title}\n"
            f'<a href="{url}">▶️ YouTube\'da izle</a>'
        )
        if thumb_path and Path(thumb_path).exists():
            ok = self.send_photo(thumb_path, caption)
            if not ok:
                self.send(caption)
        else:
            self.send(caption)

    def notify_error(self, message: str):
        self.send(f"⚠️ <b>HATA</b>\n{message}")

    def notify_start(self):
        self.send(
            f"🚀 <b>KameraShorts başlatıldı</b>\n"
            f"🕐 {datetime.now().strftime('%d.%m.%Y %H:%M')}"
        )

    # ------------------------------------------------------------------ #
    #  Komut işleyiciler                                                   #
    # ------------------------------------------------------------------ #

    def _cmd_yardim(self):
        self.send(
            "🎥 <b>KameraShorts Bot</b>\n\n"
            "/durum — Pipeline durumu\n"
            "/bugun — Bugünkü yüklemeler\n"
            "/son   — Son 5 yükleme\n"
            "/yardim — Bu menü"
        )

    def _cmd_durum(self):
        now = datetime.now().strftime("%d.%m.%Y %H:%M")
        msg = f"🖥️ <b>Sistem Durumu</b> | {now}\n\n"

        # Pipeline durumları
        icons = {"ankara": "🚌", "istanbul": "🌉", "corum": "🏛️", "konya": "🕌"}
        for key, proc in self.pipelines_ref.items():
            alive = proc is not None and proc.poll() is None
            icon = icons.get(key, "📍")
            status = "🟢 çalışıyor" if alive else "🔴 durdu"
            msg += f"{icon} <b>{key.capitalize()}</b>: {status}\n"

        # Bugünkü toplam yükleme
        total = self._count_today_all()
        msg += f"\n📊 Bugün toplam: <b>{total}</b> video"
        self.send(msg)

    def _cmd_bugun(self):
        today = date.today().strftime("%d.%m.%Y")
        msg = f"📊 <b>Bugün ({today})</b>\n\n"
        grand = 0
        icons = {"ankara": "🚌", "istanbul": "🌉", "corum": "🏛️", "konya": "🕌"}
        for city, log_path in self.log_paths.items():
            count = self._count_today(log_path)
            grand += count
            icon = icons.get(city, "📍")
            msg += f"{icon} {city.capitalize()}: <b>{count}</b> video\n"
        msg += f"\n🔢 Toplam: <b>{grand}</b>"
        self.send(msg)

    def _cmd_son(self):
        # Tüm log dosyalarından UPLOADED satırlarını topla, zaman sırasıyla
        all_lines = []
        for city, log_path in self.log_paths.items():
            if not log_path or not Path(log_path).exists():
                continue
            for line in Path(log_path).read_text(
                    encoding="utf-8", errors="replace").splitlines():
                if "UPLOADED" in line:
                    all_lines.append((line, city))

        if not all_lines:
            self.send("📋 Henüz yükleme yok")
            return

        last5 = all_lines[-5:]
        msg = "📋 <b>Son Yüklemeler</b>\n\n"
        for line, city in reversed(last5):
            try:
                # Format: "2026-05-10T12:00:00 UPLOADED <video_id> | <title>"
                parts = line.split("UPLOADED", 1)
                rest = parts[1].strip()
                vid_id, title = rest.split("|", 1)
                vid_id = vid_id.strip()
                title = title.strip()[:45]
                url = f"https://youtube.com/watch?v={vid_id}"
                msg += f"• {title}\n  <a href=\"{url}\">izle</a> — {city.capitalize()}\n\n"
            except Exception:
                continue
        self.send(msg)

    # ------------------------------------------------------------------ #
    #  Yardımcı sayaçlar                                                   #
    # ------------------------------------------------------------------ #

    def _count_today(self, log_path) -> int:
        if not log_path or not Path(log_path).exists():
            return 0
        today = date.today().isoformat()
        return sum(
            1 for l in Path(log_path).read_text(
                encoding="utf-8", errors="replace").splitlines()
            if today in l and "UPLOADED" in l
        )

    def _count_today_all(self) -> int:
        return sum(self._count_today(p) for p in self.log_paths.values())

    # ------------------------------------------------------------------ #
    #  Polling döngüsü                                                     #
    # ------------------------------------------------------------------ #

    def _handle_update(self, update: dict):
        msg = update.get("message", {})
        text = msg.get("text", "").strip()
        chat_id = str(msg.get("chat", {}).get("id", ""))

        # Sadece yetkili chat
        if chat_id != self.chat_id:
            return

        cmd = text.split()[0].lower() if text else ""
        if cmd in ("/start", "/yardim", "/help"):
            self._cmd_yardim()
        elif cmd in ("/durum", "/status"):
            self._cmd_durum()
        elif cmd in ("/bugun", "/today", "/stats"):
            self._cmd_bugun()
        elif cmd in ("/son", "/last", "/recent"):
            self._cmd_son()

    def _poll_loop(self):
        log.info("Telegram polling başladı")
        while self._running:
            try:
                r = requests.get(
                    f"{self.base}/getUpdates",
                    params={"offset": self._offset, "timeout": 25},
                    timeout=30,
                )
                if r.ok:
                    for update in r.json().get("result", []):
                        self._offset = update["update_id"] + 1
                        try:
                            self._handle_update(update)
                        except Exception as e:
                            log.debug(f"Update işleme hatası: {e}")
            except requests.Timeout:
                pass  # Normal — long poll timeout
            except Exception as e:
                log.debug(f"Telegram poll hatası: {e}")
                time.sleep(5)

    def start(self):
        self._running = True
        t = threading.Thread(
            target=self._poll_loop, daemon=True, name="telegram-bot"
        )
        t.start()

    def stop(self):
        self._running = False
