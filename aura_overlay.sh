#!/bin/bash
# exec-источник go2rtc: поток камеры ворот с оверлеем телеметрии — тот же дизайн, что у XPS и iMac
# (плашки-столбики с подгруппами, часы с долями секунды, индикаторы частоты кадров, блок скопов
# «Сигнал»: waveform, vectorscope, бегущие графики освещённости и движения).
# Конвейер: rtsp xm530 (H.265 1920x2160@20, программный декод — HEVC на этом Intel/i965 в VAAPI нет)
#   → [режим 130: scale до 960x1080] → overlay PNG-слоя (xm530_overlay_render.py, данные —
#   xm530_overlay_collect.py) → скопы (ffmpeg сам, по геометрии из scopes_geom.txt рендерера)
#   → fps=N → drawtext (часы, ● по кругу, █ по низу, номер кадра) → hwupload → h264_vaapi → RTSP {output}.
# Режимы (второй аргумент): 60 — 1280x1440 @ 60 к/с (основной live «XM530» по просьбе пользователя);
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
# Режимы 60/44/77/full/130 go2rtc запускает только пока кто-то смотрит (exec-источники по требованию).
# 2026-09-15 (вечер), по решению пользователя: режим base — 960x1080 @ 13 к/с (камера даёт 20, fps= дублирует), 3 Мбит/с — это САМ
# базовый поток go2rtc «xm530», из которого Frigate берёт detect и record (-c:v copy), так что оверлей есть
# в записях и в экономичном jsmpeg-режиме плеера всегда; работает постоянно, пока жив Frigate. Чистый поток
# камеры переименован в «xm530_cam» — вход для всех режимов здесь и для остальных xm530_*-скриптов.
set -e
export LC_ALL=C.UTF-8
FF=/usr/lib/ffmpeg/7.0/bin/ffmpeg
OUT="$1"
MODE="${2:-full}"
# 2026-09-15 («аура», термин пользователя): скрипт общий для камер, третий аргумент — имя камеры go2rtc (xm530 по
# умолчанию; xm530_overlay.sh — обёртка ради старых записей конфига). Чистый поток — <cam>_cam, каталог режима
# /tmp/<cam>_overlay_<режим>, лог logs/<cam>_overlay_<режим>.log, сборщику имя камеры идёт через OV_CAM.
# tambur: кадр 16:9 → 1280x720, три колонки (как у iMac), масштаб 1.35, скопы внизу по центру, звука у камеры нет.
CAM="${3:-xm530}"
export OV_CAM=$CAM
AUDIO=1   # ветка astats по аудиодорожке камеры; у tambur дорожки нет — ffmpeg упал бы на [0:a]
case "$MODE" in
  # base — базовый поток xm530 (detect/record/live по умолчанию): 13 к/с (было 34 → 25 → 13, по просьбе пользователя). b<N> — те же настройки (960x1080, шрифт 1.3,
  # два списка) с другой частотой, отдельные потоки go2rtc xm530_b<N> только для просмотра, в запись не идут
  # (пробовали 25/34/43/60/70/90: CPU ffmpeg 85 % до 34, 93 % на 60, 104 % на 90; на 90 полосы при просмотре с Debian).
  base|b[0-9]*)
    case "$CAM" in
      tambur) export OV_W=960 OV_H=540 OV_SCALE=1.0 OV_SCOPE_X=center; PRE="scale=960:540,"; BV=2M; MAXR=4M; SCS=0.55; OUTFPS=25; AUDIO=0;;   # 19:20: 960x540 (было 1280x720, S 1.35) — снять нагрузку без потери ауры
      x2_wq_bl) export OV_W=960 OV_H=540 OV_SCALE=0.85 OV_SCOPE_X=center; PRE="scale=960:540,"; BV=2M; MAXR=4M; SCS=0.55; OUTFPS=25;;   # 19:20: 960x540 (было 1280x720, S 1.25); камера переведена на 15 к/с по DVRIP   # двор: HEVC 1080p@25 + PCMA, DVRIP есть; 1.25 — иначе Frigate внизу слева не влезает под DVRIP-строки
      axis_2100) export OV_W=960 OV_H=1280 OV_SCALE=1.6 OV_TWO_LISTS=1; PRE="scale=960:1280,"; BV=2M; MAXR=4M; SCS=0.3; OUTFPS=25; AUDIO=0;;   # axis: мост даёт H.264 480x640 (~7 к/с), апскейл ×2, два списка как у xm530, без звука
      *)      export OV_W=960 OV_H=1080 OV_SCALE=1.6 OV_TWO_LISTS=1; PRE="scale=960:1080,"; BV=3M; MAXR=6M; SCS=0.3; OUTFPS=13;;
    esac
    [ "$MODE" != base ] && OUTFPS=${MODE#b};;
  130)    export OV_W=960 OV_H=1080 OV_SCALE=0.9; OUTFPS=130; PRE="scale=960:1080,";;
  60)     export OV_W=1280 OV_H=1440 OV_SCALE=1.2; OUTFPS=60; PRE="scale=1280:1440,";;
  44)     export OV_W=1440 OV_H=1620 OV_SCALE=1.35; OUTFPS=44; PRE="scale=1440:1620,";;
  77)     export OV_W=1152 OV_H=1296 OV_SCALE=1.08; OUTFPS=77; PRE="scale=1152:1296,";;
  *)      export OV_W=1920 OV_H=2160 OV_SCALE=1.8; OUTFPS=30; PRE="";;
