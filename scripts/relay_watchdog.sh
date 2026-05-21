#!/bin/bash
# Relay (tee -> YouTube/Kick) watchdog. Cron: her dakika.
# "Encoder CANLI ama relay OLU/TAKILI" durumunu yakalar — systemd Restart=always
# bunu gormez (surec yasiyor ama veri gitmiyor). Daha once elle mediamtx restart ile
# cozdugumuz stuck-tee senaryosunu otomatiklestirir.
#
# COK MUHAFAZAKAR (yanlis-pozitif = saglikli yayini bosuna restart etmek):
#   - 5 ardisik basarisizlik (~5 dk KESINTISIZ YouTube kopuklugu) gerekir
#   - encoder canli olmali (degilse live-service Restart=always halleder, karismayiz)
#   - aksiyon sonrasi 10 dk cooldown (restart dongusu olmaz)
# Saglikli relay'de ASLA aksiyon almaz.
#
# Mod: "check" = sadece durumu yaz, AKSIYON YOK | "run" = (varsayilan) aksiyon alabilir
set -u
MODE="${1:-run}"
STATE=/tmp/ks_relay_wd.fails
COOLDOWN=/tmp/ks_relay_wd.lastact
LOG=/opt/KameraShorts/logs/relay_watchdog.log
FAIL_LIMIT=5
COOLDOWN_SEC=600

now=$(date +%s)
log(){ echo "$(date '+%F %T') $*" >> "$LOG"; }

# Encoder canli mi? (/tmp/ks_v4/stream.pipe besleyen ffmpeg)
ENC=$(pgrep -f 'stream.pipe' | head -1)
# Tee sureci (YouTube/Kick relay) — cmdline'da youtube var
TEE=$(pgrep -f 'a.rtmp.youtube.com' | head -1)
# Tee'nin YouTube'a (uzak :1935, localhost degil) ESTABLISHED baglantisi var mi?
YT=""
if [ -n "$TEE" ]; then
  YT=$(ss -tnp 2>/dev/null | grep -F "pid=$TEE," | grep ':1935' | grep -v '127.0.0.1' | head -1)
fi

healthy=0
if [ -n "$TEE" ] && [ -n "$YT" ]; then healthy=1; fi

if [ "$MODE" = "check" ]; then
  echo "ENC=${ENC:-yok} TEE=${TEE:-yok} YT_CONN=$([ -n "$YT" ] && echo VAR || echo YOK) => $([ $healthy -eq 1 ] && echo HEALTHY || echo UNHEALTHY)"
  exit 0
fi

# Encoder yoksa bizim isimiz degil
if [ -z "$ENC" ]; then echo 0 > "$STATE"; exit 0; fi

if [ "$healthy" -eq 1 ]; then
  echo 0 > "$STATE"
  exit 0
fi

# Basarisiz — sayaci artir
fails=$(cat "$STATE" 2>/dev/null || echo 0)
fails=$((fails+1))
echo "$fails" > "$STATE"
log "UNHEALTHY ($fails/$FAIL_LIMIT) ENC=$ENC TEE=${TEE:-yok} YT_CONN=YOK"

if [ "$fails" -ge "$FAIL_LIMIT" ]; then
  last=$(cat "$COOLDOWN" 2>/dev/null || echo 0)
  if [ $((now-last)) -ge "$COOLDOWN_SEC" ]; then
    log "!! AKSIYON: $FAIL_LIMIT ardisik basarisizlik — mediamtx yeniden baslatiliyor (taze tee)"
    systemctl restart mediamtx
    echo "$now" > "$COOLDOWN"
    echo 0 > "$STATE"
  else
    log "cooldown aktif ($((now-last))s < ${COOLDOWN_SEC}s) — aksiyon ertelendi"
  fi
fi
