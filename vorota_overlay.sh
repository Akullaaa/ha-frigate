#!/bin/bash
# exec-источник go2rtc: поток камеры ворот с оверлеем телеметрии — тот же дизайн, что у XPS и iMac
# (три плашки-столбика с подгруппами, часы с долями секунды, индикаторы частоты кадров, блок скопов
# «Сигнал» внизу по центру: waveform, vectorscope, бегущие графики освещённости и движения).
# Конвейер: rtsp vorota (H.265 1920x2160@20, программный декод — HEVC на этом Intel/i965 в VAAPI нет)
#   → overlay PNG-слоя (vorota_overlay_render.py, данные — vorota_overlay_collect.py)
#   → скопы (ffmpeg сам, по геометрии из scopes_geom.txt рендерера)
#   → drawtext (часы, ● по кругу, █ по низу, номер кадра) → hwupload → h264_vaapi → RTSP {output}.
# Сборщик и рендерер живут, пока жив PID этого скрипта (после exec — сам ffmpeg): go2rtc, останавливая
# поток, убивает ffmpeg, фоновые циклы видят это и выходят. Живой fps/освещённость — через именованные
# каналы (-progress и metadata=print), как у iMac. Отладка: аргумент-путь .mp4 — 6 с в файл вместо RTSP.
# Грабли (2026-09-15): ветки скопов с РАЗНЫМИ частотами (fps=1/10/4 + overlay) намертво вешали граф
# (память росла, metadata не писались) — все скопы считаются на частоте потока по копии 320x180, а
# минута истории у drawgraph получается шириной 1200 px со scale до размера плашки. Юникод в drawtext
# (●, █, «кадр») требует LC_ALL=C.UTF-8 — без него в exec-окружении go2rtc/docker текст превращается в «???».
# Цена: ~1,7 ядра на 1920x2160@20 (программный HEVC-декод — половина), запускается go2rtc только пока
# кто-то смотрит этот поток (exec-источники по требованию), запись камеры идёт отдельно и без оверлея.
set -e
export LC_ALL=C.UTF-8
FF=/usr/lib/ffmpeg/7.0/bin/ffmpeg
export OV_DIR=/tmp/vorota_overlay
export OV_W=1920 OV_H=2160 OV_SCALE=1.5
export GO2RTC_AUTH="alena:Rm9g8fsLobAS6I2jfoEY"
OUT="$1"
SRC="rtsp://alena:Rm9g8fsLobAS6I2jfoEY@127.0.0.1:8554/vorota"
FONT=/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf
OUTFPS=20
LOG=/tmp/vorota_overlay.log
mkdir -p "$OV_DIR"
rm -f "$OV_DIR"/frame.txt "$OV_DIR"/yavg.txt "$OV_DIR"/scopes_geom.txt "$OV_DIR"/clock_pos.txt "$OV_DIR"/overlay.png
python3 /config/vorota_overlay_collect.py $$ 2>>"$LOG" &
( while kill -0 $$ 2>/dev/null; do python3 /config/vorota_overlay_render.py $$ --noclock 2>>"$LOG"; sleep 1; done ) &
FIFO_P="$OV_DIR/progress.fifo"; FIFO_Y="$OV_DIR/yavg.fifo"
rm -f "$FIFO_P" "$FIFO_Y"; mkfifo "$FIFO_P" "$FIFO_Y"
# Читатели переоткрывают канал после EOF (ffmpeg открывает файл metadata дважды при перестройке графа).
( fr=""; while kill -0 $$ 2>/dev/null; do while IFS="=" read -r k v; do
    case "$k" in frame) fr="$v";; out_time_us) echo "$fr $v" > "$OV_DIR/frame.txt.tmp" && mv -f "$OV_DIR/frame.txt.tmp" "$OV_DIR/frame.txt";; esac
  done < "$FIFO_P"; sleep 0.2; done ) &