esac
BV=${BV:-6M}; MAXR=${MAXR:-10M}   # битрейт кодера: у base ниже — он идёт в запись постоянно
export OV_FPS=$OUTFPS   # рендереру: кольцо из OUTFPS точек по орбите шарика (2026-09-15, по просьбе пользователя)
export OV_DIR=/tmp/${CAM}_overlay_$MODE
export OV_SCOPE_SCALE=$(awk -v s="$OV_SCALE" -v k="${SCS:-0.75}" 'BEGIN{printf "%.3f", s*k}')
export GO2RTC_AUTH="alena:Rm9g8fsLobAS6I2jfoEY"
SRC="rtsp://alena:Rm9g8fsLobAS6I2jfoEY@127.0.0.1:8554/${CAM}_cam"   # чистый поток камеры (до 2026-09-15 у xm530 назывался xm530)
FONT=/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf
mkdir -p /config/logs; LOG=/config/logs/${CAM}_overlay_$MODE.log   # 2026-09-15: в /config (виден из аддона Studio Code Server как /addon_configs/ccab4aaf_frigate-fa/logs), был /tmp контейнера
# размеры индикаторов drawtext — от масштаба шрифта (в единицах iMac: 21/12/15 px при S=1)
F1=$(awk -v s="$OV_SCALE" 'BEGIN{printf "%d", 21*s}'); F2=$(awk -v s="$OV_SCALE" 'BEGIN{printf "%d", 12*s}'); F3=$(awk -v s="$OV_SCALE" 'BEGIN{printf "%d", 15*s}')
mkdir -p "$OV_DIR"
rm -f "$OV_DIR"/frame.txt "$OV_DIR"/yavg.txt "$OV_DIR"/scopes_geom.txt "$OV_DIR"/clock_pos.txt "$OV_DIR"/overlay.png
python3 /config/aura_overlay_collect.py $$ 2>>"$LOG" &
( while kill -0 $$ 2>/dev/null; do python3 /config/aura_overlay_render.py $$ --noclock 2>>"$LOG"; sleep 1; done ) &
FIFO_P="$OV_DIR/progress.fifo"; FIFO_Y="$OV_DIR/yavg.fifo"; FIFO_A="$OV_DIR/audio.fifo"
rm -f "$FIFO_P" "$FIFO_Y" "$FIFO_A" "$OV_DIR/audio.txt"; mkfifo "$FIFO_P" "$FIFO_Y" "$FIFO_A"
# Читатели переоткрывают канал после EOF (ffmpeg открывает файл metadata дважды при перестройке графа).
( fr=""; while kill -0 $$ 2>/dev/null; do while IFS="=" read -r k v; do
    case "$k" in frame) fr="$v";; out_time_us) echo "$fr $v" > "$OV_DIR/frame.txt.tmp" && mv -f "$OV_DIR/frame.txt.tmp" "$OV_DIR/frame.txt";; esac
  done < "$FIFO_P"; sleep 0.2; done ) &
