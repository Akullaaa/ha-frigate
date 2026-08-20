#!/usr/bin/env python3
"""Ксоникс-слой, вариант "тринадцать кубиков": то же, что xonix_compositor_
multi7.py (окошки-миниатюры cv2.resize, физика AABB-столкновений, случайный
старт), но 13 объектов вдвое меньшего размера, и скорость задаётся не по
осям (±speed, ±speed — это всегда ровно 45°), а случайным УГЛОМ и модулем:
vx = speed*cos(θ), vy = speed*sin(θ), θ равномерно на [0, 2π) — отсюда
настоящее разнообразие направлений, а не только диагонали. Состояние
(позиции/скорости, теперь float) живёт и обновляется кадр за кадром. См.
project_tambur_hevc_playback про историю Ксоникса и почему вставка на CPU.

Запускается как средний участник конвейера трёх процессов (см.
xonix_layer.sh, третий параметр = путь к этому файлу):
  ffmpeg (decode+scale) | этот скрипт | ffmpeg (h264_vaapi -> RTSP push)
"""
import math
import random
import sys

import cv2
import numpy as np

W, H = 960, 540
RW, RH = 24, 14  # вдвое меньше, чем в xonix_compositor_multi7.py (48x27)
N = 13
SPEED_MIN, SPEED_MAX = 1.0, 3.0
FRAME_SIZE = W * H * 3  # bgr24


def overlaps(pos, box):
    x, y = pos
    bx, by = box[0], box[1]
    return x < bx + RW and x + RW > bx and y < by + RH and y + RH > by


def spawn_boxes():
    boxes = []  # каждый: [x, y, vx, vy] — всё float
    for _ in range(N):
        for _attempt in range(50):
            x = random.uniform(0, W - RW)
            y = random.uniform(0, H - RH)
            if all(not overlaps((x, y), b) for b in boxes):
                break
        theta = random.uniform(0, 2 * math.pi)
        speed = random.uniform(SPEED_MIN, SPEED_MAX)
        vx = speed * math.cos(theta)
        vy = speed * math.sin(theta)
        boxes.append([x, y, vx, vy])
    return boxes


def step(boxes):
    for b in boxes:
        b[0] += b[2]
        b[1] += b[3]
        if b[0] <= 0:
            b[0] = 0
            b[2] = abs(b[2])
        elif b[0] >= W - RW:
            b[0] = W - RW
            b[2] = -abs(b[2])
        if b[1] <= 0:
            b[1] = 0
            b[3] = abs(b[3])
        elif b[1] >= H - RH:
            b[1] = H - RH
            b[3] = -abs(b[3])

    for i in range(N):
        for j in range(i + 1, N):
            a, b = boxes[i], boxes[j]
            ox = min(a[0] + RW, b[0] + RW) - max(a[0], b[0])
            oy = min(a[1] + RH, b[1] + RH) - max(a[1], b[1])
            if ox <= 0 or oy <= 0:
                continue
            if ox < oy:
                push = ox / 2 + 0.5
                if a[0] < b[0]:
                    a[0] -= push
                    b[0] += push
                else:
                    a[0] += push
                    b[0] -= push
                a[2], b[2] = -a[2], -b[2]
            else:
                push = oy / 2 + 0.5
                if a[1] < b[1]:
                    a[1] -= push
                    b[1] += push
                else:
                    a[1] += push
                    b[1] -= push
                a[3], b[3] = -a[3], -b[3]
            a[0] = max(0, min(W - RW, a[0]))
            a[1] = max(0, min(H - RH, a[1]))
            b[0] = max(0, min(W - RW, b[0]))
            b[1] = max(0, min(H - RH, b[1]))


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
    boxes = spawn_boxes()

    while True:
        raw = read_frame(stdin)
        if raw is None:
            break

        frame = np.frombuffer(raw, dtype=np.uint8).reshape(H, W, 3)
        thumb = cv2.resize(frame, (RW, RH), interpolation=cv2.INTER_AREA)

        step(boxes)

        frame = frame.copy()
        for x, y, _vx, _vy in boxes:
            xi, yi = int(round(x)), int(round(y))
            frame[yi:yi + RH, xi:xi + RW] = thumb

        stdout.write(frame.tobytes())
        stdout.flush()


if __name__ == "__main__":
    main()
