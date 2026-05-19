#!/usr/bin/env python3
"""KameraShorts v5 — YOLO subprocess helper.

shorts.py'den subprocess olarak cagrilir.
Cikinca RAM serbest kalir (ana process YOLO'yu tasimaz).

Cikti formati (stdout):
    RESULT:{"score": N, "min_score": M, "brightness": B}
"""
import argparse
import json
import sys

from src.ai_filter import analyze_clip


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--clip", required=True)
    p.add_argument("--ffmpeg", default="ffmpeg")
    p.add_argument("--duration", type=int, default=40)
    args = p.parse_args()

    try:
        score, dyn_min, _ = analyze_clip(args.clip, args.ffmpeg, args.duration)
    except Exception as e:
        print("ERROR:%s" % e, file=sys.stderr)
        sys.exit(2)

    print("RESULT:" + json.dumps({"score": int(score), "min_score": int(dyn_min)}))
    sys.exit(0)


if __name__ == "__main__":
    main()
