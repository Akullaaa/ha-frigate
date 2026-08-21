#!/bin/bash
# Обёртка для axis_2100_bridge.py — тот же приём, что и у xonix_layer*.sh:
# python-скрипт держит единственное соединение к камере и отдаёт чистый
# raw MJPEG (JPEG-кадры подряд, без multipart-обёртки) в stdout, ffmpeg
# декодирует его штатным -f mjpeg (уже без проблем с Content-Length —
# см. axis_2100_bridge.py) и перепаковывает в RTSP push для go2rtc.
#
# Заведён как go2rtc exec-источник — см. go2rtc.streams.axis_2100 в
# config.yaml.
set -e
FF=/usr/lib/ffmpeg/7.0/bin/ffmpeg
OUT="$1"

python3 /config/axis_2100_bridge.py \
  | "$FF" -nostdin -loglevel warning \
      -f mjpeg -r 2 -probesize 32 -analyzeduration 0 -use_wallclock_as_timestamps 1 -i pipe:0 \
      -vf transpose=2,fps=2 -c:v libx264 -preset ultrafast -tune zerolatency \
      -g 4 -keyint_min 2 -an \
      -f rtsp -rtsp_transport tcp "$OUT"
