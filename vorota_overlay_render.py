#!/usr/bin/env python3
"""Рендер прозрачного PNG-слоя с телеметрией для потока камеры ворот (vorota_overlay.sh).

Порт overlay_render.swift с iMac (/config/imac_camera в основном репозитории) на PIL — тот же дизайн,
что у iMac и XPS: три плашки-столбика с подгруппами у краёв кадра. Левый и средний — обычное дерево
слева направо, правый — зеркальное «JSON-дерево»: подписи у правого края, значения слева от них,
выровнены по общей вертикальной границе столбика.

Вход: DIR/telemetry.txt (пишет vorota_overlay_collect.py): "# Имя" — заголовок подгруппы (жёлтый, воздух
сверху); "- текст" — вложенный пункт; "[left]"/"[center]"/"[right]" — переключение столбика; прочие
маркеры [xx] игнорируются. Строка "Подпись: значение" рисуется двумя колонками.
Выход: DIR/overlay.png (RGBA WxH, атомарно через tmp+rename), DIR/clock_pos.txt («x y размер» для часов,
которые рисует ffmpeg фильтром drawtext поверх — как у iMac/XPS с флагом --noclock) и DIR/scopes_geom.txt
(геометрия блока скопов «Сигнал» внизу по центру: плашку и подписи рисует этот рендерер, сами
waveform/vectorscope/графики накладывает ffmpeg по этим координатам).
Аргументы: PID процесса-родителя (ffmpeg потока: исчез — выходим), --once (один рендер), --noclock.
Размер кадра и масштаб — переменные окружения OV_W, OV_H, OV_SCALE (по умолчанию 1920x2160, 1.5 —
шрифты iMac 17/18 px × 1.5, т.к. кадр в полтора раза шире 1280).
"""
import os
import sys
import time
from datetime import datetime

from PIL import Image, ImageDraw, ImageFont

DIR = os.environ.get("OV_DIR", "/tmp/vorota_overlay")
W = int(os.environ.get("OV_W", "1920"))
H = int(os.environ.get("OV_H", "2160"))
S = float(os.environ.get("OV_SCALE", "1.5"))
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"

pad = int(10 * S)          # отступ плашки от края кадра
box_pad = int(8 * S)       # внутренний отступ плашки
indent = int(14 * S)       # отступ строк первого уровня под заголовком; вложенные — вдвое
group_gap = int(6 * S)     # воздух перед заголовком подгруппы
value_gap = int(12 * S)    # зазор между значением и подписью в правом столбике
radius = int(8 * S)
body_font = ImageFont.truetype(FONT, int(17 * S))
head_font = ImageFont.truetype(FONT, int(18 * S))
stroke = max(1, int(2 * S / 1.5))
BODY = (255, 255, 255, 240)
LABEL = (209, 209, 209, 235)
HEAD = (255, 214, 89, 250)
PLATE = (0, 0, 0, 107)     # 42 % чёрного


def text_w(t, font):
    return font.getlength(t)


def line_h(font):
    a, d = font.getmetrics()
    return a + d + int(3 * S)


body_h = line_h(body_font)
head_h = line_h(head_font)


def read_columns(clock_text):
    cols = [[], [], []]
    cur = 0
    cols[0].append(("head", clock_text, 0))
    try:
        txt = open(f"{DIR}/telemetry.txt", encoding="utf-8").read()
    except OSError:
        txt = ""
    for raw in txt.split("\n"):
        l = raw.rstrip("\r")
        if not l:
            continue
        if l == "[left]":
            cur = 0; continue
        if l == "[center]":
            cur = 1; continue
        if l == "[right]":
            cur = 2; continue
        if l.startswith("[") and l.endswith("]"):
            continue
        if l.startswith("# "):
            cols[cur].append(("head", l[2:], 0))
        elif l.startswith("- "):
            cols[cur].append(("body", l[2:], 2))
        else:
            cols[cur].append(("body", l, 1))
    return cols


def layout(lines, rtl):
    items = []
    total_h = 0
    for i, (kind, s, lvl) in enumerate(lines):
        it = {"head": kind == "head", "text": "", "label": "", "value": "", "off": 0}
        if kind == "head":
            it["text"] = s
            total_h += head_h + (0 if i == 0 else group_gap)
        else:
            it["off"] = indent * (lvl - 1)
            if ": " in s:
                it["label"], it["value"] = s.split(": ", 1)
            else:
                it["text"] = s
                it["off"] = indent * lvl
            total_h += body_h
        items.append(it)
    boundary = 0
    for it in items:
        if it["label"]:
            boundary = max(boundary, it["off"] + text_w(it["label"], body_font))
    extent = 0
    for it in items:
        if it["head"]:
            extent = max(extent, text_w(it["text"], head_font))
        elif it["label"]:
            extent = max(extent, boundary + value_gap + text_w(it["value"], body_font))
        else:
            extent = max(extent, it["off"] + text_w(it["text"], body_font))
    box_w = 0 if not items else int(extent + 2 * box_pad)
    box_h = int(min(total_h + 2 * box_pad, H - 2 * pad))
    return {"items": items, "boundary": boundary, "box_w": box_w, "box_h": box_h, "rtl": rtl}


clock_pos_written = ""