( while kill -0 $$ 2>/dev/null; do while read -r line; do case "$line" in *YAVG=*) echo "${line#*YAVG=}" > "$OV_DIR/yavg.txt.tmp" && mv -f "$OV_DIR/yavg.txt.tmp" "$OV_DIR/yavg.txt";; esac; done < "$FIFO_Y"; sleep 0.2; done ) &
# первый кадр слоя и геометрия должны лежать на диске до старта ffmpeg (файлы с переводом строки —
# иначе read под set -e возвращает ошибку на EOF)
for i in $(seq 1 40); do [ -s "$OV_DIR/overlay.png" ] && [ -s "$OV_DIR/scopes_geom.txt" ] && [ -s "$OV_DIR/clock_pos.txt" ] && break; sleep 0.25; done
read -r CX CY CFS < "$OV_DIR/clock_pos.txt" 2>/dev/null || { CX=24; CY=24; CFS=27; }
read -r WFX WFY WFW WFH VSX VSY VSS GX G1Y G2Y GW GH < "$OV_DIR/scopes_geom.txt" || [ -n "$GH" ]
case "$OUT" in
  rtsp*) SINK=(-f rtsp -rtsp_transport tcp "$OUT");;
  *)     SINK=(-t 6 -y "$OUT");;
esac
echo "=== старт $(date '+%F %T') → $OUT" >> "$LOG"
DT="drawtext=fontfile=$FONT:fontcolor=0xFFD65A:shadowcolor=black@0.8:shadowx=1:shadowy=1"
CLOCK="$DT:fontsize=$CFS:x=$CX:y=$CY:text='%{localtime\:%F  %T.%4N}'"
# ● по кругу, вписанному в ширину кадра (оборот/с), █ по нижнему краю (проход 2 с), номер кадра
FX="$DT:fontsize=32:x='w/2+(w/2-18)*cos(2*PI*t)-9':y='h/2+(w/2-18)*sin(2*PI*t)-18':text='●',$DT:fontsize=18:x='mod(t*w/2\,w)-4':y=h-18:text='█',$DT:fontsize=22:x=w-260:y=h-56:text='кадр %{n}'"
# Скопы на частоте потока по копии 320x180: waveform / vectorscope (центр 96x96 из 256x256) / два
# drawgraph по signalstats (освещённость YAVG, движение YDIF; ширина 1200 px = ~минута при 20 к/с,
# затем scale до плашки), прозрачные (colorkey по чёрному, 85 %). YAVG для сборщика — из той же ветки.
SC="[sc]scale=320:180,format=yuv420p,split=3[w][v][g];\
[w]waveform=mode=column:intensity=0.12:scale=ire,format=rgba,scale=${WFW}:${WFH},colorkey=black:0.12:0.05,colorchannelmixer=aa=0.85[wf];\
[v]vectorscope=mode=color3:intensity=0.2,format=rgba,crop=96:96:80:80,scale=${VSS}:${VSS},colorkey=black:0.12:0.05,colorchannelmixer=aa=0.85[vs];\
[g]signalstats,metadata=print:key=lavfi.signalstats.YAVG:file=$FIFO_Y:direct=1,split=2[g1][g2];\
[g1]drawgraph=m1=lavfi.signalstats.YAVG:fg1=0xFF5AD6FF:min=0:max=255:mode=line:slide=scroll:size=1200x${GH}:bg=0x00000000,scale=${GW}:${GH}[gr1];\
[g2]drawgraph=m1=lavfi.signalstats.YDIF:fg1=0xFF6AFF6A:min=0:max=40:mode=line:slide=scroll:size=1200x${GH}:bg=0x00000000,scale=${GW}:${GH}[gr2];"
exec "$FF" -nostdin -hide_banner -loglevel warning \
  -rtsp_transport tcp -i "$SRC" \
  -framerate 10 -loop 1 -f image2 -i "$OV_DIR/overlay.png" \
  -progress "$FIFO_P" -stats_period 1 \
  -filter_complex "[0:v]setpts=PTS-STARTPTS,split=2[main][sc];\
${SC}\
[1:v]setpts=PTS-STARTPTS[ovl];[main][ovl]overlay=0:0:shortest=1[o1];[o1][wf]overlay=${WFX}:${WFY}[o2];[o2][vs]overlay=${VSX}:${VSY}[o3];\
[o3][gr1]overlay=${GX}:${G1Y}[o4];[o4][gr2]overlay=${GX}:${G2Y}[o5];\
[o5]${CLOCK},${FX},format=nv12,hwupload[v]" \
  -vaapi_device /dev/dri/renderD128 \
  -map "[v]" -r $OUTFPS -c:v h264_vaapi -rc_mode VBR -b:v 6M -maxrate 10M -bufsize 10M -g $((OUTFPS * 2)) -an \
  "${SINK[@]}" 2>>"$LOG"
