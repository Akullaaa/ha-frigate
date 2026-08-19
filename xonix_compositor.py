#!/usr/bin/env python3
"""Ксоникс-слой: читает сырые кадры (bgr24, WxH) живого потока с stdin. На каждом
кадре копирует квадратный кусок ИЗ ТОГО ЖЕ САМОГО кадра — из позиции, где кубик
находился бы 7 шагов (кадров) назад по формуле отскока — в текущую позицию
кубика. Источник и приёмник — один буфер, это и есть программная реализация
"блита" (раз аппаратный VAAPI overlay/blend на этом Haswell недоступен, см.
project_tambur_hevc_playback). Пишет результат на stdout.

Запускается как средний участник конвейера трёх процессов (см. xonix_layer.sh):
  ffmpeg (decode+scale) | xonix_compositor.py | ffmpeg (h264_vaapi -> RTSP push)
"""
import sys

import numpy as np

W, H = 960, 540
SQ = 36  # сторона квадратика — уменьшена в 4 раза относительно исходных 146
DELAY = 7  # "шагов" (кадров) назад — источник копирования на той же картинке
SPEED_X, SPEED_Y = 1, 1  # шаг — 1 пиксель за кадр
RANGE_X, RANGE_Y = W - SQ, H - SQ
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

        sx, sy = ball_pos(max(0, n - DELAY))
        cx, cy = ball_pos(n)

        patch = frame[sy:sy + SQ, sx:sx + SQ].copy()  # copy — источник и приёмник могут пересекаться
        frame = frame.copy()
        frame[cy:cy + SQ, cx:cx + SQ] = patch

        stdout.write(frame.tobytes())
        stdout.flush()
        n += 1


if __name__ == "__main__":
    main()