def draw_col(d, L, box_x, clock_spacer=False, noclock=False):
    global clock_pos_written
    if not L["items"]:
        return
    d.rounded_rectangle([box_x, pad, box_x + L["box_w"], pad + L["box_h"]], radius=radius, fill=PLATE)
    edge = box_x + L["box_w"] - box_pad if L["rtl"] else box_x + box_pad

    def ax(rel, width):
        return edge - rel - width if L["rtl"] else edge + rel

    y = pad + box_pad
    for i, it in enumerate(L["items"]):
        if it["head"]:
            if i > 0:
                y += group_gap
            tw = text_w(it["text"], head_font)
            if clock_spacer and i == 0 and noclock:
                pos = f"{int(ax(0, tw))} {int(y)} {head_font.size}"
                if pos != clock_pos_written:
                    with open(f"{DIR}/clock_pos.txt.tmp", "w") as f:
                        f.write(pos + "\n")
                    os.replace(f"{DIR}/clock_pos.txt.tmp", f"{DIR}/clock_pos.txt")
                    clock_pos_written = pos
            else:
                d.text((ax(0, tw), y), it["text"], font=head_font, fill=HEAD, stroke_width=stroke, stroke_fill="black")
            y += head_h
        else:
            if it["label"]:
                lw = text_w(it["label"], body_font)
                vw = text_w(it["value"], body_font)
                d.text((ax(L["boundary"] - lw, lw), y), it["label"], font=body_font, fill=LABEL, stroke_width=stroke, stroke_fill="black")
                d.text((ax(L["boundary"] + value_gap, vw), y), it["value"], font=body_font, fill=BODY, stroke_width=stroke, stroke_fill="black")
            else:
                tw = text_w(it["text"], body_font)
                d.text((ax(it["off"], tw), y), it["text"], font=body_font, fill=BODY, stroke_width=stroke, stroke_fill="black")
            y += body_h
        if y > H - pad:
            break


# Блок скопов «Сигнал» внизу по центру (как на XPS): плашка + подписи здесь, содержимое — ffmpeg.
SC_W, SC_H = int(720 * S), int(150 * S)
SC_X, SC_Y = (W - SC_W) // 2, H - pad - SC_H
_in = int(8 * S)
_lab = body_h
WF_W, WF_H = int(320 * S), int(96 * S)
VS_S = int(96 * S)
G_W, G_H = int(248 * S), int(40 * S)
WF_X, WF_Y = SC_X + _in, SC_Y + _in + head_h + _lab
VS_X, VS_Y = WF_X + WF_W + _in, WF_Y
G_X = VS_X + VS_S + _in
G1_Y = WF_Y
G2_Y = G1_Y + G_H + _lab + int(2 * S)
scopes_written = False


def draw_scopes(d):
    global scopes_written
    d.rounded_rectangle([SC_X, SC_Y, SC_X + SC_W, SC_Y + SC_H], radius=radius, fill=PLATE)
    d.text((SC_X + _in, SC_Y + _in), "Сигнал", font=head_font, fill=HEAD, stroke_width=stroke, stroke_fill="black")
    d.text((WF_X, WF_Y - _lab), "яркость (waveform)", font=body_font, fill=LABEL, stroke_width=stroke, stroke_fill="black")
    d.text((VS_X, VS_Y - _lab), "цвет", font=body_font, fill=LABEL, stroke_width=stroke, stroke_fill="black")
    d.text((G_X, G1_Y - _lab), "освещённость, 1 мин", font=body_font, fill=LABEL, stroke_width=stroke, stroke_fill="black")
    d.text((G_X, G2_Y - _lab), "движение (YDIF), 1 мин", font=body_font, fill=LABEL, stroke_width=stroke, stroke_fill="black")
    if not scopes_written:
        geom = f"{WF_X} {WF_Y} {WF_W} {WF_H} {VS_X} {VS_Y} {VS_S} {G_X} {G1_Y} {G2_Y} {G_W} {G_H}"
        with open(f"{DIR}/scopes_geom.txt.tmp", "w") as f:
            f.write(geom + "\n")
        os.replace(f"{DIR}/scopes_geom.txt.tmp", f"{DIR}/scopes_geom.txt")
        scopes_written = True


def render(noclock):
    now = datetime.now()
    clock = now.strftime("%Y-%m-%d  %H:%M:%S.") + f"{now.microsecond // 100:04d}"
    cols = read_columns(clock)
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    Ls = [layout(cols[0], False), layout(cols[1], False), layout(cols[2], True)]
    used = sum(L["box_w"] for L in Ls)
    gap = max(4, (W - 2 * pad - used) / 2)
    draw_col(d, Ls[0], pad, clock_spacer=True, noclock=noclock)
    draw_col(d, Ls[1], int(pad + Ls[0]["box_w"] + gap))
    draw_col(d, Ls[2], W - pad - Ls[2]["box_w"])
    draw_scopes(d)
    tmp, dst = f"{DIR}/overlay.png.tmp", f"{DIR}/overlay.png"
    img.save(tmp, "PNG", compress_level=1)
    os.replace(tmp, dst)


def main():
    args = sys.argv[1:]
    once = "--once" in args
    noclock = "--noclock" in args
    watch = next((int(a) for a in args if a.isdigit()), 0)
    os.makedirs(DIR, exist_ok=True)
    while True:
        try:
            render(noclock)
        except Exception as e:  # не падать из-за одного кривого файла телеметрии
            print("render:", e, file=sys.stderr)
        if once:
            break
        if watch and not os.path.exists(f"/proc/{watch}"):
            break
        time.sleep(0.5)


if __name__ == "__main__":
    main()
