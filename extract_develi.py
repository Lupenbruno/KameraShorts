import re
import json
import subprocess
import concurrent.futures
import warnings
warnings.filterwarnings("ignore")
import requests
try:
    requests.packages.urllib3.disable_warnings()
except Exception:
    pass
import resolvers


def get(u):
    return resolvers._get(u, ref="https://seyret.develi.bel.tr/")


def fetch_cam(slug):
    cp = get(f"https://seyret.develi.bel.tr/{slug}")
    if not cp:
        return None
    e = re.search(r'/stream/EmbedPlayer/(\d+)', cp.text)
    if not e:
        return None
    nm = re.search(r'<h1>([^<]+)</h1>', cp.text)
    base = nm.group(1).strip() if nm else slug.replace('-', ' ').title()
    return {"city": "Develi", "name": ("Develi " + base)[:60], "slug": slug,
            "embed_id": e.group(1), "type": "develi_embed", "source": "seyret.develi"}


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
    pages = ["https://seyret.develi.bel.tr/develi-genel", "https://seyret.develi.bel.tr/"]
    cands = []
    for p in pages:
        r = get(p)
        if r:
            cands += re.findall(r'href="/([A-Za-z0-9\-]{3,})"', r.text)
    cands = [c for c in dict.fromkeys(cands) if c not in ("develi-genel",)][:60]
    print("aday slug:", len(cands))

    cams = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as ex:
        for r in ex.map(fetch_cam, cands):
            if r:
                cams.append(r)
    seen, uniq = set(), []
    for c in cams:
        if c["embed_id"] not in seen:
            seen.add(c["embed_id"])
            uniq.append(c)
    cams = uniq
    print("kamera (embed_id):", len(cams))

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
        list(ex.map(chk, cams))
    active = [{k: c[k] for k in ("city", "name", "slug", "embed_id", "type", "source")}
              for c in cams if c.get("active")]
    print("AKTİF:", len(active), "/", len(cams))
    with open("/opt/KameraShorts/extra_develi.json", "w", encoding="utf-8") as f:
        json.dump(active, f, ensure_ascii=False, indent=1)
    print("yazıldı: extra_develi.json")


if __name__ == "__main__":
    main()
