#!/usr/bin/env python3
"""Ксоникс-игра: два раздельных поля (сплит-экран), у каждого игрока своя
захватываемая территория. Незанятая часть поля — общая фоновая камера
(dvor). Занятая территория игрока 1 открывает камеру vorota, игрока 2 —
камеру tambur. Управление — через MQTT (см. project memory
project_xonix_cameras_composite.md и обсуждение архитектуры в чате:
топики xonix/game/p{1,2}/move, p{1,2}/mode, control, state).

По умолчанию ОБА игрока — боты (attract mode): партия идёт вечно сама с
собой, человек перехватывает управление в любой момент простым нажатием
направления на дашборде ХА — движок сам видит свежий вход и на несколько
секунд (IDLE_TIMEOUT) отдаёт кубик человеку, потом тихо возвращает боту.

Камеры декодируются через уже открытые go2rtc-потоки (localhost-рестрим,
те же, что у Frigate detect/record) — ровно одно физическое подключение на
камеру, как и в xonix_compositor_cameras.py (правило проекта).

Три ffmpeg-процесса декодирования (dvor фон, vorota P1, tambur P2) +
MQTT-клиент в фоновом потоке + сам игровой цикл, который пишет готовые
кадры в stdout — снаружи их забирает xonix_game.sh (encode h264_vaapi ->
RTSP push в go2rtc, тот же паттерн, что xonix_layer_multicam.sh).
"""
import json
import math
import random
import subprocess
import sys
import threading
import time

import cv2
import numpy as np
import paho.mqtt.client as mqtt

FF = "/usr/lib/ffmpeg/7.0/bin/ffmpeg"
CANVAS_W, CANVAS_H = 960, 540
HALF_W = CANVAS_W // 2
FPS = 12
CELL = 12
GRID_W = HALF_W // CELL   # 40
GRID_H = CANVAS_H // CELL  # 45

MQTT_HOST = "core-mosquitto"
MQTT_PORT = 1883
MQTT_USER = "frigateu"
MQTT_PASS = "qqqqqqq7"

IDLE_TIMEOUT = 8.0
NUM_BALLS = 3
BALL_SPEED_MIN, BALL_SPEED_MAX = 0.12, 0.22
TARGET_PERCENT = 75.0
MAX_RAID_LEN = 60  # клеток следа, после которых бот начинает возвращаться

EMPTY, TERRITORY, TRAIL = 0, 1, 2

DIRS = {
    "up": (0, -1),
    "down": (0, 1),
    "left": (-1, 0),
    "right": (1, 0),
}
OPPOSITE = {"up": "down", "down": "up", "left": "right", "right": "left"}

# --- камеры -----------------------------------------------------------

CAMERAS = {
    "dvor": ((CANVAS_W, CANVAS_H), [
        FF, "-nostdin", "-loglevel", "warning", "-vaapi_device", "/dev/dri/renderD128",
        "-rtsp_transport", "tcp", "-i", "rtsp://127.0.0.1:8554/dvor_sub",
        "-vf", f"format=nv12,hwupload,scale_vaapi={CANVAS_W}:{CANVAS_H},hwdownload,format=nv12,format=bgr24",
        "-r", str(FPS), "-f", "rawvideo", "-",
    ]),
    "vorota": ((HALF_W, CANVAS_H), [
        FF, "-nostdin", "-loglevel", "warning", "-vaapi_device", "/dev/dri/renderD128",
        "-rtsp_transport", "tcp", "-i", "rtsp://127.0.0.1:8554/vorota",
        "-vf", f"format=nv12,hwupload,scale_vaapi={HALF_W}:{CANVAS_H},hwdownload,format=nv12,format=bgr24",
        "-r", str(FPS), "-f", "rawvideo", "-",
    ]),
    "tambur": ((HALF_W, CANVAS_H), [
        FF, "-nostdin", "-loglevel", "warning", "-vaapi_device", "/dev/dri/renderD128",
        "-rtsp_transport", "tcp", "-i", "rtsp://127.0.0.1:8554/tambur",
        "-vf", f"format=nv12,hwupload,scale_vaapi={HALF_W}:{CANVAS_H},hwdownload,format=nv12,format=bgr24",
        "-r", str(FPS), "-f", "rawvideo", "-",
    ]),
}

