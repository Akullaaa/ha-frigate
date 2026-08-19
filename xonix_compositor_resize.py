#!/usr/bin/env python3
"""Ксоникс-слой, вариант "миниатюра": читает сырые кадры (bgr24, WxH) живого
потока с stdin. На каждом кадре берёт ЦЕЛИКОМ ТОТ ЖЕ САМЫЙ кадр, уменьшает его
(с сохранением пропорций, через cv2.resize) до размера окошка и вставляет
получившуюся миниатюру в текущую позицию отскока — окошко показывает не
вырезку куска картинки (см. xonix_compositor.py, вариант "квадратик"), а всю
сцену целиком, просто маленькую. Источник и приёмник — один и тот же кадр, это
программная реализация "блита" (аппаратный VAAPI overlay/blend на этом Haswell
не поддерживается драйвером, см. project_tambur_hevc_playback). Пишет
результат на stdout.

Запускается как средний участник конвейера трёх процессов (см.
xonix_layer_resize.sh): ffmpeg (decode+scale) | этот скрипт | ffmpeg (h264_vaapi
-> RTSP push).
"""
import sys

import cv2
import numpy as np

W, H = 960, 540
RW, RH = 144, 81  # окошко-миниатюра, те же пропорции 16:9, что у канваса
SPEED_X, SPEED_Y = 1, 1  # шаг — 1 пиксель за кадр
RANGE_X, RANGE_Y = W - RW, H - RH
FRAME_SIZE = W * H * 3  # bgr24


def ball_pos(n: int) -> tuple[int, int]:
    x = abs((n * SPEED_X) % (2 * RANGE_X) - RANGE_X)
    y = abs((n * SPEED_Y) % (2 * RANGE_Y) - RANGE_Y)
    return x, y


def read_frame(stream) -> bytes | None:
    data = bytearray()
    while len(data) < FRAME_SIZE:
        chunk = stream.read(FRAME_SIZE - len(data))
        if not chunk:
            return None
        data.extend(chunk)
    return bytes(data)


def main() -> None:
    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer
    n = 0
    while True:
        raw = read_frame(stdin)
        if raw is None:
            break

        frame = np.frombuffer(raw, dtype=np.uint8).reshape(H, W, 3)
        thumb = cv2.resize(frame, (RW, RH), interpolation=cv2.INTER_AREA)

        cx, cy = ball_pos(n)
        frame = frame.copy()
        frame[cy:cy + RH, cx:cx + RW] = thumb

        stdout.write(frame.tobytes())
        stdout.flush()
        n += 1


if __name__ == "__main__":
    main()
