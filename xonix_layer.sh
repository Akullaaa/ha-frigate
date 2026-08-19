#!/bin/bash
# Ксоникс-слой, гибридная версия GPU+CPU: один непрерывный процесс, без
# перезапусков. Чистый GPU-путь оказался недостижим для плавной анимации: ни у
# одного VAAPI-фильтра здесь нет runtime-параметров (sendcmd не работает) и нет
# expression-синтаксиса в layout у xstack_vaapi (проверено — падает даже на
# голом "n"), а перезапуск процесса под каждый шаг выглядит как разрыв потока
# каждый раз (см. project_tambur_hevc_playback — там же итог GPU-only разведки:
# crop_vaapi нет, overlay_vaapi/transpose_vaapi драйвер не поддерживает,
# xstack_vaapi рисует только последний вход композита).
#
# Источник — суб-поток камеры (dvor_sub, 800x448@12fps), а не основной
# 1920x1080@25fps: софтовый декод HEVC полного потока не успевал за реальным
# временем на этом CPU (speed=0.7x под нагрузкой трёх продакшен-камер, замерено
# напрямую) — отсюда была рваная, неровная анимация. Суб-поток декодируется с
# speed=1.1x, то есть с запасом. Скорость отскока в компоузере пересчитана под
# 12fps (было под 25fps).
#
# Три стадии конвейера:
#   ffmpeg (decode + scale_vaapi, GPU) | xonix_compositor.py (копирование
#   квадратика тем же кадром, CPU — единственный кусок без GPU-пути) |
#   ffmpeg (h264_vaapi, GPU → RTSP push)
# Заведён как go2rtc exec-источник — см. go2rtc.streams в config.yaml.
set -e
FF=/usr/lib/ffmpeg/7.0/bin/ffmpeg
CAM="$1"
OUT="$2"

"$FF" -nostdin -loglevel warning -vaapi_device /dev/dri/renderD128 \
    -rtsp_transport tcp -i "rtsp://127.0.0.1:8554/${CAM}_sub" \
    -vf format=nv12,hwupload,scale_vaapi=960:540,hwdownload,format=nv12,format=bgr24 -f rawvideo - \
  | python3 /config/xonix_compositor.py \
  | "$FF" -nostdin -loglevel warning -vaapi_device /dev/dri/renderD128 \
      -f rawvideo -pix_fmt bgr24 -video_size 960x540 -framerate 12 -i - \
      -vf format=nv12,hwupload -c:v h264_vaapi -qp 20 -bf 0 -g 24 -keyint_min 24 -an \
      -f rtsp -rtsp_transport tcp "$OUT"
