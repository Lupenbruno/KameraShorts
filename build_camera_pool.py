#!/usr/bin/env python3
"""Kamera havuzu (registry) oluşturur + ffprobe ile AKTİF olanları doğrular.

cameras.json YOK — kameralar config.yaml (cities) + kod içi sabit listeler (İstanbul)
+ dinamik API (Ankara status.json) içinde. Bu script hepsini tek HAVUZA toplar,
her m3u8'i ffprobe ile test eder, /opt/KameraShorts/camera_pool.json'a yazar.

Yayına HİÇBİR ŞEY vermez — sadece kayıt + doğrulama. Kamera ekledikçe genişletilir.

ÖNEMLİ: Tüm akış main() içinde — import edilince ÇALIŞMAZ (yanlışlıkla pool'u
yeniden kurma footgun'u engellendi). Çalıştırmak için: python build_camera_pool.py
"""
import concurrent.futures
import glob as _glob
import json
import os
import ssl
import subprocess
import time
import urllib.request
from collections import Counter

import yaml

OUT = "/opt/KameraShorts/camera_pool.json"
CONFIG = "/opt/KameraShorts/config.yaml"

# ffprobe paralelliği: bu sunucu 3 çekirdek + CANLI STREAM çalıştırıyor. Yüksek
# paralellik stream'i CPU'dan aç bırakır (donma riski). DÜŞÜK tut + her ffprobe'u
# nice -n 15 ile çalıştır → stream'e CPU önceliği. (Önceki 12 → yük zıplıyordu.)
MAX_WORKERS = 6

ISTANBUL_BASE = "https://livestream.ibb.gov.tr/cam_turistik/{slug}.stream/playlist.m3u8"
ISTANBUL = [
    ("Sultanahmet 1", "b_sultanahmet"), ("Sultanahmet 2", "b_sultanahmet2"),
    ("Salacak", "b_salacak"), ("Kapalı Çarşı", "b_kapalicarsi"),
    ("Kadıköy", "b_kadikoy"), ("Taksim Meydanı", "b_taksim_meydan"),
    ("Üsküdar", "b_uskudar"), ("Kız Kulesi", "new_Kızkulesi"),
    ("Eyüp Sultan", "b_eyupsultan"), ("Anadolu Hisarı", "b_anadoluhisari"),
    ("Dragos", "b_dragos"), ("Hidiv Kasrı", "b_hidivkasri"),
    ("Küçükçekmece", "b_kucukcekmece"), ("Metrohan", "b_metrohan"),
    ("Mısır Çarşısı", "b_misircarsisi"), ("Saraçhane", "b_sarachane"),
    ("Ulus Parkı", "b_ulusparki"), ("Pierre Lotti", "b_pierreloti"),
    ("Beyazıt Kulesi 1", "b_beyazitkule"), ("Beyazıt Kulesi 2", "b_beyazitkule2new"),
    ("Beyazıt Meydanı", "b_beyazitmeydani"), ("Büyük Çamlıca", "b_buyukcamlıca"),
    ("Miniatürk", "b_miniatürk"),
]


def probe(cam):
    url = cam.get("stream_url")
    if not url:
        # dinamik/resolver'lı kaynak (extractor önceden doğruladı). ankara_ego hariç (mobil).
        if cam.get("type") and cam.get("type") != "ankara_ego":
            cam["status"] = "active"
        return cam
    try:
        # nice -n 15 → ffprobe stream'e CPU önceliği bırakır (rebuild yayını dondurmasın)
        _cmd = ["nice", "-n", "15", "ffprobe", "-v", "error"]
        _ref = (cam.get("headers") or {}).get("Referer")
        if _ref:
            _cmd += ["-headers", f"Referer: {_ref}\r\n"]
        _cmd += ["-rw_timeout", "8000000", "-select_streams", "v:0",
                 "-show_entries", "stream=width,height", "-of", "csv=p=0", url]
        r = subprocess.run(_cmd, capture_output=True, timeout=18, text=True)
        out = (r.stdout or "").strip().splitlines()
        if out and "," in out[0]:
            cam["status"] = "active"
            cam["res"] = out[0].replace(",", "x")
        else:
            cam["status"] = "dead"
            cam["err"] = (r.stderr or "").strip()[:70]
    except subprocess.TimeoutExpired:
        cam["status"] = "timeout"
    except Exception as e:
        cam["status"] = "dead"
        cam["err"] = str(e)[:70]
    return cam


def _n(c):
    return c.get("count", 1)