( while kill -0 $$ 2>/dev/null; do while read -r line; do case "$line" in *YAVG=*) echo "${line#*YAVG=}" > "$OV_DIR/yavg.txt.tmp" && mv -f "$OV_DIR/yavg.txt.tmp" "$OV_DIR/yavg.txt";; esac; done < "$FIFO_Y"; sleep 0.2; done ) &
# уровень звука камеры (2026-09-15): astats по аудиодорожке того же RTSP → RMS dB раз в ~1 с → audio.txt
( while kill -0 $$ 2>/dev/null; do while read -r line; do case "$line" in *RMS_level=*) echo "${line#*RMS_level=}" > "$OV_DIR/audio.txt.tmp" && mv -f "$OV_DIR/audio.txt.tmp" "$OV_DIR/audio.txt";; esac; done < "$FIFO_A"; sleep 0.2; done ) &
# первый кадр слоя и геометрия должны лежать на диске до старта ffmpeg
for i in $(seq 1 40); do [ -s "$OV_DIR/overlay.png" ] && [ -s "$OV_DIR/scopes_geom.txt" ] && [ -s "$OV_DIR/clock_pos.txt" ] && break; sleep 0.25; done
read -r CX CY CFS < "$OV_DIR/clock_pos.txt" 2>/dev/null || { CX=24; CY=24; CFS=27; }
read -r WFX WFY WFW WFH VSX VSY VSS GX G1Y G2Y GW GH < "$OV_DIR/scopes_geom.txt" || [ -n "$GH" ]
case "$OUT" in
  rtsp*) SINK=(-f rtsp -rtsp_transport tcp "$OUT");;
  *)     SINK=(-t 6 -y "$OUT");;
esac
echo "=== старт $(date '+%F %T') режим $MODE → $OUT" >> "$LOG"
DT="drawtext=fontfile=$FONT:fontcolor=0xFFD65A@0.7:borderw=1:bordercolor=black@0.9:shadowcolor=black@0.6:shadowx=1:shadowy=1"   # 2026-09-15: полупрозрачный текст, как в PNG-слое; контур контрастнее
CLOCK="$DT:fontsize=$CFS:x=$CX:y=$CY:text='%{localtime\:%T.%4N} (%{n})'"   # без даты с 16:10 (см. рендерер)   # 2026-09-15: счётчик кадров в скобках после долей секунды (по просьбе пользователя), отдельной подписи «кадр N» больше нет
# ● по кругу, вписанному в ширину кадра: один оборот в секунду, за кадр — ровно одно из OUTFPS положений
# (77 точек в потоке 77 к/с, 60 в 60 и т.д.; угол от номера кадра n, не от t). Хвост кометы (2026-09-15, по
# просьбе пользователя): TAILN точек вдоль дуги до ПРЕДЫДУЩЕГО положения (n-1), убывающие по размеру и
# прозрачности. █ по нижнему краю (проход 2 с); номер кадра — в строке часов (CLOCK).
R="(w/2-$F1)"
TAILN=${OV_TAIL:-6}; TAIL=""   # OV_TAIL=0 — без хвоста (для замеров)
for j in $(seq 1 $TAILN); do
  fs=$(awk -v f="$F1" -v j="$j" -v n="$TAILN" 'BEGIN{printf "%d", f*(1-0.7*j/n)}')
  al=$(awk -v j="$j" -v n="$TAILN" 'BEGIN{printf "%.2f", 0.75*(1-j/(n+1))}')
  TAIL="$TAIL$DT:fontsize=$fs:fontcolor=0xFFD65A@$al:x='w/2+$R*cos(2*PI*(n-$j/$TAILN)/$OUTFPS)-$fs/3':y='h/2+$R*sin(2*PI*(n-$j/$TAILN)/$OUTFPS)-$fs/2':text='●',"