latest_frames: dict[str, np.ndarray] = {}
frames_lock = threading.Lock()


def camera_reader(name: str, size: tuple[int, int], cmd: list[str]) -> None:
    w, h = size
    frame_size = w * h * 3
    with frames_lock:
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
                with frames_lock:
                    latest_frames[name] = frame
        except EOFError:
            pass
        finally:
            proc.kill()
            proc.wait()
        time.sleep(2)


# --- MQTT ---------------------------------------------------------------

class MqttState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.move: dict[str, str] = {"p1": "right", "p2": "left"}
        self.last_input_ts: dict[str, float] = {"p1": 0.0, "p2": 0.0}
        self.mode: dict[str, str] = {"p1": "auto", "p2": "auto"}
        self.control: str | None = None

    def is_human(self, player: str) -> bool:
        with self.lock:
            mode = self.mode[player]
            if mode == "human":
                return True
            if mode == "ai":
                return False
            return (time.monotonic() - self.last_input_ts[player]) < IDLE_TIMEOUT

    def get_move(self, player: str) -> str:
        with self.lock:
            return self.move[player]

    def pop_control(self) -> str | None:
        with self.lock:
            c = self.control
            self.control = None
            return c


mqtt_state = MqttState()


def on_mqtt_message(client, userdata, msg) -> None:
    topic = msg.topic
    payload = msg.payload.decode("utf-8", errors="ignore").strip()
    with mqtt_state.lock:
        if topic == "xonix/game/p1/move" and payload in DIRS:
            mqtt_state.move["p1"] = payload
            mqtt_state.last_input_ts["p1"] = time.monotonic()
        elif topic == "xonix/game/p2/move" and payload in DIRS:
            mqtt_state.move["p2"] = payload
            mqtt_state.last_input_ts["p2"] = time.monotonic()
        elif topic == "xonix/game/p1/mode" and payload in ("auto", "ai", "human"):
            mqtt_state.mode["p1"] = payload
        elif topic == "xonix/game/p2/mode" and payload in ("auto", "ai", "human"):
            mqtt_state.mode["p2"] = payload
        elif topic == "xonix/game/control" and payload in ("start", "pause", "restart"):
            mqtt_state.control = payload


def start_mqtt() -> mqtt.Client:
    client = mqtt.Client()
    client.username_pw_set(MQTT_USER, MQTT_PASS)
    client.on_message = on_mqtt_message
    client.connect(MQTT_HOST, MQTT_PORT, keepalive=30)
    client.subscribe("xonix/game/p1/move", qos=1)
    client.subscribe("xonix/game/p2/move", qos=1)
    client.subscribe("xonix/game/p1/mode", qos=1)
    client.subscribe("xonix/game/p2/mode", qos=1)
    client.subscribe("xonix/game/control", qos=1)
    client.loop_start()
    return client


# --- игровое поле ---------------------------------------------------------

class Ball:
    __slots__ = ("x", "y", "vx", "vy")

    def __init__(self) -> None:
        self.x = random.uniform(2, GRID_W - 2)
        self.y = random.uniform(2, GRID_H - 2)
        theta = random.uniform(0, 2 * math.pi)
        speed = random.uniform(BALL_SPEED_MIN, BALL_SPEED_MAX)
        self.vx = speed * math.cos(theta)
        self.vy = speed * math.sin(theta)


