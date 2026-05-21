import re
import json
import subprocess
import concurrent.futures
import warnings
from collections import Counter
warnings.filterwarnings("ignore")
import requests
try:
    requests.packages.urllib3.disable_warnings()
except Exception:
    pass
import resolvers

UA = resolvers._UA


def get(u, ref=None):
    return resolvers._get(u, ref)


def fetch_cam(slug):
    cp = get(f"https://tv.kayseri.bel.tr/{slug}")
    if not cp:
        return None
    e = re.search(r'/stream/EmbedPlayer/(\d+)', cp.text)
    if not e:
        return None
    nm = re.search(r'<h1>([^<]+)</h1>', cp.text)
    base = nm.group(1).strip() if nm else slug.replace('-', ' ').title()
    return {"city": "Kayseri", "name": ("Kayseri " + base)[:60], "slug": slug,
            "embed_id": e.group(1), "type": "kayseri_embed", "source": "tv.kayseri"}


def chk(c):
    u = resolvers.resolve(c)
    if not u:
        c["active"] = False
        return c
    try:
        rr = subprocess.run(
            ["nice", "-n", "15", "ffprobe", "-v", "error", "-rw_timeout", "8000000",
             "-select_streams", "v:0", "-show_entries", "stream=width,height",
             "-of", "csv=p=0", u], capture_output=True, timeout=18, text=True)
        c["active"] = bool((rr.stdout or "").strip() and "," in rr.stdout)
    except Exception:
        c["active"] = False
    return c


def main():
    h = get("https://tv.kayseri.bel.tr/").text
    # kamera linkleri: <a href="slug">  <div class="w3-quarter ...
    slugs = list(dict.fromkeys(re.findall(
        r'<a href="([a-z0-9\-]{3,})">\s*<div class="w3-quarter', h, re.I)))
    if len(slugs) < 10:  # fallback: thumbnail'i takip eden href
        slugs = list(dict.fromkeys(re.findall(
            r'<a href="([a-z0-9\-]{3,})">(?=(?:(?!</a>).){0,400}Thumbnails)', h, re.S | re.I)))
    print("slug sayısı:", len(slugs))

    cams = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
        for r in ex.map(fetch_cam, slugs):
            if r:
                cams.append(r)
    print("kamera (embed_id bulundu):", len(cams))

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
        list(ex.map(chk, cams))

    active = [{k: c[k] for k in ("city", "name", "slug", "embed_id", "type", "source")}
              for c in cams if c.get("active")]
    print("AKTİF:", len(active), "/", len(cams))
    with open("/opt/KameraShorts/extra_kayseri.json", "w", encoding="utf-8") as f:
        json.dump(active, f, ensure_ascii=False, indent=1)
    print("yazıldı: extra_kayseri.json")


if __name__ == "__main__":
    main()
