#!/bin/bash
# Дополнительный live-поток "XM530" БЕЗ vflip — как камера отдаёт картинку на самом
# деле (перевёрнуто по вертикали, см. комментарий у xm530_vflip.sh и в config.yaml).
# Тот же "сырой" источник (H265, что видят detect/record), просто перекодирован в
# H264 для браузера, без коррекции ориентации. Софтверный decode+encode, по образцу
# xm530_vflip.sh.
set -e
FF=/usr/lib/ffmpeg/7.0/bin/ffmpeg
OUT="$1"
SRC="rtsp://alena:Rm9g8fsLobAS6I2jfoEY@127.0.0.1:8554/xm530_cam"   # 2026-09-15: чистый поток камеры (xm530 теперь с оверлеем)
LOG="/tmp/xm530_raw_h264.log"

"$FF" -nostdin -loglevel warning -rtsp_transport tcp -i "$SRC" \
  -c:v libx264 -preset veryfast -crf 21 -g 50 \
  -c:a copy \
  -f rtsp -rtsp_transport tcp "$OUT" 2>>"$LOG"
echo "=== xm530_raw_h264 exited with $? at $(date -u +%H:%M:%S) ===" >>"$LOG"
