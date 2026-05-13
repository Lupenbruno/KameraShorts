#!/bin/bash
DIRS=(
  /opt/KameraShorts/data/clips
  /opt/KameraShorts/data/istanbul_clips
  /opt/KameraShorts/data/corum_clips
  /opt/KameraShorts/data/konya_clips
  /opt/KameraShorts/data/yolo_test
)
for d in "${DIRS[@]}"; do
  find "$d" -name '*.mp4' -mtime +1 -delete 2>/dev/null
  find "$d" -name '*.meta.json' -mtime +1 -delete 2>/dev/null
  find "$d" -name '*.jpg' -mtime +1 -delete 2>/dev/null
done
echo "$(date): Klip temizliği tamamlandı"
