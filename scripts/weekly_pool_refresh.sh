#!/bin/bash
# Haftalik kamera havuzu yenileme (cron: Pazar 04:00 — dusuk trafik).
# Dinamik sehir listelerini (Kayseri/Develi/Kocaeli) tazeler + havuzu yeniden kurar.
# Boylece offline olan kameralar dusurulur, geri gelenler eklenir (havuz curumesini onler).
# Tum isler 'nice -n 15' + timeout ile calisir — canli yayini rahatsiz etmez.
# GUVENLIK: yeni havuz eskisinin yarisindan azsa (kaynak site cokmus olabilir)
#           ESKI havuzu geri yukler — bir kotu rebuild havuzu silmesin.
set -u
cd /opt/KameraShorts || exit 1
PY=/opt/KameraShorts/venv/bin/python
LOG=/opt/KameraShorts/logs/pool_refresh.log
POOL=/opt/KameraShorts/camera_pool.json

count() { "$PY" -c "import json,sys;print(len(json.load(open(sys.argv[1]))['cameras']))" "$1" 2>/dev/null || echo 0; }

echo "===== $(date '+%F %T') havuz yenileme basladi =====" >> "$LOG"
[ -f "$POOL" ] && cp -p "$POOL" "$POOL.prev"
OLD=$(count "$POOL.prev")

for ex in extract_kayseri.py extract_develi.py extract_kocaeli.py; do
  echo "--- $ex ---" >> "$LOG"
  nice -n 15 timeout 600 "$PY" "$ex" >> "$LOG" 2>&1 || echo "$ex HATA/timeout" >> "$LOG"
done

echo "--- build_camera_pool.py ---" >> "$LOG"
nice -n 15 timeout 900 "$PY" build_camera_pool.py >> "$LOG" 2>&1 || echo "build HATA/timeout" >> "$LOG"

NEW=$(count "$POOL")
echo "kamera sayisi: eski=$OLD yeni=$NEW" >> "$LOG"
if [ "$OLD" -gt 0 ] && [ "$NEW" -lt $((OLD/2)) ]; then
  echo "!! UYARI: yeni havuz cok kucuk ($NEW < $OLD/2) — ESKI havuz geri yukleniyor" >> "$LOG"
  cp -p "$POOL.prev" "$POOL"
fi
echo "===== $(date '+%F %T') bitti =====" >> "$LOG"