class Half:
    """Одно из двух полей (игрок 1 или игрок 2)."""

    def __init__(self, player: str) -> None:
        self.player = player
        self.reset()

    def reset(self) -> None:
        self.grid = np.zeros((GRID_W, GRID_H), dtype=np.uint8)
        self.grid[0, :] = TERRITORY
        self.grid[-1, :] = TERRITORY
        self.grid[:, 0] = TERRITORY
        self.grid[:, -1] = TERRITORY
        self.cursor = (0, 0)
        self.last_dir = "right" if self.player == "p1" else "left"
        self.trail: list[tuple[int, int]] = []
        self.balls = [Ball() for _ in range(NUM_BALLS)]
        self.raid_len = 0

    def percent(self) -> float:
        total = GRID_W * GRID_H
        return 100.0 * int((self.grid == TERRITORY).sum()) / total

    def walkable_for_ball(self, ix: int, iy: int) -> bool:
        if ix < 0 or iy < 0 or ix >= GRID_W or iy >= GRID_H:
            return False
        return self.grid[ix, iy] != TERRITORY

    def step_balls(self) -> None:
        for b in self.balls:
            tx, ty = b.x + b.vx, b.y + b.vy
            if not self.walkable_for_ball(int(tx), int(b.y)):
                b.vx = -b.vx
            if not self.walkable_for_ball(int(b.x), int(ty)):
                b.vy = -b.vy
            b.x += b.vx
            b.y += b.vy

    def ball_touches(self, cx: int, cy: int) -> bool:
        for b in self.balls:
            if int(b.x) == cx and int(b.y) == cy:
                return True
        return False

    def die(self) -> None:
        for (tx, ty) in self.trail:
            self.grid[tx, ty] = EMPTY
        self.trail = []
        self.raid_len = 0
        xs, ys = np.where(self.grid == TERRITORY)
        i = random.randrange(len(xs))
        self.cursor = (int(xs[i]), int(ys[i]))

    def claim_trail_and_flood(self) -> None:
        for (tx, ty) in self.trail:
            self.grid[tx, ty] = TERRITORY
        self.trail = []
        self.raid_len = 0
        visited = np.zeros((GRID_W, GRID_H), dtype=bool)
        for sx in range(GRID_W):
            for sy in range(GRID_H):
                if self.grid[sx, sy] != EMPTY or visited[sx, sy]:
                    continue
                stack = [(sx, sy)]
                visited[sx, sy] = True
                component = []
                has_ball = False
                while stack:
                    x, y = stack.pop()
                    component.append((x, y))
                    if self.ball_touches(x, y):
                        has_ball = True
                    for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                        nx, ny = x + dx, y + dy
                        if 0 <= nx < GRID_W and 0 <= ny < GRID_H and not visited[nx, ny] and self.grid[nx, ny] == EMPTY:
                            visited[nx, ny] = True
                            stack.append((nx, ny))
                if not has_ball:
                    for (cx, cy) in component:
                        self.grid[cx, cy] = TERRITORY

    def ai_direction(self) -> str:
        cx, cy = self.cursor
        on_territory = self.grid[cx, cy] == TERRITORY
        best_d, best_score = self.last_dir, -1e9
        for d, (dx, dy) in DIRS.items():
            if len(self.trail) > 0 and d == OPPOSITE[self.last_dir]:
                continue
            nx, ny = cx + dx, cy + dy
            if nx < 0 or ny < 0 or nx >= GRID_W or ny >= GRID_H:
                continue
            score = random.uniform(0, 1) * 0.3
            for b in self.balls:
                dist = math.hypot(b.x - nx, b.y - ny)
                if dist < 4:
                    score -= (4 - dist) * 2
            if on_territory and len(self.trail) == 0:
                if self.grid[nx, ny] == EMPTY:
                    score += 1.0
            else:
                if self.raid_len > MAX_RAID_LEN:
                    xs, ys = np.where(self.grid == TERRITORY)
                    dists = (xs - nx) ** 2 + (ys - ny) ** 2
                    nearest = dists.min() if len(dists) else 1e9
                    score += 3.0 / (1.0 + nearest)
            if score > best_score:
                best_score, best_d = score, d
        return best_d

    def step(self, direction: str) -> None:
        self.last_dir = direction
        dx, dy = DIRS[direction]
        cx, cy = self.cursor
        nx, ny = cx + dx, cy + dy
        if nx < 0 or ny < 0 or nx >= GRID_W or ny >= GRID_H:
            self.step_balls()
            return
        cell = self.grid[nx, ny]
        if cell == TERRITORY:
            if self.trail:
                self.claim_trail_and_flood()
            self.cursor = (nx, ny)
        elif cell == EMPTY:
            self.grid[nx, ny] = TRAIL
            self.trail.append((nx, ny))
            self.raid_len += 1
            self.cursor = (nx, ny)
            if self.ball_touches(nx, ny):
                self.die()
        else:  # TRAIL — самопересечение
            self.die()
        self.step_balls()
        # шарик мог заехать на уже существующий след ПОСЛЕ своего хода —
        # проверяем отдельно от проверки на "только что нарисованную" клетку выше
        for (tx, ty) in self.trail:
            if self.ball_touches(tx, ty):
                self.die()
                break


