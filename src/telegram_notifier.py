"""Telegram bot — KameraShorts interaktif kontrol paneli.

Komutlar:
  /durum    — Pipeline durumu + bugünkü yükleme sayısı
  /baslat   — Pipeline başlatma menüsü
  /durdur   — Pipeline durdurma menüsü
  /yeniden  — Tüm pipeline'ları yeniden başlat
  /bugun    — Bugün yüklenen videolar (şehre göre)
  /son      — Son 5 yükleme (YouTube linkleriyle)
  /yardim   — Komut listesi

Otomatik bildirimler:
  ✅ Her başarılı YouTube yüklemesinde (thumbnail + link)
"""
import logging
import threading
import time
from datetime import date, datetime
from pathlib import Path

import requests

log = logging.getLogger("kamerashorts")

_notifier = None

CITIES = ["ankara", "istanbul", "corum", "konya"]
CITY_LABEL = {"ankara": "Ankara", "istanbul": "İstanbul",
               "corum": "Çorum", "konya": "Konya"}
CITY_ICON  = {"ankara": "🚌", "istanbul": "🌉",
               "corum": "🏛️", "konya": "🕌"}


def get_notifier() -> "TelegramNotifier | None":
    return _notifier


def init_notifier(token: str, chat_id: str,
                  log_paths: dict = None,
                  pipelines_ref: dict = None,
                  start_fn=None,
                  stop_fn=None) -> "TelegramNotifier":
    global _notifier
    _notifier = TelegramNotifier(token, chat_id, log_paths,
                                  pipelines_ref, start_fn, stop_fn)
    return _notifier


