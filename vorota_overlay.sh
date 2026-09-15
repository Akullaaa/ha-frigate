#!/bin/bash
# exec-источник go2rtc: поток камеры ворот с оверлеем телеметрии — тот же дизайн, что у XPS и iMac
# (плашки-столбики с подгруппами, часы с долями секунды, индикаторы частоты кадров, блок скопов
# «Сигнал»: waveform, vectorscope, бегущие графики освещённости и движения).
# Конвейер: rtsp vorota (H.265 1920x2160@20, программный декод — HEVC на этом Intel/i965 в VAAPI нет)
#   → [режим 130: scale до 960x1080] → overlay PNG-слоя (vorota_overlay_render.py, данные —
#   vorota_overlay_collect.py) → скопы (ffmpeg сам, по геометрии из scopes_geom.txt рендерера)
#   → fps=N → drawtext (часы, ● по кругу, █ по низу, номер кадра) → hwupload → h264_vaapi → RTSP {output}.
# Режимы (второй аргумент): 60 — 1280x1440 @ 60 к/с (основной live «Vorota» по просьбе пользователя);
# 77 — 1152x1296 @ 77 к/с (по просьбе; 1280x1440@77 в замере отставал сильнее); full — 1920x2160 @ 30 к/с;
# 130 — 960x1080 @ 130 к/с как у iMac. Каждый режим — отдельный поток go2rtc, так пользователю удобнее выбирать. Замер на реальном потоке (6 с вывода):
# 1920x2160@60 — 13,9 с (≈0,55x, не тянет), 1440x1620@60 — 10,2–10,8 с (отстаёт), 1280x1440@60 — 9,2–9,7 с
# (реальное время, как full@30 и 960x1080@130).
# Замер 2026-09-15 (testsrc2, h264_vaapi Haswell): 1920x2160 fps=130 — 0,48x даже без текста, fps=60 — 1,08x,
# fps=30 + 3 drawtext — 1,85x; 960x1080 fps=130 + 3 drawtext — 1,52x; 1280x1440@130 — 0,93x. Упор — кодер
# VAAPI + hwupload, не drawtext. Поэтому 130 к/с только на половинном кадре.
# Сборщик и рендерер живут, пока жив PID этого скрипта (после exec — сам ffmpeg): go2rtc, останавливая
# поток, убивает ffmpeg, фоновые циклы видят это и выходят. Живой fps/освещённость — через именованные
# каналы (-progress и metadata=print), как у iMac. Отладка: первый аргумент-путь .mp4 — 6 с в файл вместо RTSP.
# Грабли (2026-09-15): ветки скопов с РАЗНЫМИ частотами (fps=1/10/4 + overlay) намертво вешали граф
# (память росла, metadata не писались) — все скопы считаются на частоте потока по копии 320x180, а
# минута истории у drawgraph получается шириной 1200 px со scale до размера плашки. Юникод в drawtext
# (●, █, «кадр») требует LC_ALL=C.UTF-8 — без него в exec-окружении go2rtc/docker текст превращается в «???».
# Файлы clock_pos/scopes_geom — с переводом строки, иначе read под set -e возвращает ошибку на EOF.
# Запускается go2rtc только пока кто-то смотрит этот поток (exec-источники по требованию); запись камеры
# идёт отдельно и без оверлея.
set -e
export LC_ALL=C.UTF-8
FF=/usr/lib/ffmpeg/7.0/bin/ffmpeg
OUT="$1"
MODE="${2:-full}"
case "$MODE" in
  130)    export OV_W=960 OV_H=1080 OV_SCALE=0.9; OUTFPS=130; PRE="scale=960:1080,";;
  60)     export OV_W=1280 OV_H=1440 OV_SCALE=1.2; OUTFPS=60; PRE="scale=1280:1440,";;
  77)     export OV_W=1152 OV_H=1296 OV_SCALE=1.08; OUTFPS=77; PRE="scale=1152:1296,";;
  *)      export OV_W=1920 OV_H=2160 OV_SCALE=1.8; OUTFPS=30; PRE="";;
