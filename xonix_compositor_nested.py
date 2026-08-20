#!/usr/bin/env python3
"""Ксоникс-слой, вариант "вложенные окна": четыре окошка-миниатюры вложены
друг в друга (0 — самое большое/внешнее, 3 — самое маленькое/внутреннее), но
теперь КАЖДОЕ двигается и отскакивает НЕЗАВИСИМО В ГРАНИЦАХ РОДИТЕЛЯ — окно 0
отскакивает от стен канваса, окно 1 отскакивает от границ ТЕКУЩЕГО положения
окна 0, окно 2 — от границ окна 1, окно 3 — от границ окна 2. Это иерархическая
система (не общий жёсткий центр, как в первой версии) — у каждого уровня своя
позиция (относительно родителя) и своя скорость.

Порядок отрисовки даёт рекурсивный эффект "зеркало в зеркале": сначала берём
cv2.resize от ИСХОДНОГО кадра до размера окна 0 и вставляем — следующий resize
делается уже с ЭТОГО изменённого кадра (где окно 0 уже вставлено), и так для
каждого следующего окна.

Движение — случайный угол и модуль скорости, случайный старт при каждом
запуске. См. project_tambur_hevc_playback про историю Ксоникса и почему сама
вставка на CPU, а не на GPU.

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
# размеры вложенных окон, от внешнего к внутреннему — увеличены в 4 раза
# относительно первой версии (192,108)/(128,72)/(80,45)/(48,27)
SIZES = [(768, 432), (512, 288), (320, 180), (192, 108)]
SPEED_MIN, SPEED_MAX = 1.0, 3.0
FRAME_SIZE = W * H * 3  # bgr24


def rand_vel() -> tuple[float, float]:
    theta = random.uniform(0, 2 * math.pi)
    speed = random.uniform(SPEED_MIN, SPEED_MAX)
    return speed * math.cos(theta), speed * math.sin(theta)


def spawn() -> list[list[float]]:
    """levels[i] = [x, y, vx, vy] — позиция ОТНОСИТЕЛЬНО родителя (для i=0 —
    относительно канваса, т.е. абсолютная)."""
    levels = []
    parent_w, parent_h = W, H
    for w, h in SIZES:
        x = random.uniform(0, parent_w - w)
        y = random.uniform(0, parent_h - h)
        vx, vy = rand_vel()
        levels.append([x, y, vx, vy])
        parent_w, parent_h = w, h
    return levels


def step(levels: list[list[float]]) -> None:
    parent_w, parent_h = W, H
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
    """levels[i][0:2] хранит позицию относительно родителя — переводим в
    абсолютные координаты канваса накопительной суммой по уровням."""
    positions = []
    ax, ay = 0.0, 0.0
    for lvl in levels:
        ax += lvl[0]
        ay += lvl[1]
        positions.append((int(round(ax)), int(round(ay))))
    return positions


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
    levels = spawn()

    while True:
        raw = read_frame(stdin)
        if raw is None:
            break

        frame = np.frombuffer(raw, dtype=np.uint8).reshape(H, W, 3).copy()

        step(levels)
        positions = absolute_positions(levels)

        for (w, h), (x, y) in zip(SIZES, positions):
            thumb = cv2.resize(frame, (w, h), interpolation=cv2.INTER_AREA)
            frame[y:y + h, x:x + w] = thumb

        stdout.write(frame.tobytes())
        stdout.flush()


if __name__ == "__main__":
    main()
