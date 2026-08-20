#!/bin/bash
# Обёртка для xonix_game.py — тот же двухпроцессный паттерн, что у
# xonix_layer_multicam.sh: сам движок порождает decode-процессы камер и
# MQTT-клиента изнутри, здесь только финальная стадия (h264_vaapi encode
# -> RTSP push в go2rtc).
#
# Заведён как go2rtc exec-источник — см. go2rtc.streams.xonix_game в
# config.yaml.
set -e
FF=/usr/lib/ffmpeg/7.0/bin/ffmpeg
OUT="$1"

python3 /config/xonix_game.py \
  | "$FF" -nostdin -loglevel warning -vaapi_device /dev/dri/renderD128 \
      -f rawvideo -pix_fmt bgr24 -video_size 960x540 -framerate 12 -i - \
      -vf format=nv12,hwupload -c:v h264_vaapi -qp 20 -bf 0 -g 24 -keyint_min 24 -an \
      -f rtsp -rtsp_transport tcp "$OUT"
