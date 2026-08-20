#!/usr/bin/env python3
"""Отдельный процесс-агент, играющий за одного из игроков Ксоникс-игры
(xonix_game.py) — подключается по MQTT ТОЧНО ТАК ЖЕ, как человек с
дашборда: читает состояние поля из xonix/game/board и публикует
направление в xonix/game/p{N}/ai_move (свой канал, не тот же, что у
человека — так движок может выбирать между ними по логике auto/ai/human,
не подмешивая источники). Сам движок ничего не знает о стратегии и не
содержит игровой логики ИИ вообще — она целиком здесь, в отдельном
процессе, который можно заменить/расширить не трогая движок.

Запуск: python3 xonix_ai_agent.py p1|p2 [--strategy smart|simple]
Стратегию также можно переключить на лету с дашборда — топик
xonix/game/p{N}/strategy (retained), агент подписан и переключается
без перезапуска.

Стратегии:
- smart  — три фазы (exiting/raiding/returning), уклонение от шариков и
  соперника, цель набега на безопасном удалении. Перенесена без
  изменений логики из первой версии движка (см. историю коммитов
  xonix_game.py и project_xonix_cameras_composite.md) — там же разбор
  багов, из-за которых первая версия зависала.
- simple — гораздо более тупой бот: почти случайное блуждание с только
  базовым уклонением от шариков, без цели и без осмысленного возврата
  домой. Настоящая "лёгкая" стратегия для выбора на дашборде, а не
  формальная вторая позиция в списке.
"""
import argparse
import json
import math
import random
import sys
import threading
import time

import numpy as np
import paho.mqtt.client as mqtt

MQTT_HOST = "core-mosquitto"
MQTT_PORT = 1883
MQTT_USER = "frigateu"
MQTT_PASS = "qqqqqqq7"

MAX_RAID_LEN = 30
DECIDE_PERIOD = 0.25  # держим темп публикации ходов в шаге с публикацией board у движка

EMPTY, P1_TERRITORY, P2_TERRITORY, P1_TRAIL, P2_TRAIL = 0, 1, 2, 3, 4
TERRITORY_OF = {"p1": P1_TERRITORY, "p2": P2_TERRITORY}

DIRS = {
    "up": (0, -1),
    "down": (0, 1),
    "left": (-1, 0),
    "right": (1, 0),
}
OPPOSITE = {"up": "down", "down": "up", "left": "right", "right": "left"}


class BoardState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.grid: np.ndarray | None = None
        self.grid_w = 0
        self.grid_h = 0
        self.cursor: dict[str, tuple[int, int]] = {}
        self.trail_len: dict[str, int] = {}
        self.last_dir: dict[str, str] = {}
        self.balls: list[tuple[float, float]] = []
        self.raid_target: tuple[int, int] | None = None  # состояние стратегии "smart", живёт тут в агенте

    def update(self, msg: dict) -> None:
        with self.lock:
            self.grid_w = msg["grid_w"]
            self.grid_h = msg["grid_h"]
            flat = np.frombuffer(bytes.fromhex(msg["grid"]), dtype=np.uint8)
            self.grid = flat.reshape(self.grid_w, self.grid_h)
            self.cursor = {k: tuple(v) for k, v in msg["cursor"].items()}
            self.trail_len = msg["trail_len"]
            self.last_dir = msg["last_dir"]
            self.balls = [tuple(b) for b in msg["balls"]]

    def snapshot(self):
        with self.lock:
            if self.grid is None:
                return None
            return (
                self.grid.copy(), self.grid_w, self.grid_h,
                dict(self.cursor), dict(self.trail_len), dict(self.last_dir),
                list(self.balls),
            )


def strategy_simple(player: str, board: BoardState) -> str:
    """Тупой бот: случайное направление с базовым уклонением от шариков,
    без цели и без плана возврата — настоящая "лёгкая" стратегия."""
    snap = board.snapshot()
    if snap is None:
        return "right"
    grid, gw, gh, cursor, trail_len, last_dir, balls = snap
    cx, cy = cursor[player]
    opp = "p2" if player == "p1" else "p1"
    opp_territory = TERRITORY_OF[opp]

    candidates = []
    for d, (dx, dy) in DIRS.items():
        nx, ny = cx + dx, cy + dy
        if nx < 0 or ny < 0 or nx >= gw or ny >= gh:
            continue
        if grid[nx, ny] == opp_territory:
            continue
        score = random.uniform(0, 1)
        for bx, by in balls:
            dist = math.hypot(bx - nx, by - ny)
            if dist < 4:
                score -= (4 - dist)
        candidates.append((score, d))
    if not candidates:
        return last_dir.get(player, "right")
    return max(candidates)[1]


