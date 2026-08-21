#!/usr/bin/env python3
"""Ксоникс-слой "все камеры": вложенные окна, но каждое — своя ЖИВАЯ камера,
а не увеличенная копия родителя (как в xonix_compositor_nested.py). Фон
(весь холст 960x540) — dvor. Внутри него, друг в друге, отскакивают два
окна поменьше — vorota, tambur. Итог — один поток, показывающий текущее
состояние сразу по нескольким стабильным камерам.

Не включена:
- spalnia — 404 через echo-прокси HA, нестабильна на момент реализации.

axis_2100 (2026-08-21) теперь ВКЛЮЧЕНА — см. четвёртое вложенное окно ниже.
Раньше не удавалось: у камеры multipart без Content-Length и с "голым" \n
вместо \r\n (не по HTTP-стандарту) — из-за этого и нативный Go-парсер
go2rtc, и обычный `ffmpeg -f mjpeg -i http://...` виснут (родной ffmpeg —
почти на пустом месте, до 30-секундного таймаута exec-продюсера go2rtc).
Решение — свой мост `axis_2100_bridge.py`: единственное HTTP-подключение к
камере, кадры вычленяются вручную по маркерам JPEG SOI/EOI (без опоры на
multipart-заголовки вообще) и отдаются в stdout уже как чистый raw-MJPEG,
который ffmpeg спокойно читает через `-f mjpeg -i pipe:0`. Тот же ffmpeg
дальше кодирует в h264 и пушит в go2rtc (см. axis_2100_bridge.sh) — именно
этот go2rtc-стрим `axis_2100` теперь и физическое подключение Frigate
(detect/record), и источник для этого композита: правило "одно физическое
подключение на камеру" не нарушено. Камера реально отдаёт ~1.2 кадра/сек
(старый Axis 2100, не "исходных ~25", как предполагалось раньше без
измерений) — мост апсемплит до постоянных 2fps (`fps=2` в ffmpeg-фильтре)
для стабильного таймстемпинга RTSP, а не потому что камера может больше.

В отличие от nested-варианта, здесь НЕТ рекурсивного cv2.resize на каждом
тике: каждая камера уже декодируется и масштабируется под точный размер
своего окна собственным ffmpeg (GPU scale_vaapi). Питон только двигает
окна и вставляет уже готовые по размеру кадры — дешевле, чем у одиночной
версии.

К каждой камере — РОВНО одно физическое подключение (правило проекта, не
нарушать): все источники ниже — localhost-рестримы go2rtc
(rtsp://127.0.0.1:8554/...), те же самые, к которым уже подключён Frigate
для detect/record, а не отдельные sub-потоки или второе прямое
подключение к камере. Из-за этого vorota/tambur декодируются здесь В
ПОЛНОМ разрешении (а не через _sub, как dvor, — dvor_sub существовал уже
давно, отдельным подключением, до этого скрипта, трогать не стали) —
аппаратного HEVC-декодирования на этом железе нет (Haswell/i965, только
программное), так что это реальная нагрузка на CPU, принято осознанно.

В отличие от xonix_layer.sh (там ffmpeg decode отдельным процессом в шелл-
пайпе), здесь Python сам порождает все decode-процессы через subprocess —
входов несколько, и bash-пайп с одним stdin на входе для этого не годится.
Второй ffmpeg (h264_vaapi encode → RTSP push) остаётся снаружи, в
xonix_layer_multicam.sh, как и раньше.

Каждая камера читается в отдельном потоке, хранится только САМЫЙ СВЕЖИЙ
кадр (с блокировкой) — если конкретная камера подвисла (у dvor и vorota
уже наблюдался дрейф MAC/IP с простоями), холст просто продолжает
показывать её последний кадр, не блокируя остальные и не падая целиком.
"""
import math
import random
import subprocess
import sys
import threading
import time

import numpy as np

FF = "/usr/lib/ffmpeg/7.0/bin/ffmpeg"
SPEED_MIN, SPEED_MAX = 1.0, 3.0
CANVAS_W, CANVAS_H = 960, 540
FPS = 12

# (имя, размер_окна, команда декодирования -> raw bgr24 нужного размера на stdout)
# dvor — фон, размер = весь холст; остальные — вложенные окна, от большего к меньшему.
CAMERAS = [
    ("dvor", (CANVAS_W, CANVAS_H), [
        FF, "-nostdin", "-loglevel", "warning", "-vaapi_device", "/dev/dri/renderD128",
        "-rtsp_transport", "tcp", "-i", "rtsp://127.0.0.1:8554/dvor_sub",
        "-vf", f"format=nv12,hwupload,scale_vaapi={CANVAS_W}:{CANVAS_H},hwdownload,format=nv12,format=bgr24",
        "-r", str(FPS), "-f", "rawvideo", "-",
    ]),
    ("vorota", (640, 360), [
        FF, "-nostdin", "-loglevel", "warning", "-vaapi_device", "/dev/dri/renderD128",
        "-rtsp_transport", "tcp", "-i", "rtsp://127.0.0.1:8554/vorota",
        "-vf", "format=nv12,hwupload,scale_vaapi=640:360,hwdownload,format=nv12,format=bgr24",
        "-r", str(FPS), "-f", "rawvideo", "-",
    ]),
    ("tambur", (400, 225), [
        FF, "-nostdin", "-loglevel", "warning", "-vaapi_device", "/dev/dri/renderD128",
        "-rtsp_transport", "tcp", "-i", "rtsp://127.0.0.1:8554/tambur",
        "-vf", "format=nv12,hwupload,scale_vaapi=400:225,hwdownload,format=nv12,format=bgr24",
        "-r", str(FPS), "-f", "rawvideo", "-",
    ]),
    ("axis_2100", (150, 200), [
        FF, "-nostdin", "-loglevel", "warning", "-vaapi_device", "/dev/dri/renderD128",
        "-rtsp_transport", "tcp", "-i", "rtsp://127.0.0.1:8554/axis_2100",
        "-vf", "format=nv12,hwupload,scale_vaapi=150:200,hwdownload,format=nv12,format=bgr24",
        "-r", str(FPS), "-f", "rawvideo", "-",
    ]),
]