class TelegramNotifier:
    def __init__(self, token, chat_id,
                 log_paths=None, pipelines_ref=None,
                 start_fn=None, stop_fn=None):
        self.token = token
        self.chat_id = str(chat_id)
        self.log_paths = log_paths or {}
        self.pipelines_ref = pipelines_ref or {}
        self.start_fn = start_fn   # callable(key: str)
        self.stop_fn  = stop_fn    # callable(key: str)
        self.base = f"https://api.telegram.org/bot{token}"
        self._offset = 0
        self._running = False

    # ------------------------------------------------------------------ #
    #  Temel gönderim                                                      #
    # ------------------------------------------------------------------ #

    def send(self, text: str, parse_mode: str = "HTML",
             keyboard=None, message_id: int = None) -> dict:
        """Mesaj gönder. keyboard varsa inline keyboard ekle."""
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }
        if keyboard:
            payload["reply_markup"] = {"inline_keyboard": keyboard}
        try:
            r = requests.post(f"{self.base}/sendMessage", json=payload, timeout=10)
            return r.json().get("result", {})
        except Exception as e:
            log.debug(f"Telegram gönderim hatası: {e}")
            return {}

    def edit(self, message_id: int, text: str, keyboard=None):
        """Mevcut mesajı güncelle (inline button sonrası)."""
        payload = {
            "chat_id": self.chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if keyboard:
            payload["reply_markup"] = {"inline_keyboard": keyboard}
        try:
            requests.post(f"{self.base}/editMessageText", json=payload, timeout=10)
        except Exception:
            pass

    def answer_callback(self, callback_id: str, text: str = ""):
        try:
            requests.post(f"{self.base}/answerCallbackQuery",
                          json={"callback_query_id": callback_id, "text": text},
                          timeout=5)
        except Exception:
            pass

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
    #  Bildirimler                                                         #
    # ------------------------------------------------------------------ #

    def notify_upload(self, city: str, title: str, url: str,
                      thumb_path: str = None):
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
        # 1 saat içinde tekrar gönderme — dosyaya yazarak restart'tan sonra da hatırla
        _flag = Path("data/.last_start_notify")
        try:
            if _flag.exists():
                if time.time() - _flag.stat().st_mtime < 3600:
                    return
            _flag.parent.mkdir(parents=True, exist_ok=True)
            _flag.touch()
        except Exception:
            pass
        self.send(
            f"🚀 <b>KameraShorts başlatıldı</b>\n"
            f"🕐 {datetime.now().strftime('%d.%m.%Y %H:%M')}\n\n"
            f"Komutlar için /yardim"
        )

    # ------------------------------------------------------------------ #
    #  Durum metni                                                         #
    # ------------------------------------------------------------------ #

    def _status_text(self) -> str:
        now = datetime.now().strftime("%d.%m.%Y %H:%M")
        msg = f"🖥️ <b>Sistem Durumu</b> | {now}\n\n"
        for key in CITIES:
            proc = self.pipelines_ref.get(key)
            alive = proc is not None and proc.poll() is None
            icon = CITY_ICON.get(key, "📍")
            status = "🟢 çalışıyor" if alive else "🔴 durdu"
            count = self._count_today(self.log_paths.get(key))
            msg += f"{icon} <b>{CITY_LABEL[key]}</b>: {status} ({count} video)\n"
        total = self._count_today_all()
        msg += f"\n📊 Bugün toplam: <b>{total}</b> video"
        return msg

    def _status_keyboard(self):
        return [
            [{"text": "🔄 Yenile", "callback_data": "refresh_status"}],
            [{"text": "▶️ Hepsini Başlat", "callback_data": "start_all"},
             {"text": "⏹️ Hepsini Durdur", "callback_data": "stop_all"}],
        ]

    # ------------------------------------------------------------------ #
    #  Komut işleyiciler                                                   #
    # ------------------------------------------------------------------ #

    def _cmd_yardim(self):
        self.send(
            "🎥 <b>KameraShorts Kontrol</b>\n\n"
            "/durum   — Sistem durumu\n"
            "/baslat  — Pipeline başlat\n"
            "/durdur  — Pipeline durdur\n"
            "/yeniden — Tümünü yeniden başlat\n"
            "/bugun   — Bugünkü yüklemeler\n"
            "/son     — Son 5 yükleme\n"
            "/yardim  — Bu menü"
        )

    def _cmd_durum(self):
        self.send(self._status_text(), keyboard=self._status_keyboard())

    def _cmd_baslat(self):
        rows = []
        for key in CITIES:
            proc = self.pipelines_ref.get(key)
            alive = proc is not None and proc.poll() is None
            label = f"{'🟢' if alive else '⚫'} {CITY_ICON[key]} {CITY_LABEL[key]}"
            rows.append([{"text": label, "callback_data": f"start_{key}"}])
        rows.append([{"text": "▶️ Hepsini Başlat", "callback_data": "start_all"}])
        self.send("▶️ <b>Hangi pipeline başlatılsın?</b>", keyboard=rows)

    def _cmd_durdur(self):
        rows = []
        for key in CITIES:
            proc = self.pipelines_ref.get(key)
            alive = proc is not None and proc.poll() is None
            label = f"{'🟢' if alive else '⚫'} {CITY_ICON[key]} {CITY_LABEL[key]}"
            rows.append([{"text": label, "callback_data": f"stop_{key}"}])
        rows.append([{"text": "⏹️ Hepsini Durdur", "callback_data": "stop_all"}])
        self.send("⏹️ <b>Hangi pipeline durdurulsun?</b>", keyboard=rows)

    def _cmd_yeniden(self):
        if not self.start_fn or not self.stop_fn:
            self.send("⚠️ Kontrol fonksiyonu bağlı değil")
            return
        self.send("🔄 Tüm pipeline'lar yeniden başlatılıyor...")
        for key in CITIES:
            try:
                self.stop_fn(key)
            except Exception:
                pass
        time.sleep(2)
        for key in CITIES:
            try:
                self.start_fn(key)
            except Exception:
                pass
        self.send("✅ Tüm pipeline'lar yeniden başlatıldı\n\n" + self._status_text())

    def _cmd_bugun(self):
        today = date.today().strftime("%d.%m.%Y")
        msg = f"📊 <b>Bugün ({today})</b>\n\n"
        grand = 0
        for city in CITIES:
            count = self._count_today(self.log_paths.get(city))
            grand += count
            msg += f"{CITY_ICON[city]} {CITY_LABEL[city]}: <b>{count}</b> video\n"
        msg += f"\n🔢 Toplam: <b>{grand}</b>"
        self.send(msg)

    def _cmd_son(self):
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
                parts = line.split("UPLOADED", 1)
                rest = parts[1].strip()
                vid_id, title = rest.split("|", 1)
                vid_id = vid_id.strip()
                title = title.strip()[:45]
                url = f"https://youtube.com/watch?v={vid_id}"
                icon = CITY_ICON.get(city, "📍")
                msg += f"{icon} {title}\n<a href=\"{url}\">▶️ izle</a>\n\n"
            except Exception:
                continue
        self.send(msg)

    # ------------------------------------------------------------------ #
    #  Callback query işleyici (inline button'lar)                         #
    # ------------------------------------------------------------------ #

    def _handle_callback(self, callback: dict):
        cid = callback.get("id", "")
        data = callback.get("data", "")
        msg = callback.get("message", {})
        message_id = msg.get("message_id")
        chat_id = str(callback.get("from", {}).get("id", ""))

        if chat_id != self.chat_id:
            self.answer_callback(cid, "⛔ Yetkisiz")
            return

        if data == "refresh_status":
            self.answer_callback(cid, "🔄 Güncelleniyor...")
            self.edit(message_id, self._status_text(),
                      keyboard=self._status_keyboard())

        elif data.startswith("start_"):
            key = data[6:]
            if key == "all":
                self.answer_callback(cid, "▶️ Hepsi başlatılıyor...")
                for k in CITIES:
                    try: self.start_fn and self.start_fn(k)
                    except Exception: pass
                self.edit(message_id,
                          "✅ Tüm pipeline'lar başlatıldı\n\n" + self._status_text(),
                          keyboard=self._status_keyboard())
            elif key in CITIES and self.start_fn:
                self.answer_callback(cid, f"▶️ {CITY_LABEL[key]} başlatılıyor...")
                try:
                    self.start_fn(key)
                    self.edit(message_id,
                              f"✅ {CITY_ICON[key]} <b>{CITY_LABEL[key]}</b> başlatıldı\n\n"
                              + self._status_text(),
                              keyboard=self._status_keyboard())
                except Exception as e:
                    self.edit(message_id, f"⚠️ Hata: {e}")

        elif data.startswith("stop_"):
            key = data[5:]
            if key == "all":
                self.answer_callback(cid, "⏹️ Hepsi durduruluyor...")
                for k in CITIES:
                    try: self.stop_fn and self.stop_fn(k)
                    except Exception: pass
                self.edit(message_id,
                          "⏹️ Tüm pipeline'lar durduruldu\n\n" + self._status_text(),
                          keyboard=self._status_keyboard())
            elif key in CITIES and self.stop_fn:
                self.answer_callback(cid, f"⏹️ {CITY_LABEL[key]} durduruluyor...")
                try:
                    self.stop_fn(key)
                    self.edit(message_id,
                              f"⏹️ {CITY_ICON[key]} <b>{CITY_LABEL[key]}</b> durduruldu\n\n"
                              + self._status_text(),
                              keyboard=self._status_keyboard())
                except Exception as e:
                    self.edit(message_id, f"⚠️ Hata: {e}")
        else:
            self.answer_callback(cid)

    # ------------------------------------------------------------------ #
    #  Güvenli komut router                                                #
    # ------------------------------------------------------------------ #

    def _handle_message(self, msg: dict):
        text = msg.get("text", "").strip()
        chat_id = str(msg.get("chat", {}).get("id", ""))
        if chat_id != self.chat_id:
            return
        cmd = text.split()[0].lower() if text else ""
        if cmd in ("/start", "/yardim", "/help"):
            self._cmd_yardim()
        elif cmd in ("/durum", "/status"):
            self._cmd_durum()
        elif cmd in ("/baslat", "/start_all"):
            self._cmd_baslat()
        elif cmd in ("/durdur", "/stop_all"):
            self._cmd_durdur()
        elif cmd in ("/yeniden", "/restart"):
            self._cmd_yeniden()
        elif cmd in ("/bugun", "/today", "/stats"):
            self._cmd_bugun()
        elif cmd in ("/son", "/last", "/recent"):
            self._cmd_son()

    # ------------------------------------------------------------------ #
    #  Polling                                                             #
    # ------------------------------------------------------------------ #

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
                            if "callback_query" in update:
                                self._handle_callback(update["callback_query"])
                            elif "message" in update:
                                self._handle_message(update["message"])
                        except Exception as e:
                            log.debug(f"Update işleme hatası: {e}")
            except requests.Timeout:
                pass
            except Exception as e:
                log.debug(f"Telegram poll hatası: {e}")
                time.sleep(5)

    # ------------------------------------------------------------------ #
    #  Yardımcılar                                                         #
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

    def start(self):
        self._running = True
        threading.Thread(target=self._poll_loop, daemon=True,
                         name="telegram-bot").start()

    def stop(self):
        self._running = False