done
FX="${TAIL}$DT:fontsize=$F1:x='w/2+$R*cos(2*PI*n/$OUTFPS)-$F1/3':y='h/2+$R*sin(2*PI*n/$OUTFPS)-$F1/2':text='●',$DT:fontsize=$F2:x='mod(t*w/2\,w)-$F2/4':y=h-$F2:text='█'"
# Скопы на частоте потока по копии 320x180: waveform / vectorscope (центр 96x96 из 256x256) / два
# drawgraph по signalstats (освещённость YAVG, движение YDIF; ширина 1200 px = ~минута при 20 к/с,
# затем scale до плашки), прозрачные (colorkey по чёрному, 85 %). YAVG для сборщика — из той же ветки.
SC="[sc]scale=320:180,format=yuv420p,split=3[w][v][g];\
[w]waveform=mode=column:intensity=0.12:scale=ire,format=rgba,scale=${WFW}:${WFH},colorkey=black:0.12:0.05,colorchannelmixer=aa=0.85[wf];\
[v]vectorscope=mode=color3:intensity=0.2,format=rgba,crop=96:96:80:80,scale=${VSS}:${VSS},colorkey=black:0.12:0.05,colorchannelmixer=aa=0.85[vs];\
[g]signalstats,metadata=print:key=lavfi.signalstats.YAVG:file=$FIFO_Y:direct=1,split=2[g1][g2];\
[g1]drawgraph=m1=lavfi.signalstats.YAVG:fg1=0xFF5AD6FF:min=0:max=255:mode=line:slide=scroll:size=1200x${GH}:bg=0x00000000,scale=${GW}:${GH}[gr1];\
[g2]drawgraph=m1=lavfi.signalstats.YDIF:fg1=0xFF6AFF6A:min=0:max=40:mode=line:slide=scroll:size=1200x${GH}:bg=0x00000000,scale=${GW}:${GH}[gr2];"
# звук: ветка без выхода (anullsink), только метаданные astats в FIFO; reset=25 кадров PCMA ≈ 1 с
AU="[0:a]astats=metadata=1:reset=25:measure_perchannel=none:measure_overall=RMS_level,ametadata=print:key=lavfi.astats.Overall.RMS_level:file=$FIFO_A:direct=1,anullsink;"
[ "$AUDIO" = 1 ] || AU=""   # у камеры без звука ветки нет
exec "$FF" -nostdin -hide_banner -loglevel warning \
  -rtsp_transport tcp -i "$SRC" \
  -re -thread_queue_size 64 -framerate 10 -loop 1 -f image2 -i "$OV_DIR/overlay.png" \
  -progress "$FIFO_P" -stats_period 1 \
  -filter_complex "[0:v]setpts=PTS-STARTPTS,${PRE}split=2[main][sc];\
${SC}${AU}\
[1:v]setpts=PTS-STARTPTS[ovl];[main][ovl]overlay=0:0:shortest=1[o1];[o1][wf]overlay=${WFX}:${WFY}[o2];[o2][vs]overlay=${VSX}:${VSY}[o3];\
[o3][gr1]overlay=${GX}:${G1Y}[o4];[o4][gr2]overlay=${GX}:${G2Y}[o5];\
[o5]fps=${OUTFPS},${CLOCK},${FX},format=nv12,hwupload[v]" \
  -vaapi_device /dev/dri/renderD128 \
  -map "[v]" -c:v h264_vaapi -rc_mode VBR -b:v $BV -maxrate $MAXR -bufsize $MAXR -g $((OUTFPS * 2)) -bf 0 -an \
  "${SINK[@]}" 2>>"$LOG"
