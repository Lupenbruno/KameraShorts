"""Kanal için şehir playlistleri oluşturur ve ID'leri config.yaml'a yazar.

Kullanım:
  python create_playlists.py
"""
import yaml
from pathlib import Path
from src.youtube_uploader import YouTubeUploader

PLAYLISTS = {
    "ankara":   {"tr": "Ankara Canlı Kamera",   "desc": "Ankara EGO otobüs ve şehir kameraları"},
    "istanbul": {"tr": "İstanbul Canlı Kamera",  "desc": "İstanbul şehir ve boğaz kameraları"},
    "corum":    {"tr": "Çorum Canlı Kamera",     "desc": "Çorum şehir kameraları"},
    "konya":    {"tr": "Konya Canlı Kamera",     "desc": "Konya şehir kameraları"},
}

CONFIG_PATH = Path("config.yaml")


def main():
    cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))

    # Uploader üzerinden authenticate ol
    uploader = YouTubeUploader(cfg)
    uploader.authenticate()
    service = uploader.service

    created = {}
    for key, info in PLAYLISTS.items():
        # Zaten config'de var mı kontrol et
        existing = None
        if key == "ankara":
            existing = cfg.get("youtube", {}).get("playlist_id")
        else:
            existing = cfg.get("cities", {}).get(key, {}).get("youtube", {}).get("playlist_id")

        if existing:
            print(f"  [{key}] Zaten var: {existing}")
            created[key] = existing
            continue

        resp = service.playlists().insert(
            part="snippet,status",
            body={
                "snippet": {
                    "title": info["tr"],
                    "description": info["desc"],
                },
                "status": {"privacyStatus": "public"},
            }
        ).execute()

        pid = resp["id"]
        created[key] = pid
        print(f"  [{key}] Oluşturuldu: {pid} — {info['tr']}")

    # config.yaml'a yaz
    cfg_text = CONFIG_PATH.read_text(encoding="utf-8")

    # Ankara (global youtube bloğu)
    if "ankara" in created:
        pid = created["ankara"]
        if "playlist_id:" not in cfg_text.split("youtube:")[1].split("cities:")[0]:
            cfg_text = cfg_text.replace(
                "  client_secret_path:",
                f"  playlist_id: \"{pid}\"\n  client_secret_path:"
            )

    # Şehirler
    for key in ["istanbul", "corum", "konya"]:
        if key not in created:
            continue
        pid = created[key]
        # Her şehrin youtube: bloğunun altına ekle
        search = f"      token_path: \"credentials/token_{key}.json\""
        replace = f"      token_path: \"credentials/token_{key}.json\"\n      playlist_id: \"{pid}\""
        if search in cfg_text and f"playlist_id: \"{pid}\"" not in cfg_text:
            cfg_text = cfg_text.replace(search, replace)

    CONFIG_PATH.write_text(cfg_text, encoding="utf-8")
    print("\nconfig.yaml güncellendi.")
    print("\nPlaylist ID'leri:")
    for k, v in created.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
