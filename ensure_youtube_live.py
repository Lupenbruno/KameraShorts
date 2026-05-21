#!/usr/bin/env python3
"""YouTube canlı yayınını her zaman canlı tutar — Kick gibi auto-resume.

Sorun: broadcast'lerde enableAutoStop=true olduğu için ingest 1-2 sn kesilince
(OOM restart, ffmpeg reconnect) YouTube broadcast'i KALICI olarak 'complete'
yapıyor; eski anahtarla geri gelince canlı broadcast kalmadığı için görünmüyor.

Çözüm:
  - enableAutoStop=false  → kesintide YouTube yayını bitirmez, reconnect'te devam.
  - enableAutoStart=false → başlatmayı biz kontrol ederiz ('ready'de takılma yok).
  - Stream sağlıklı + canlı broadcast yoksa → broadcast'i canlıya al.
  - Uygun broadcast yoksa → yeni oluştur + reusable anahtara bağla + canlıya al.

live_streamer'dan BAĞIMSIZ. systemd timer ile her ~2 dk çalışır. Stream'e dokunmaz.
"""
import datetime
import json
import sys

TOKEN_PATH = "/opt/KameraShorts/credentials/token.json"
# Reusable "Default stream key" (5183-... anahtarının liveStream id'si)
STREAM_ID = "8OU9UM9svnsSU5DVjTRi1g1779268655794272"

AYLAR = ["Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran",
         "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık"]
GUNLER = ["Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma",
          "Cumartesi", "Pazar"]
TITLE_TEMPLATE = "🔴 {date} | Türkiye Canlı Şehir Kameraları - Sokak & Trafik 7/24"


def log(*a):
    print(datetime.datetime.now().strftime("%H:%M:%S"), *a, flush=True)


def build_title() -> str:
    now = datetime.datetime.now()
    d = f"{now.day} {AYLAR[now.month - 1]} {now.year} {GUNLER[now.weekday()]}"
    return TITLE_TEMPLATE.format(date=d)[:100]


def yt_client():
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    tok = json.load(open(TOKEN_PATH))
    creds = Credentials(
        token=tok.get("token"),
        refresh_token=tok.get("refresh_token"),
        token_uri=tok.get("token_uri", "https://oauth2.googleapis.com/token"),
        client_id=tok.get("client_id"),
        client_secret=tok.get("client_secret"),
        scopes=tok.get("scopes"),
    )
    return build("youtube", "v3", credentials=creds)


def stream_active(yt) -> bool:
    items = yt.liveStreams().list(part="status", id=STREAM_ID).execute().get("items", [])
    if not items:
        log("HATA: stream bulunamadı:", STREAM_ID)
        return False
    st = items[0]["status"]
    log("stream:", st.get("streamStatus"), "/", st.get("healthStatus", {}).get("status"))
    return st.get("streamStatus") == "active"


def bound_broadcasts(yt, status):
    """Bizim STREAM_ID'ye bağlı, verilen lifecycle statüsündeki broadcast'ler."""
    out = []
    items = yt.liveBroadcasts().list(
        part="snippet,status,contentDetails",
        broadcastStatus=status, maxResults=25).execute().get("items", [])
    for b in items:
        if b["contentDetails"].get("boundStreamId") == STREAM_ID:
            out.append(b)
    return out


def ensure_flags(yt, b):
    """enableAutoStop=false yap. enableAutoStart'a DOKUNMA — statüye göre kilitli
    olabilir (enableAutoStartModificationNotAllowed). Mevcut değeri aynen gönder."""
    cd = b["contentDetails"]
    if cd.get("enableAutoStop") is False:
        return  # zaten istediğimiz gibi
    mutable = {
        "enableAutoStart": cd.get("enableAutoStart", True),  # MEVCUT değer — değiştirme
        "enableAutoStop": False,                              # asıl değişiklik
        "enableDvr": cd.get("enableDvr", True),
        "enableEmbed": cd.get("enableEmbed", True),
        "recordFromStart": cd.get("recordFromStart", True),
        "enableContentEncryption": cd.get("enableContentEncryption", False),
        "startWithSlate": cd.get("startWithSlate", False),
    }
    if "monitorStream" in cd:
        mutable["monitorStream"] = cd["monitorStream"]
    if "latencyPreference" in cd:
        mutable["latencyPreference"] = cd["latencyPreference"]
    try:
        yt.liveBroadcasts().update(
            part="contentDetails",
            body={"id": b["id"], "contentDetails": mutable}).execute()
        log("autoStop=False ayarlandı:", b["id"])
    except Exception as e:
        log("autoStop ayarlanamadı (%s):" % b["id"], str(e)[:140])


