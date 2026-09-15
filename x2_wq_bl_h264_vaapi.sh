#!/bin/bash
# VAAPI-вариант обычного "X2-WQ-BL" live (был встроенный шаблон go2rtc
# "ffmpeg:x2_wq_bl#video=h264#audio=copy" — софтверный, без -vf). Та же схема, что
# xm530_vflip_vaapi.sh/tambur_h264_vaapi.sh: софтверный decode, hwupload,
# h264_vaapi encode. Без vflip — x2_wq_bl физически не перевёрнута.
# 2026-09-09: замер во время реального просмотра — 87.4% CPU на софтверном
# варианте (x2_wq_bl_h264 из встроенного шаблона go2rtc).
set -e
FF=/usr/lib/ffmpeg/7.0/bin/ffmpeg
OUT="$1"
SRC="rtsp://alena:Rm9g8fsLobAS6I2jfoEY@127.0.0.1:8554/x2_wq_bl"
LOG="/tmp/x2_wq_bl_h264_vaapi.log"

"$FF" -nostdin -loglevel warning -rtsp_transport tcp -i "$SRC" \
  -vf "format=nv12,hwupload" \
  -vaapi_device /dev/dri/renderD128 -c:v h264_vaapi -qp 23 -g 50 \
  -c:a copy \
  -f rtsp -rtsp_transport tcp "$OUT" 2>>"$LOG"
echo "=== x2_wq_bl_h264_vaapi exited with $? at $(date -u +%H:%M:%S) ===" >>"$LOG"
