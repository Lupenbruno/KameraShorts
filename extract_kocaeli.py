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

REF = "https://player.tvkur.com/"


def get(u):
    return resolvers._get(u, ref="https://kocaeliyiseyret.com/")


def chk(c):
    u = resolvers.resolve(c)
    if not u:
        c["active"] = False
        return c
    try:
        rr = subprocess.run(
            ["nice", "-n", "15", "ffprobe", "-v", "error", "-headers", f"Referer: {REF}\r\n",
             "-rw_timeout", "8000000", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0", u],
            capture_output=True, timeout=18, text=True)
        c["active"] = bool((rr.stdout or "").strip() and "," in rr.stdout)
    except Exception:
        c["active"] = False
    return c


def main():
    links = []
    r = get("https://kocaeliyiseyret.com/")
    if r:
        links += re.findall(r'/Kamera/Index/([A-Za-z0-9\-]+)/(\d+)', r.text)
    links = list(dict.fromkeys(links))
    print("kamera link:", len(links))

    cams = [{"city": "Kocaeli", "name": ("Kocaeli " + s.replace('-', ' ').title())[:60],
             "slug": s, "cid": c, "type": "kocaeli_tvkur", "source": "kocaeliyiseyret",
             "headers": {"Referer": REF}} for s, c in links]

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
        list(ex.map(chk, cams))
    active = [{k: c[k] for k in ("city", "name", "slug", "cid", "type", "source", "headers")}
              for c in cams if c.get("active")]
    print("AKTİF:", len(active), "/", len(cams))
    with open("/opt/KameraShorts/extra_kocaeli.json", "w", encoding="utf-8") as f:
        json.dump(active, f, ensure_ascii=False, indent=1)
    print("yazıldı: extra_kocaeli.json")


if __name__ == "__main__":
    main()