# --- рендер ---------------------------------------------------------------

def upsample_mask(grid_bool: np.ndarray) -> np.ndarray:
    return np.repeat(np.repeat(grid_bool, CELL, axis=0), CELL, axis=1)


def render(halves: dict[str, Half]) -> np.ndarray:
    with frames_lock:
        bg = latest_frames["dvor"].copy()
        cam = {"p1": latest_frames["vorota"], "p2": latest_frames["tambur"]}

    canvas = bg
    for player, x_off in (("p1", 0), ("p2", HALF_W)):
        half = halves[player]
        mask = upsample_mask(half.grid.T == TERRITORY)  # .T: grid[x,y] -> [y,x] под изображение
        region = canvas[:, x_off:x_off + HALF_W]
        region[mask] = cam[player][mask]

        trail_mask = upsample_mask(half.grid.T == TRAIL)
        color = (255, 200, 0) if player == "p1" else (0, 200, 255)
        region[trail_mask] = color

        # кубик игрока — окошко в его собственную камеру (не в фон, в отличие
        # от шариков) плюс цветная рамка команды
        cx, cy = half.cursor
        px, py = x_off + cx * CELL, cy * CELL
        canvas[py:py + CELL, px:px + CELL] = cam[player][py:py + CELL, px - x_off:px - x_off + CELL]
        cv2.rectangle(canvas, (px, py), (px + CELL, py + CELL), color, 2)

        for b in half.balls:
            bx, by = x_off + int(b.x * CELL), int(b.y * CELL)
            r = CELL
            y0, y1 = max(0, by - r // 2), min(CANVAS_H, by + r // 2)
            x0, x1 = max(0, bx - r // 2), min(CANVAS_W, bx + r // 2)
            if y1 > y0 and x1 > x0:
                canvas[y0:y1, x0:x1] = 255 - canvas[y0:y1, x0:x1]
                cv2.circle(canvas, (bx, by), r // 2, (0, 0, 255), 1)

    return canvas


# --- главный цикл -----------------------------------------------------------

def main() -> None:
    for name, (size, cmd) in CAMERAS.items():
        threading.Thread(target=camera_reader, args=(name, size, cmd), daemon=True).start()

    client = start_mqtt()

    halves = {"p1": Half("p1"), "p2": Half("p2")}
    phase = "playing"
    phase_until = 0.0

    stdout = sys.stdout.buffer
    period = 1.0 / FPS
    last_state_pub = 0.0

    while True:
        t0 = time.monotonic()

        cmd = mqtt_state.pop_control()
        if cmd == "restart":
            halves["p1"].reset()
            halves["p2"].reset()
            phase = "playing"
        elif cmd == "pause":
            phase = "paused" if phase == "playing" else "playing"

        if phase == "playing":
            for player, half in halves.items():
                direction = mqtt_state.get_move(player) if mqtt_state.is_human(player) else half.ai_direction()
                half.step(direction)
            for player, half in halves.items():
                if half.percent() >= TARGET_PERCENT:
                    phase = "gameover"
                    phase_until = t0 + 2.5
        elif phase == "gameover" and t0 >= phase_until:
            halves["p1"].reset()
            halves["p2"].reset()
            phase = "playing"

        frame = render(halves)
        stdout.write(frame.tobytes())
        stdout.flush()

        if t0 - last_state_pub > 1.0:
            last_state_pub = t0
            state = {
                "phase": phase,
                "p1_percent": round(halves["p1"].percent(), 1),
                "p2_percent": round(halves["p2"].percent(), 1),
                "p1_controller": "human" if mqtt_state.is_human("p1") else "ai",
                "p2_controller": "human" if mqtt_state.is_human("p2") else "ai",
            }
            client.publish("xonix/game/state", json.dumps(state), qos=0)

        dt = period - (time.monotonic() - t0)
        if dt > 0:
            time.sleep(dt)


if __name__ == "__main__":
    main()
