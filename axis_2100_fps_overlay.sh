#!/bin/bash
# Оверлей живого FPS для axis_2100 — независимо от axis_2100_bridge.sh
# (см. комментарий там про риск tee). Источник — уже тёплый локальный
# рестрим rtsp://127.0.0.1:8554/axis_2100 (Frigate и так постоянно его
# читает для detect/record), НЕ сырые байты от камеры: ни одного лишнего
# физического подключения к самой камере.
#
# drawtext читает /tmp/axis_2100_fps.txt (reload=1 — перечитывает по
# изменению mtime), файл пишет axis_2100_bridge.py из реального времени
# прихода кадров с камеры.
set -e
FF=/usr/lib/ffmpeg/7.0/bin/ffmpeg
OUT="$1"
SRC="rtsp://alena:Rm9g8fsLobAS6I2jfoEY@127.0.0.1:8554/axis_2100_cam"   # 2026-09-15: чистый поток моста (axis_2100 теперь с аурой)

"$FF" -nostdin -loglevel warning -rtsp_transport tcp -i "$SRC" \
  -vf "drawtext=textfile=/tmp/axis_2100_fps.txt:reload=1:fontsize=28:fontcolor=white@0.75:box=1:boxcolor=black@0.4:boxborderw=6:x=10:y=10" \
  -c:v libx264 -preset ultrafast -tune zerolatency -g 4 -keyint_min 2 -an \
  -f rtsp -rtsp_transport tcp "$OUT" 2>>/tmp/axis_2100_fps_overlay.log
echo "=== exited with $? at $(date -u +%H:%M:%S) ===" >>/tmp/axis_2100_fps_overlay.log
