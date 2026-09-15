#!/bin/bash
# Мост коррекции геометрии Tuya SP для live-просмотра (go2rtc.streams.tuya_sp_fixed,
# live.streams у камеры tuya_sp в config.yaml). Запись/детект Frigate его не используют —
# смотрят на tuya_sp напрямую.
#
# Полностью аппаратный путь: hwaccel vaapi decode -> hwdownload (только для обрезки) ->
# crop -> hwupload -> scale_vaapi -> h264_vaapi encode.
#
# 2026-08-31: первая версия (подключённая сразу к live) падала на старте с "Invalid too big
# or non positive size for width/height" в crop — гонка: источник tuya_sp ещё не отдавал
# кадры 2304x1296 в момент, когда этот скрипт стартовал сразу после рестарта Frigate/go2rtc
# (в ручных тестах source уже был "тёплым", живым какое-то время). Починено ожиданием
# подтверждения продюсера через go2rtc API перед запуском ffmpeg — проверено изолированно
# (отдельный тестовый go2rtc-поток, не подключённый к live) дважды подряд без ошибок,
# затем подключено к live.
set -e
FF=/usr/lib/ffmpeg/7.0/bin/ffmpeg
OUT="$1"
SRC="rtsp://127.0.0.1:8554/tuya_sp?video"

# Ждём, пока go2rtc сам не подтвердит, что у tuya_sp есть продюсер (реальное соединение с
# камерой), а не гадаем по таймауту вслепую.
for i in $(seq 1 30); do
  CODE=$(curl -s -o /dev/null -w "%{http_code}" -u "alena:Rm9g8fsLobAS6I2jfoEY" \
    "http://127.0.0.1:1984/api/streams?src=tuya_sp" 2>/dev/null || echo "000")
  if [ "$CODE" = "200" ]; then
    HAS_PRODUCER=$(curl -s -u "alena:Rm9g8fsLobAS6I2jfoEY" "http://127.0.0.1:1984/api/streams?src=tuya_sp" \
      | python3 -c "import json,sys; d=json.load(sys.stdin,strict=False); print(len(d.get('producers',[])))" 2>/dev/null || echo "0")
    if [ "$HAS_PRODUCER" -gt 0 ] 2>/dev/null; then
      break
    fi
  fi
  sleep 1
done

"$FF" -nostdin -loglevel warning -user_agent "FFmpeg Frigate/bridge" -rtsp_transport tcp -timeout 10000000 \
  -analyzeduration 5000000 -probesize 5000000 \
  -hwaccel vaapi -hwaccel_output_format vaapi -vaapi_device /dev/dri/renderD128 -i "$SRC" \
  -vf "hwdownload,format=nv12,crop=2004:1296:150:0,hwupload,scale_vaapi=w=2304:h=1296" \
  -c:v h264_vaapi -qp 23 \
  -c:a aac \
  -f rtsp -rtsp_transport tcp "$OUT"
