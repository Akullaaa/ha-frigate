#!/bin/bash
# 2026-09-15: vflip убран — после сброса камера отдаёт кадр в нормальной ориентации; имя скрипта историческое.
# Аппаратный (VAAPI) вариант xm530_vflip.sh — тот же vflip для обычного
# live-просмотра "XM530", но кодирование на GPU вместо софтверного libx264.
# Тот же приём, что и у vbr_overlay_vaapi.sh: софтверный decode+vflip (vflip
# сам по себе дешёвый фильтр, не по нему был расход), затем hwupload и
# h264_vaapi encode — по образцу record: у dvor/xm530 в config.yaml.
# 2026-09-09: причина перевода — обычный (не VBR) live-просмотр "XM530" через
# xm530_vflip.sh (софтверный) замерен на 240% CPU в одиночку во время
# реального просмотра — больше, чем сам детектор.
set -e
FF=/usr/lib/ffmpeg/7.0/bin/ffmpeg
OUT="$1"
SRC="rtsp://alena:Rm9g8fsLobAS6I2jfoEY@127.0.0.1:8554/xm530_cam"   # 2026-09-15: чистый поток камеры (xm530 теперь с оверлеем)
LOG="/tmp/xm530_vflip_vaapi.log"

"$FF" -nostdin -loglevel warning -rtsp_transport tcp -i "$SRC" \
  -vf "format=nv12,hwupload" \
  -vaapi_device /dev/dri/renderD128 -c:v h264_vaapi -qp 23 -g 50 \
  -c:a copy \
  -f rtsp -rtsp_transport tcp "$OUT" 2>>"$LOG"
echo "=== xm530_vflip_vaapi exited with $? at $(date -u +%H:%M:%S) ===" >>"$LOG"
