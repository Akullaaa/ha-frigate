#!/bin/bash
# 2026-09-15: vflip убран — после сброса камера отдаёт кадр в нормальной ориентации; имя скрипта историческое.
# Отражение по вертикали (vflip) для обычного live-просмотра "XM530" — у камеры
# нет собственного ONVIF mirror/flip (см. комментарий в config.yaml у записи).
# Раньше здесь был встроенный шаблон go2rtc "ffmpeg:xm530#video=h264#audio=copy",
# который не поддерживает произвольные фильтры (-vf) — заменено на explicit exec:
# с полным контролем. Источник — "сырой" поток (H265, тот же, что видят
# detect/record), не отдельное подключение к камере. Софтверный decode+encode
# (не VAAPI) — по образцу оригинального vbr_overlay.sh, без лишних экспериментов
# с hwaccel decode (см. историю с tuya_sp/vbr_overlay_vaapi.sh, decode на VAAPI
# там не задался).
set -e
FF=/usr/lib/ffmpeg/7.0/bin/ffmpeg
OUT="$1"
SRC="rtsp://alena:Rm9g8fsLobAS6I2jfoEY@127.0.0.1:8554/xm530_cam"   # 2026-09-15: чистый поток камеры (xm530 теперь с оверлеем)
LOG="/tmp/xm530_vflip.log"

"$FF" -nostdin -loglevel warning -rtsp_transport tcp -i "$SRC" \
  -vf null \
  -c:v libx264 -preset veryfast -crf 21 -g 50 \
  -c:a copy \
  -f rtsp -rtsp_transport tcp "$OUT" 2>>"$LOG"
echo "=== xm530_vflip exited with $? at $(date -u +%H:%M:%S) ===" >>"$LOG"
