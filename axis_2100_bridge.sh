#!/bin/bash
# Обёртка для axis_2100_bridge.py — тот же приём, что и у xonix_layer*.sh:
# python-скрипт держит единственное соединение к камере и отдаёт чистый
# raw MJPEG (JPEG-кадры подряд, без multipart-обёртки) в stdout, ffmpeg
# декодирует его штатным -f mjpeg (уже без проблем с Content-Length —
# см. axis_2100_bridge.py) и перепаковывает в RTSP push для go2rtc.
#
# Заведён как go2rtc exec-источник — см. go2rtc.streams.axis_2100 в
# config.yaml. НЕ трогать этот файл ради доп. потоков/оверлеев — раньше тут
# был tee на второй ffmpeg (живой FPS-оверлей), но это разветвление сырого
# потока от камеры оказалось рискованным: если "второй" потребитель на
# секунду тормозил (например, при первом открытии его в браузере), tee мог
# придержать запись в ОБА конца, включая критичный для Frigate основной
# поток. FPS-оверлей теперь отдельным потоком axis_2100_fps
# (axis_2100_fps_overlay.sh) — берёт уже готовый, постоянно тёплый
# rtsp://127.0.0.1:8554/axis_2100 из go2rtc, а не сырые байты от камеры —
# полностью независимо от этого моста.
set -e
FF=/usr/lib/ffmpeg/7.0/bin/ffmpeg
OUT="$1"

# Реальный fps камеры ~6.6-6.9 (см. живое измерение в /tmp/axis_2100_fps.txt,
# считает сам axis_2100_bridge.py) — раньше тут стояло -r 2/fps=2, искусственно
# урезая поток втрое под detect.fps=2 из config.yaml (это только частота
# сэмплирования детектором, не лимит камеры/потока). GOP пересчитан под то же
# ~2-секундное окно ключевого кадра (7 fps * 2 = 14).
python3 /config/axis_2100_bridge.py \
  | "$FF" -nostdin -loglevel warning \
      -f mjpeg -r 7 -probesize 32 -analyzeduration 0 -use_wallclock_as_timestamps 1 -i pipe:0 \
      -vf transpose=2,fps=7 -c:v libx264 -preset ultrafast -tune zerolatency \
      -g 14 -keyint_min 7 -an \
      -f rtsp -rtsp_transport tcp "$OUT"