esac
export OV_DIR=/tmp/vorota_overlay_$MODE
export OV_SCOPE_SCALE=$(awk -v s="$OV_SCALE" 'BEGIN{printf "%.3f", s*0.75}')
export GO2RTC_AUTH="alena:Rm9g8fsLobAS6I2jfoEY"
SRC="rtsp://alena:Rm9g8fsLobAS6I2jfoEY@127.0.0.1:8554/vorota"
FONT=/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf
LOG=/tmp/vorota_overlay_$MODE.log
# размеры индикаторов drawtext — от масштаба шрифта (в единицах iMac: 21/12/15 px при S=1)
F1=$(awk -v s="$OV_SCALE" 'BEGIN{printf "%d", 21*s}'); F2=$(awk -v s="$OV_SCALE" 'BEGIN{printf "%d", 12*s}'); F3=$(awk -v s="$OV_SCALE" 'BEGIN{printf "%d", 15*s}')
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
# первый кадр слоя и геометрия должны лежать на диске до старта ffmpeg
for i in $(seq 1 40); do [ -s "$OV_DIR/overlay.png" ] && [ -s "$OV_DIR/scopes_geom.txt" ] && [ -s "$OV_DIR/clock_pos.txt" ] && break; sleep 0.25; done
read -r CX CY CFS < "$OV_DIR/clock_pos.txt" 2>/dev/null || { CX=24; CY=24; CFS=27; }
read -r WFX WFY WFW WFH VSX VSY VSS GX G1Y G2Y GW GH < "$OV_DIR/scopes_geom.txt" || [ -n "$GH" ]
case "$OUT" in
  rtsp*) SINK=(-f rtsp -rtsp_transport tcp "$OUT");;
  *)     SINK=(-t 6 -y "$OUT");;
esac
echo "=== старт $(date '+%F %T') режим $MODE → $OUT" >> "$LOG"
DT="drawtext=fontfile=$FONT:fontcolor=0xFFD65A:shadowcolor=black@0.8:shadowx=1:shadowy=1"
CLOCK="$DT:fontsize=$CFS:x=$CX:y=$CY:text='%{localtime\:%F  %T.%4N}'"
# ● по кругу, вписанному в ширину кадра: один оборот в секунду, за кадр — ровно одно из OUTFPS положений
# (77 точек в потоке 77 к/с, 60 в 60 и т.д.; угол от номера кадра n, не от t). Хвост кометы (2026-09-15, по
# просьбе пользователя): TAILN точек вдоль дуги до ПРЕДЫДУЩЕГО положения (n-1), убывающие по размеру и
# прозрачности. █ по нижнему краю (проход 2 с), номер кадра — слева от скопов.
R="(w/2-$F1)"
TAILN=6; TAIL=""
for j in $(seq 1 $TAILN); do
  fs=$(awk -v f="$F1" -v j="$j" -v n="$TAILN" 'BEGIN{printf "%d", f*(1-0.7*j/n)}')
  al=$(awk -v j="$j" -v n="$TAILN" 'BEGIN{printf "%.2f", 0.75*(1-j/(n+1))}')
  TAIL="$TAIL$DT:fontsize=$fs:fontcolor=0xFFD65A@$al:x='w/2+$R*cos(2*PI*(n-$j/$TAILN)/$OUTFPS)-$fs/3':y='h/2+$R*sin(2*PI*(n-$j/$TAILN)/$OUTFPS)-$fs/2':text='●',"
done
FX="${TAIL}$DT:fontsize=$F1:x='w/2+$R*cos(2*PI*n/$OUTFPS)-$F1/3':y='h/2+$R*sin(2*PI*n/$OUTFPS)-$F1/2':text='●',$DT:fontsize=$F2:x='mod(t*w/2\,w)-$F2/4':y=h-$F2:text='█',$DT:fontsize=$F3:x=${WFX}-tw-$((F3 / 2)):y=h-$((F3 * 5 / 2)):text='кадр %{n}'"
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
  -re -thread_queue_size 64 -framerate 10 -loop 1 -f image2 -i "$OV_DIR/overlay.png" \
  -progress "$FIFO_P" -stats_period 1 \
  -filter_complex "[0:v]setpts=PTS-STARTPTS,${PRE}split=2[main][sc];\
${SC}\
[1:v]setpts=PTS-STARTPTS[ovl];[main][ovl]overlay=0:0:shortest=1[o1];[o1][wf]overlay=${WFX}:${WFY}[o2];[o2][vs]overlay=${VSX}:${VSY}[o3];\
[o3][gr1]overlay=${GX}:${G1Y}[o4];[o4][gr2]overlay=${GX}:${G2Y}[o5];\
[o5]fps=${OUTFPS},${CLOCK},${FX},format=nv12,hwupload[v]" \
  -vaapi_device /dev/dri/renderD128 \
  -map "[v]" -c:v h264_vaapi -rc_mode VBR -b:v 6M -maxrate 10M -bufsize 10M -g $((OUTFPS * 2)) -an \
  "${SINK[@]}" 2>>"$LOG"
