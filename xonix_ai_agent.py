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

Третий, необязательный уровень — xonix_llm_strategist.py: раз в ~20с
спрашивает настоящую LLM (Claude Haiku / Kimi) "как в целом играть" по
тексту-"характеру" от пользователя и публикует в xonix/game/p{N}/llm_params
веса aggression/caution — strategy_smart их читает и подмешивает в свою
обычную одношаговую оценку (не подменяет её — LLM слишком медленная для
решения каждого хода). По умолчанию весов нет, эвристика работает как
раньше (0.5/0.5 — нейтрально).
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
STARTUP_GRACE = 30.0  # сколько ждать первую board, прежде чем считать движок не запустившимся
BOARD_STALE_TIMEOUT = 20.0  # board публикуется раз в 0.25с — 20с тишины = движок точно мёртв

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
        self.last_update_ts: float | None = None
        # веса от xonix_llm_strategist.py (третий, медленный уровень — не решает
        # ходы, только настраивает "характер" быстрой эвристики раз в ~20с).
        # 0.5/0.5 — нейтральные значения, воспроизводящие поведение ДО того, как
        # эта настройка появилась (обратная совместимость по умолчанию).
        self.llm_params = {"aggression": 0.5, "caution": 0.5}

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
            self.last_update_ts = time.monotonic()

    def is_stale(self, timeout: float) -> bool:
        """True, если движок никогда не публиковал board ИЛИ замолчал дольше
        timeout — значит xonix_game.py уже не работает (упал, был убит через
        SIGKILL, который trap в xonix_game.sh перехватить не может), и этому
        процессу-агенту больше незачем жить. Самостоятельная страховка вместо
        того, чтобы полагаться только на сигнал от родителя — на практике
        родительский trap иногда не срабатывал, агенты копились orphan'ами
        (найдено 2026-08-20: 5 пар одновременно, все пишут в один топик)."""
        with self.lock:
            if self.last_update_ts is None:
                return False  # ещё ни разу не подключились к движку — не наша забота
            return (time.monotonic() - self.last_update_ts) > timeout

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

    candidates = []
    for d, (dx, dy) in DIRS.items():
        nx, ny = cx + dx, cy + dy
        if nx < 0 or ny < 0 or nx >= gw or ny >= gh:
            continue
        # чужая территория теперь тоже отвоёвывается — не блокируем движение
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
    opp_territory = TERRITORY_OF[opp]

    llm = board.llm_params
    aggression = llm.get("aggression", 0.5)
    caution_factor = 0.5 + llm.get("caution", 0.5)  # 0.5 (беспечно) .. 1.5 (осторожно), 1.0 = как раньше

    best_d, best_score = prev_dir, -1e9
    for d, (dx, dy) in DIRS.items():
        if trailing and d == OPPOSITE[prev_dir]:
            continue
        nx, ny = cx + dx, cy + dy
        if nx < 0 or ny < 0 or nx >= gw or ny >= gh:
            continue
        # чужая территория теперь тоже отвоёвывается — не блокируем движение
        score = random.uniform(0, 1) * 0.2
        for bx, by in balls:
            dist = math.hypot(bx - nx, by - ny)
            if dist < 6:
                score -= (6 - dist) * 2.5 * caution_factor
        odist = math.hypot(opp_cursor[0] - nx, opp_cursor[1] - ny)
        if odist < 3:
            score -= (3 - odist) * 1.0 * caution_factor
        if grid[nx, ny] == opp_territory:
            # LLM-настройка "агрессии" — насколько охотно лезть на чужую
            # землю сверх того, что и так подскажет обычная жадная оценка
            score += aggression * 3.0

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
                    changed = state["strategy"] != payload
                    state["strategy"] = payload
                if changed:
                    client.publish(f"xonix/game/{player}/strategy_active", payload, qos=0, retain=True)
        elif msg.topic == f"xonix/game/{player}/llm_params":
            # публикует xonix_llm_strategist.py раз в ~20с — не решение хода,
            # а настройка "характера" strategy_smart (см. её докстринг)
            try:
                params = json.loads(msg.payload)
                board.llm_params["aggression"] = float(params.get("aggression", 0.5))
                board.llm_params["caution"] = float(params.get("caution", 0.5))
            except (json.JSONDecodeError, TypeError, ValueError):
                pass

    client = mqtt.Client()
    client.username_pw_set(MQTT_USER, MQTT_PASS)
    client.on_message = on_message
    client.connect(MQTT_HOST, MQTT_PORT, keepalive=30)
    client.subscribe("xonix/game/board", qos=0)
    client.subscribe(f"xonix/game/{player}/strategy", qos=1)
    client.subscribe(f"xonix/game/{player}/llm_params", qos=0)
    client.loop_start()
    # подтверждаем реально применённую стратегию сразу при старте (не только
    # при смене) — иначе дашборд не знает актуальное значение по умолчанию,
    # пока кто-то хоть раз не нажал кнопку
    client.publish(f"xonix/game/{player}/strategy_active", args.strategy, qos=0, retain=True)

    start_ts = time.monotonic()
    while True:
        t0 = time.monotonic()

        if board.last_update_ts is None:
            if t0 - start_ts > STARTUP_GRACE:
                # движок так и не подключился за разумное время — скорее
                # всего, его вообще не запустили (или он упал ещё до первой
                # публикации board), самостоятельно завершаемся
                return
        elif board.is_stale(BOARD_STALE_TIMEOUT):
            # движок был, но замолчал — упал/убит, держаться за мёртвую игру незачем
            return

        with state_lock:
            strategy_name = state["strategy"]
        direction = STRATEGIES[strategy_name](player, board)
        client.publish(f"xonix/game/{player}/ai_move", direction, qos=0)
        dt = DECIDE_PERIOD - (time.monotonic() - t0)
        if dt > 0:
            time.sleep(dt)


if __name__ == "__main__":
    main()
