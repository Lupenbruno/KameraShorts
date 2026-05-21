#!/usr/bin/env python3
"""KameraShorts — YOLO subprocess runner.

Lazy-load YOLO modeli SADECE bu subprocess icinde. Cikinca RAM serbest kalir.
Harvester ve clip_recorder bu script'i subprocess.run ile cagirir.

Eski (kotu) durum:
  harvester.py daemon olarak surekli calisirken YOLO modelini import ediyor
  → harvester RSS 772 MB sabit (saatte 1 kez 30s kullanim icin)

Yeni (iyi) durum:
  harvester.py daemon YOLO import etmez → ~50 MB RSS
  Saatte bir kez bu script subprocess olarak baslar:
    1. YOLO load (8-10s + ~400 MB RAM)
    2. analyze_clip veya quick_check
    3. JSON sonuc stdout'a
    4. Cikinca RAM TAMAMEN serbest

Cikti formati (stdout):
  RESULT:{"score": N, "threshold": M, "passed": true/false}

Kullanim:
  python -m src.yolo_runner analyze --clip /path/to.mp4 --duration 40 --ffmpeg /usr/bin/ffmpeg
  python -m src.yolo_runner quickcheck --url https://... --ffmpeg /usr/bin/ffmpeg
"""
import argparse
import json
import sys


def cmd_analyze(args):
    from src.ai_filter import analyze_clip
    try:
        score, threshold, thumb = analyze_clip(
            args.clip, args.ffmpeg, args.duration)
        result = {
            "score": int(score),
            "threshold": int(threshold),
            "passed": int(score) >= int(threshold),
            "thumb": thumb or "",
        }
        print("RESULT:" + json.dumps(result))
        sys.exit(0 if result["passed"] else 2)
    except Exception as e:
        print("ERROR:" + str(e), file=sys.stderr)
        sys.exit(3)


def cmd_quickcheck(args):
    from src.ai_filter import quick_check
    try:
        passed = quick_check(args.url, args.ffmpeg)
        result = {"passed": bool(passed)}
        print("RESULT:" + json.dumps(result))
        sys.exit(0 if passed else 2)
    except Exception as e:
        print("ERROR:" + str(e), file=sys.stderr)
        sys.exit(3)


def cmd_framecheck(args):
    """ÖNCEDEN ÇIKARILMIŞ kareyi ön-ele (Referer-bilir: kareyi çağıran çeker)."""
    from src.ai_filter import quick_check_frame
    try:
        passed = quick_check_frame(args.frame)
        print("RESULT:" + json.dumps({"passed": bool(passed)}))
        sys.exit(0 if passed else 2)
    except Exception as e:
        print("ERROR:" + str(e), file=sys.stderr)
        sys.exit(3)


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("analyze", help="Klip analizi (skor + threshold)")
    a.add_argument("--clip", required=True)
    a.add_argument("--ffmpeg", default="ffmpeg")
    a.add_argument("--duration", type=int, default=40)
    a.set_defaults(func=cmd_analyze)

    q = sub.add_parser("quickcheck", help="Stream URL on-kontrol (1 kare)")
    q.add_argument("--url", required=True)
    q.add_argument("--ffmpeg", default="ffmpeg")
    q.set_defaults(func=cmd_quickcheck)

    f = sub.add_parser("framecheck", help="Önceden çıkarılmış kare ön-eleme")
    f.add_argument("--frame", required=True)
    f.set_defaults(func=cmd_framecheck)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
