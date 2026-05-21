"""Havuz tabanlı kamera seçici — SHUFFLE-PLAY rotasyon.

Algoritma (müzik çalar "shuffle" mantığı):
  Tüm AKTİF havuz kameralarını bir DESTE'ye koy, karıştır → deste bitene kadar
  her kamera TAM 1 KEZ çıkar (deste bitmeden hiçbir kamera tekrarlamaz). Bitince
  yeniden karışır. Deste şehirler-arası round-robin + şehir-içi karışık kurulur
  → ardışık seçimler farklı şehir olur (tekdüzelik yok), kameralar da karışık.
  Offline kamera (resolve None) atlanır. Son gösterilenler diske yazılır →
  yayın restart'ında bile hemen aynı kameralar gösterilmez.

Dinamik kameralar resolvers.resolve() ile taze m3u8 alır. live_streamer
resolve_camera('pool_city') bunu çağırır. Çekirdeğe dokunmaz — sadece sıradaki kaynak.
"""
import json
import os
import random
import threading

import resolvers

POOL_PATH = "/opt/KameraShorts/camera_pool.json"
RECENT_PATH = "/opt/KameraShorts/data/pool_recent.json"  # restart-güvenli son gösterilenler
EXCLUDE_CITIES = {"Ankara"}  # Ankara mobil/ayrı (ankara_api), pool_city dışı
RECENT_KEEP = 15             # restart + deste-sınırı tekrarını önlemek için son N
MAX_SKIP = 20               # bir pick'te en fazla N offline kamera atla (cluster güvenliği)

_lock = threading.Lock()
_st = {"deck": [], "pool": None, "mtime": 0.0, "recent": None}


def _load_pool():
    try:
        m = os.path.getmtime(POOL_PATH)
        if m != _st["mtime"] or _st["pool"] is None:
            data = json.load(open(POOL_PATH, encoding="utf-8"))
            _st["pool"] = data.get("cameras", [])
            _st["mtime"] = m
            _st["deck"] = []   # havuz değişti → desteyi yeniden kur
    except Exception:
        if _st["pool"] is None:
            _st["pool"] = []
    return _st["pool"]


def _cities_active():
    """{şehir: [aktif kamera]} — status active + Ankara hariç."""
    out = {}
    for c in _load_pool():
        if c.get("status") == "active" and c.get("city") not in EXCLUDE_CITIES:
            out.setdefault(c["city"], []).append(c)
    return out


def _key(cam):
    # BENZERSİZ kimlik: aynı adlı kameralar (Balıkesir 20× "Şehir Kameraları")
    # channel_id/slug/embed_id/url ile ayrışsın → recent doğru çalışsın.
    uid = (cam.get("channel_id") or cam.get("slug") or cam.get("embed_id")
           or cam.get("stream_url") or cam.get("name", ""))
    return str(uid) + "@" + cam.get("city", "")


def _load_recent():
    if _st["recent"] is None:
        try:
            _st["recent"] = list(json.load(open(RECENT_PATH, encoding="utf-8")).get("recent", []))
        except Exception:
            _st["recent"] = []
    return _st["recent"]


def _save_recent():
    try:
        os.makedirs(os.path.dirname(RECENT_PATH), exist_ok=True)
        json.dump({"recent": _st["recent"][-RECENT_KEEP:]},
                  open(RECENT_PATH, "w", encoding="utf-8"))
    except Exception:
        pass


def _build_deck(cities):
    """KESİR serpiştirme: her şehrin kameralarını TÜM desteye eşit dağıt.
    Her kameraya pozisyon = (şehir-içi sıra + jitter) / şehir_kamera_sayısı verilir;
    pozisyona göre sıralanır → büyük şehirler sık, küçükler seyrek ama HER YERE yayılır
    (round-robin'in 'küçük şehir erken tükenir → sonda kümelenme' sorunu yok).
    Son gösterilenler deste SONUNA itilir (deste-sınırı + restart tekrarı yok)."""
    items = []
    for city, cams in cities.items():
        cl = list(cams)
        random.shuffle(cl)
        n = max(1, len(cl))
        for i, cam in enumerate(cl):
            pos = (i + random.uniform(0.0, 1.0)) / n   # [0,1) aralığına eşit dağıt
            items.append((pos, cam))
    items.sort(key=lambda x: x[0])
    deck = [cam for _, cam in items]
    recent = set(_load_recent())
    head = [c for c in deck if _key(c) not in recent]
    tail = [c for c in deck if _key(c) in recent]
    _st["deck"] = head + tail               # baştan pop → recent'lar en sonda


def pick_next():
    """Shuffle-play: desteden sıradaki CANLI kamera. Deste bitince yeniden karışır.
    Döner: {city, name, stream_url, headers, ssl} veya None."""
    with _lock:
        cities = _cities_active()
        if not cities:
            return None
        skipped = 0
        while skipped <= MAX_SKIP:
            if not _st["deck"]:
                _build_deck(cities)
                if not _st["deck"]:
                    return None
            cam = _st["deck"].pop(0)
            url = resolvers.resolve(cam)
            if not url:
                skipped += 1                 # offline → atla, sıradaki kamera
                continue
            _st["recent"] = (_load_recent() + [_key(cam)])[-RECENT_KEEP:]
            _save_recent()
            return {"city": cam.get("city", ""),
                    "name": cam.get("name", cam.get("city", "")),
                    "stream_url": url, "headers": cam.get("headers", {}) or {},
                    "ssl": False}  # municipal self-signed sertifikalar — doğrulama kapalı
        return None


def status():
    cities = _cities_active()
    total = sum(len(v) for v in cities.values())
    return {"deste_kalan": len(_st["deck"]), "deste_toplam": total,
            "havuz_sehir": list(cities.keys()),
            "sehir_kamera": {k: len(v) for k, v in cities.items()},
            "son_gosterilen": _load_recent()[-8:]}