def _pick_raid_target(grid, gw, gh, cx, cy, balls):
    best_pt, best_safety = None, -1e9
    for _ in range(20):
        tx = random.randint(1, gw - 2)
        ty = random.randint(1, gh - 2)
        dist = math.hypot(cx - tx, cy - ty)
        if grid[tx, ty] != EMPTY or dist < 8 or dist > 20:
            continue
        safety = min((math.hypot(bx - tx, by - ty) for bx, by in balls), default=99.0)
        if safety > best_safety:
            best_safety, best_pt = safety, (tx, ty)
    return best_pt


def strategy_smart(player: str, board: BoardState) -> str:
    """Три фазы: exiting (идём к ближайшей нейтральной клетке, если стоим
    на своей территории — иначе жадный 1-шаговый выбор залипает внутри
    большого куска своей земли, см. историю багов) -> raiding (идём к
    случайной безопасной цели) -> returning (цель достигнута/след слишком
    длинный — идём к ближайшей своей территории)."""
    snap = board.snapshot()
    if snap is None:
        return "right"
    grid, gw, gh, cursor, trail_len, last_dir, balls = snap
    cx, cy = cursor[player]
    opp = "p2" if player == "p1" else "p1"
    opp_cursor = cursor[opp]
    opp_territory = TERRITORY_OF[opp]
    my_territory = TERRITORY_OF[player]
    trailing = trail_len[player] > 0
    exiting = not trailing

    if exiting:
        board.raid_target = None
    else:
        target = board.raid_target
        if target is None:
            target = _pick_raid_target(grid, gw, gh, cx, cy, balls)
            board.raid_target = target
        if target is not None:
            tx, ty = target
            reached = math.hypot(cx - tx, cy - ty) < 2
            too_long = trail_len[player] > MAX_RAID_LEN
            if reached or too_long:
                board.raid_target = None

    target = board.raid_target
    heading_home = trailing and target is None
    prev_dir = last_dir.get(player, "right")

    best_d, best_score = prev_dir, -1e9
    for d, (dx, dy) in DIRS.items():
        if trailing and d == OPPOSITE[prev_dir]:
            continue
        nx, ny = cx + dx, cy + dy
        if nx < 0 or ny < 0 or nx >= gw or ny >= gh:
            continue
        if grid[nx, ny] == opp_territory:
            continue
        score = random.uniform(0, 1) * 0.2
        for bx, by in balls:
            dist = math.hypot(bx - nx, by - ny)
            if dist < 6:
                score -= (6 - dist) * 2.5
        odist = math.hypot(opp_cursor[0] - nx, opp_cursor[1] - ny)
        if odist < 3:
            score -= (3 - odist) * 1.0

        if exiting:
            xs, ys = np.where(grid == EMPTY)
            dists = (xs - nx) ** 2 + (ys - ny) ** 2
            nearest = dists.min() if len(dists) else 1e9
            score += 6.0 / (1.0 + nearest)
        elif heading_home:
            xs, ys = np.where(grid == my_territory)
            dists = (xs - nx) ** 2 + (ys - ny) ** 2
            nearest = dists.min() if len(dists) else 1e9
            score += 4.0 / (1.0 + nearest)
        else:
            tx, ty = target
            score += 2.0 / (1.0 + math.hypot(nx - tx, ny - ty))
        if score > best_score:
            best_score, best_d = score, d
    return best_d


STRATEGIES = {"smart": strategy_smart, "simple": strategy_simple}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("player", choices=["p1", "p2"])
    parser.add_argument("--strategy", choices=list(STRATEGIES), default="smart")
    args = parser.parse_args()
    player = args.player

    board = BoardState()
    state = {"strategy": args.strategy}
    state_lock = threading.Lock()

    def on_message(client, userdata, msg) -> None:
        if msg.topic == "xonix/game/board":
            try:
                board.update(json.loads(msg.payload))
            except Exception:
                pass
        elif msg.topic == f"xonix/game/{player}/strategy":
            payload = msg.payload.decode("utf-8", errors="ignore").strip()
            if payload in STRATEGIES:
                with state_lock:
                    state["strategy"] = payload

    client = mqtt.Client()
    client.username_pw_set(MQTT_USER, MQTT_PASS)
    client.on_message = on_message
    client.connect(MQTT_HOST, MQTT_PORT, keepalive=30)
    client.subscribe("xonix/game/board", qos=0)
    client.subscribe(f"xonix/game/{player}/strategy", qos=1)
    client.loop_start()

    while True:
        t0 = time.monotonic()
        with state_lock:
            strategy_name = state["strategy"]
        direction = STRATEGIES[strategy_name](player, board)
        client.publish(f"xonix/game/{player}/ai_move", direction, qos=0)
        dt = DECIDE_PERIOD - (time.monotonic() - t0)
        if dt > 0:
            time.sleep(dt)


if __name__ == "__main__":
    main()