def main():
    pool = []

    # --- Çorum + Konya (config.yaml) ---
    with open(CONFIG, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    cities = cfg.get("cities", {})
    for ckey, disp in (("corum", "Çorum"), ("konya", "Konya")):
        for cam in cities.get(ckey, {}).get("cameras", []):
            pool.append({"city": disp, "name": cam.get("name"),
                         "stream_url": cam.get("stream_url"),
                         "source": ckey, "type": "hls"})

    # --- İstanbul (kod içi sabit) ---
    for name, slug in ISTANBUL:
        pool.append({"city": "İstanbul", "name": name,
                     "stream_url": ISTANBUL_BASE.format(slug=slug),
                     "source": "ibb", "type": "hls"})

    # --- Ekstra çıkarılan kameralar (extra_*.json — her şehir kendi dosyası) ---
    for _ef_path in sorted(_glob.glob("/opt/KameraShorts/extra_*.json")):
        try:
            with open(_ef_path, encoding="utf-8") as _ef:
                _extra = json.load(_ef)
            for _c in _extra:
                _c.setdefault("type", "hls")
                pool.append(_c)
            print(f"{_ef_path.split('/')[-1]}: {len(_extra)} kamera eklendi")
        except Exception as _e:
            print(f"{_ef_path} HATA: {_e}")

    # --- Ankara (dinamik status.json) ---
    print("Ankara status.json çekiliyor...")
    try:
        # seyret.ankara.bel.tr SELF-SIGNED sertifika — CERT_NONE zorunlu (halka açık feed).
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        req = urllib.request.Request(
            "https://seyret.ankara.bel.tr/status.json",
            headers={"Referer": "https://seyret.ankara.bel.tr/",
                     "User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15, context=ctx) as _resp:
            data = json.load(_resp)
        print("  type:", type(data).__name__)
        items = None
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            print("  keys:", list(data.keys())[:15])
            for k in ("cameras", "dvrs", "list", "items", "data", "streams"):
                if isinstance(data.get(k), list):
                    items = data[k]
                    break
            if items is None:
                vals = list(data.values())
                if vals and isinstance(vals[0], dict):
                    items = vals
        if items:
            print("  Ankara kamera/DVR sayısı:", len(items))
            if isinstance(items[0], dict):
                print("  örnek:", json.dumps(items[0], ensure_ascii=False)[:220])
            # 381 ayrı satır yerine tek aggregate kayıt (mobil otobüs filosu)
            pool.append({"city": "Ankara",
                         "name": f"EGO Otobüs Filosu ({len(items)} araç)",
                         "stream_url": None, "source": "seyret.ankara (relay)",
                         "type": "ankara_ego", "count": len(items), "status": "mobile",
                         "note": "Mobil otobüs kameraları — relay ile açılır, harvester kaynağı"})
        else:
            print("  Ankara: liste çıkarılamadı, ham:", json.dumps(data)[:200])
    except Exception as e:
        print("  Ankara status.json HATA:", str(e)[:160])

    # --- ffprobe ile doğrulama (paralel, niced) ---
    probe_list = [c for c in pool
                  if c.get("stream_url") or (c.get("type") and c.get("type") != "ankara_ego")]
    print(f"\nffprobe ile {len(probe_list)} kamera test ediliyor "
          f"(paralel, max_workers={MAX_WORKERS}, nice)...")
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        list(ex.map(probe, probe_list))

    by_city, act_city, mob_city = Counter(), Counter(), Counter()
    for c in pool:
        by_city[c["city"]] += _n(c)
        if c.get("status") == "active":
            act_city[c["city"]] += _n(c)
        elif c.get("status") == "mobile":
            mob_city[c["city"]] += _n(c)

    print("\n===== KAMERA HAVUZU OZETI =====")
    print(f"{'SEHIR':<12}{'TOPLAM':>8}{'AKTIF':>8}{'MOBIL':>8}")
    for city in sorted(by_city):
        print(f"{city:<12}{by_city[city]:>8}{act_city.get(city, 0):>8}{mob_city.get(city, 0):>8}")
    print("-" * 36)
    print(f"{'TOPLAM':<12}{sum(by_city.values()):>8}{sum(act_city.values()):>8}{sum(mob_city.values()):>8}")
    print(f"\nŞehir sayısı: {len(by_city)}")

    # --- ATOMİK yazma: tmp'ye yaz → os.replace → okuyan (canlı stream) yarım dosya görmez
    payload = {"updated": time.strftime("%Y-%m-%d %H:%M"), "cameras": pool}
    tmp = OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, OUT)
    print(f"\ncamera_pool.json yazıldı (atomik): {OUT}  ({len(pool)} kamera kaydı)")


if __name__ == "__main__":
    main()
