#!/bin/bash
# Обёртка для мультикамерного Ксоникса (xonix_compositor_cameras.py) — в
# отличие от xonix_layer.sh, здесь всего ДВА процесса в пайпе, а не три:
# сам Python-скрипт порождает все свои decode-процессы (по одному на
# камеру) сам через subprocess, а не через shell-пайп, потому что входов
# несколько и одним stdin их не прогнать. Финальная стадия (h264_vaapi
# encode -> RTSP push) — та же, что и в xonix_layer.sh, вынесена сюда
# без изменений для единообразия.
#
# Заведён как go2rtc exec-источник — см. go2rtc.streams.xonix_cameras в
# config.yaml.
set -e
FF=/usr/lib/ffmpeg/7.0/bin/ffmpeg
OUT="$1"

python3 /config/xonix_compositor_cameras.py \
  | "$FF" -nostdin -loglevel warning -vaapi_device /dev/dri/renderD128 \
      -f rawvideo -pix_fmt bgr24 -video_size 960x540 -framerate 12 -i - \
      -vf format=nv12,hwupload -c:v h264_vaapi -qp 20 -bf 0 -g 24 -keyint_min 24 -an \
      -f rtsp -rtsp_transport tcp "$OUT"