def create_and_bind(yt):
    body = {
        "snippet": {
            "title": build_title(),
            "scheduledStartTime": datetime.datetime.utcnow().isoformat("T") + "Z",
        },
        "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False},
        "contentDetails": {
            "enableAutoStart": False,
            "enableAutoStop": False,
            "enableDvr": True,
            "enableEmbed": True,
            "recordFromStart": True,
            "monitorStream": {"enableMonitorStream": False},
        },
    }
    ins = yt.liveBroadcasts().insert(
        part="snippet,status,contentDetails", body=body).execute()
    bid = ins["id"]
    yt.liveBroadcasts().bind(id=bid, part="id,contentDetails", streamId=STREAM_ID).execute()
    log("YENİ broadcast oluşturuldu + bağlandı:", bid)
    return yt.liveBroadcasts().list(
        part="snippet,status,contentDetails", id=bid).execute()["items"][0]


def transition_live(yt, b):
    bid = b["id"]
    life = b["status"]["lifeCycleStatus"]
    if life in ("live", "liveStarting"):
        log("zaten canlı:", bid, life)
        return True
    mon = b["contentDetails"].get("monitorStream", {}).get("enableMonitorStream", False)
    paths = [["testing", "live"]] if mon else [["live"], ["testing", "live"]]
    for path in paths:
        try:
            for to in path:
                yt.liveBroadcasts().transition(
                    broadcastStatus=to, id=bid, part="status").execute()
                log("transition ->", to, "OK")
            log("✓ CANLI:", bid)
            return True
        except Exception as e:
            log("transition", path, "hata:", str(e)[:160])
    return False


def main():
    yt = yt_client()

    # 1. Zaten canlı broadcast var mı? (en ucuz happy-path — 1 API çağrısı)
    live = bound_broadcasts(yt, "active")
    if live:
        ensure_flags(yt, live[0])  # autoStop zaten false ise ekstra çağrı yok
        log("YouTube zaten canlı:", live[0]["id"])
        return 0

    # 2. Canlı yok → ingest sağlıklı mı? (değilse canlıya almak anlamsız)
    if not stream_active(yt):
        log("canlı broadcast yok ve ingest aktif değil — çıkılıyor.")
        return 0

    # 3. Mevcut upcoming (created/ready) adaylarını canlıya almayı dene
    cands = bound_broadcasts(yt, "upcoming")
    for c in cands:
        log("aday deneniyor:", c["id"], c["status"]["lifeCycleStatus"])
        ensure_flags(yt, c)
        c = yt.liveBroadcasts().list(
            part="snippet,status,contentDetails", id=c["id"]).execute()["items"][0]
        if transition_live(yt, c):
            return 0
        log("aday canlıya alınamadı (muhtemelen autoStart=true kilidi):", c["id"])

    # 3. Hiçbir aday olmadı → temiz broadcast oluştur (autoStart=False) ve canlıya al
    log("temiz broadcast oluşturuluyor (autoStart=False, autoStop=False)")
    fresh = create_and_bind(yt)
    ensure_flags(yt, fresh)
    fresh = yt.liveBroadcasts().list(
        part="snippet,status,contentDetails", id=fresh["id"]).execute()["items"][0]
    return 0 if transition_live(yt, fresh) else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        log("GENEL HATA:", e)
        sys.exit(2)