# вложенные окна (без фона-dvor) — те же 2, в том же порядке, что CAMERAS[1:]
SIZES = [size for _, size, _ in CAMERAS[1:]]

latest_frames: dict[str, np.ndarray] = {}
lock = threading.Lock()


def rand_vel() -> tuple[float, float]:
    theta = random.uniform(0, 2 * math.pi)
    speed = random.uniform(SPEED_MIN, SPEED_MAX)
    return speed * math.cos(theta), speed * math.sin(theta)


def spawn() -> list[list[float]]:
    """levels[i] = [x, y, vx, vy] — позиция ОТНОСИТЕЛЬНО родителя (для i=0 —
    относительно канваса, т.е. абсолютная). Та же схема, что в
    xonix_compositor_nested.py, но со своим SIZES (3 камеры, не 4 копии)."""
    levels = []
    parent_w, parent_h = CANVAS_W, CANVAS_H
    for w, h in SIZES:
        x = random.uniform(0, parent_w - w)
        y = random.uniform(0, parent_h - h)
        vx, vy = rand_vel()
        levels.append([x, y, vx, vy])
        parent_w, parent_h = w, h
    return levels


def step(levels: list[list[float]]) -> None:
    parent_w, parent_h = CANVAS_W, CANVAS_H
    for i, lvl in enumerate(levels):
        w, h = SIZES[i]
        lvl[0] += lvl[2]
        lvl[1] += lvl[3]
        if lvl[0] <= 0:
            lvl[0] = 0
            lvl[2] = abs(lvl[2])
        elif lvl[0] >= parent_w - w:
            lvl[0] = parent_w - w
            lvl[2] = -abs(lvl[2])
        if lvl[1] <= 0:
            lvl[1] = 0
            lvl[3] = abs(lvl[3])
        elif lvl[1] >= parent_h - h:
            lvl[1] = parent_h - h
            lvl[3] = -abs(lvl[3])
        parent_w, parent_h = w, h


def absolute_positions(levels: list[list[float]]) -> list[tuple[int, int]]:
    positions = []
    ax, ay = 0.0, 0.0
    for lvl in levels:
        ax += lvl[0]
        ay += lvl[1]
        positions.append((int(round(ax)), int(round(ay))))
    return positions


def reader(name: str, size: tuple[int, int], cmd: list[str]) -> None:
    w, h = size
    frame_size = w * h * 3
    # чёрный кадр-заглушка, пока не пришёл первый настоящий
    with lock:
        latest_frames[name] = np.zeros((h, w, 3), dtype=np.uint8)

    while True:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        stdout = proc.stdout
        try:
            while True:
                data = bytearray()
                while len(data) < frame_size:
                    chunk = stdout.read(frame_size - len(data))
                    if not chunk:
                        raise EOFError
                    data.extend(chunk)
                frame = np.frombuffer(bytes(data), dtype=np.uint8).reshape(h, w, 3)
                with lock:
                    latest_frames[name] = frame
        except EOFError:
            pass
        finally:
            proc.kill()
            proc.wait()
        # камера отвалилась (например, тот самый дрейф MAC/IP у dvor/vorota) —
        # держим последний кадр, ждём и перезапускаем decode-процесс
        time.sleep(2)


def main() -> None:
    for name, size, cmd in CAMERAS:
        threading.Thread(target=reader, args=(name, size, cmd), daemon=True).start()

    # без искусственной паузы — первые кадры и так чёрные заглушки (reader
    # выставляет их сразу), ждать здесь нечего, а Frigate live-view не готов
    # долго терпеть тишину перед первым кадром (иначе уходит в "экономичный
    # режим")
    levels = spawn()
    stdout = sys.stdout.buffer
    period = 1.0 / FPS

    while True:
        t0 = time.monotonic()

        with lock:
            bg = latest_frames["dvor"].copy()
            windows = [latest_frames[name] for name, _, _ in CAMERAS[1:]]

        step(levels)
        positions = absolute_positions(levels)

        for win, (w, h), (x, y) in zip(windows, SIZES, positions):
            bg[y:y + h, x:x + w] = win

        stdout.write(bg.tobytes())
        stdout.flush()

        dt = period - (time.monotonic() - t0)
        if dt > 0:
            time.sleep(dt)


if __name__ == "__main__":
    main()
