"""Dinamik m3u8 resolver — token'lı kamera kaynakları için TAZE URL üretir.

Bazı belediye kameraları kısa ömürlü token kullanır (Kayseri bltoken 5dk,
Kocaeli tvkur JWT 10dk). Kalıcı m3u8 saklanamaz; kullanım anında çözülür.
Hem extractor hem (ileride) live_streamer bunu kullanır.
"""
import logging
import re
import warnings
warnings.filterwarnings("ignore")
import requests
try:
    requests.packages.urllib3.disable_warnings()
except Exception:
    pass

log = logging.getLogger("resolvers")

_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"}

# Tek paylaşımlı Session → aynı host'a (content.tvkur.com, kocaeliyiseyret.com,
# EmbedPlayer vendorları) TCP/TLS bağlantısı yeniden kullanılır → her resolve'da
# yeni handshake yok, ~100-300ms tasarruf. requests.Session thread-safe (urllib3
# pool); stream pick_next + harvester ayrı thread'lerden güvenle çağırır.
_SESSION = requests.Session()
_SESSION.headers.update(_UA)
_SESSION.verify = False


def _get(u, ref=None, timeout=15):
    h = {"Referer": ref} if ref else None
    last = None
    for _ in range(2):
        try:
            return _SESSION.get(u, headers=h, timeout=timeout)
        except Exception as e:
            last = e
    # Sessizce yutma — debug'ta görünür (bir kamera neden çözülmüyor anlaşılsın)
    log.debug("_get başarısız %s: %s", (u or "")[:70], last)
    return None


def _resolve_embedplayer(domain, embed_id, referer):
    """Ortak EmbedPlayer çözücü: {domain}/stream/EmbedPlayer/{id} → modelJson.EmbedUrl.
    Kayseri, Develi (ve aynı vendor'u kullanan diğerleri) bunu paylaşır."""
    if not embed_id:
        return None
    ep = _get(f"https://{domain}/stream/EmbedPlayer/{embed_id}", ref=referer)
    if not ep:
        return None
    m = re.search(r'"EmbedUrl"\s*:\s*"([^"]+\.m3u8[^"]*)"', ep.text)
    return m.group(1).replace('\\/', '/') if m else None


def resolve_kayseri(embed_id):
    return _resolve_embedplayer("yayin.kayseri.bel.tr", embed_id,
                                "https://tv.kayseri.bel.tr/")


def resolve_develi(embed_id):
    return _resolve_embedplayer("yayin.develi.bel.tr", embed_id,
                                "https://seyret.develi.bel.tr/")


def resolve_kocaeli(slug, cid):
    """Kocaeli: kamera sayfası → player.tvkur.com/l/{JWT} →
    content.tvkur.com/l/{JWT}/master.m3u8. JWT ~10dk; oynatım için
    Referer: player.tvkur.com gerekir (headers ile)."""
    if not (slug and cid):
        return None
    cp = _get(f"https://kocaeliyiseyret.com/Kamera/Index/{slug}/{cid}",
              ref="https://kocaeliyiseyret.com/")
    if not cp:
        return None
    m = re.search(r'player\.tvkur\.com/l/([A-Za-z0-9_.\-]+)', cp.text)
    if not m:
        return None
    return f"https://content.tvkur.com/l/{m.group(1)}/master.m3u8"


def resolve_tvkur_channel(channel_id):
    """tvkur SABİT kanal id (Balıkesir vb.) → canlı m3u8 veya None.

    content.tvkur.com/l/{id} JSON API döner:
      canlı  → {"sources":[{"src":".../master.m3u8"}], ...}
      kapalı → {"error":"Live stream not started yet", ...}
    Kanal id SABİT (dönmez); kamera kapalıyken None döneriz → çağıran TEMİZ atlar
    (eski statik master.m3u8 kapalıyken 404 + transcode çöpü üretiyordu, artık üretmez).
    Oynatımda Referer: player.tvkur.com gerekir (pool headers ile geçilir)."""
    if not channel_id:
        return None
    r = _get(f"https://content.tvkur.com/l/{channel_id}",
             ref="https://player.tvkur.com/")
    if not r or r.status_code != 200:
        return None
    try:
        data = r.json()
    except Exception:
        return None
    for s in (data.get("sources") or []):
        u = s.get("src")
        if u and ".m3u8" in u:
            return u
    return None   # canlı değil ("Live stream not started yet")


def resolve(cam):
    """Kameranın type'ına göre taze m3u8. Statikse stored stream_url döner."""
    t = cam.get("type")
    if t == "kayseri_embed":
        return resolve_kayseri(cam.get("embed_id"))
    if t == "develi_embed":
        return resolve_develi(cam.get("embed_id"))
    if t == "kocaeli_tvkur":
        return resolve_kocaeli(cam.get("slug"), cam.get("cid"))
    if t == "tvkur_channel":
        return resolve_tvkur_channel(cam.get("channel_id"))
    return cam.get("stream_url")
