#!/usr/bin/env python3
"""Ксоникс-игра: ОДНО общее поле на двоих (не два раздельных, как в первой
версии) — игроки отнимают территорию у одного нейтрального пула и
соревнуются, кто первым дотянет до порога (порог зависит от сложности,
см. DIFFICULTY_PRESETS, переключается с дашборда через MQTT). Незанятая
часть поля — общая фоновая камера (dvor). Территория игрока 1 открывает
камеру vorota, игрока 2 — камеру tambur, обе теперь декодируются в
ПОЛНОМ размере холста (не в половину, как раньше), потому что владение
клетками больше не привязано к фиксированной половине экрана.

Можно наступить на след соперника — тогда гибнет ОН (его след стирается),
а не вы. Чужую УЖЕ ЗАНЯТУЮ территорию тоже можно отвоёвывать (не только
нейтральную) — наступил, клетка сразу стала твоим следом, соперник теряет
её немедленно, не дожидаясь замыкания контура. Если погибнешь раньше, чем
успеешь замкнуть контур — клетка не возвращается сопернику, откатывается
в нейтральную (обычное поведение die()) — вторжение это реальный риск для
атакующего, а не бесплатный захват.

Управление — через MQTT (см. project memory
project_xonix_cameras_composite.md и обсуждение архитектуры в чате:
топики xonix/game/p{1,2}/move, p{1,2}/mode, control, difficulty, state).

По умолчанию ОБА игрока — боты (attract mode): партия идёт вечно сама с
собой, человек перехватывает управление в любой момент простым нажатием
направления на дашборде ХА — движок сам видит свежий вход и на несколько
секунд (IDLE_TIMEOUT) отдаёт кубик человеку, потом тихо возвращает боту.

ВАЖНО: у самого движка НЕТ встроенной стратегии ИИ (была в первой версии,
убрана). "Бот" — это ОТДЕЛЬНЫЙ процесс xonix_ai_agent.py, подключающийся
по MQTT точно так же, как человек: читает состояние поля из
xonix/game/board и публикует направление в xonix/game/p{1,2}/ai_move —
свой канал, отдельный от p{1,2}/move (туда пишет только дашборд/человек),
чтобы движок мог выбирать между ними по той же логике auto/ai/human, не
путая, кто сейчас реально ведёт. Движок только публикует богатое
состояние и слушает оба канала — он ничего не решает сам.

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
from PIL import Image, ImageDraw, ImageFont

FF = "/usr/lib/ffmpeg/7.0/bin/ffmpeg"
CANVAS_W, CANVAS_H = 960, 540
FPS = 12
CELL = 12
GRID_W = CANVAS_W // CELL  # 80
GRID_H = CANVAS_H // CELL  # 45

# cv2.putText не умеет кириллицу (только Hershey-шрифты, ASCII) — HUD со
# счётом рисуется через PIL шрифтом NotoSans (скачан и закоммичен прямо в
# репозиторий, а не взят из системных шрифтов контейнера — на момент
# написания в контейнере Frigate вообще не было ни одного TTF с кириллицей).
HUD_FONT_PATH = "/config/fonts/NotoSans.ttf"
WINS_FILE = "/config/xonix_wins.json"

MQTT_HOST = "core-mosquitto"
MQTT_PORT = 1883
MQTT_USER = "frigateu"
MQTT_PASS = "qqqqqqq7"

IDLE_TIMEOUT = 8.0
MAX_RAID_LEN = 30  # клеток следа, после которых бот начинает возвращаться, даже не дойдя до цели
BASE_SIZE = 6  # размер стартовой базы каждого игрока, клеток

DIFFICULTY_PRESETS = {
    "easy":   {"target_percent": 30, "num_balls": 4, "speed": (0.10, 0.16)},
    "normal": {"target_percent": 45, "num_balls": 6, "speed": (0.12, 0.20)},
    "hard":   {"target_percent": 60, "num_balls": 9, "speed": (0.16, 0.26)},
}

EMPTY, P1_TERRITORY, P2_TERRITORY, P1_TRAIL, P2_TRAIL = 0, 1, 2, 3, 4
TERRITORY_OF = {"p1": P1_TERRITORY, "p2": P2_TERRITORY}
TRAIL_OF = {"p1": P1_TRAIL, "p2": P2_TRAIL}
OPPONENT = {"p1": "p2", "p2": "p1"}
PLAYER_COLOR = {"p1": (255, 200, 0), "p2": (0, 200, 255)}  # BGR: p1 Голубой, p2 Жёлтый

DIRS = {
    "up": (0, -1),
    "down": (0, 1),
    "left": (-1, 0),
    "right": (1, 0),
}
OPPOSITE = {"up": "down", "down": "up", "left": "right", "right": "left"}

# --- камеры -----------------------------------------------------------
# vorota/tambur теперь декодируются в полный размер холста — владение
# клеткой больше не привязано к фиксированной половине экрана.

CAMERAS = {
    "dvor": [
        FF, "-nostdin", "-loglevel", "warning", "-vaapi_device", "/dev/dri/renderD128",
        "-rtsp_transport", "tcp", "-i", "rtsp://127.0.0.1:8554/dvor_sub",
        "-vf", f"format=nv12,hwupload,scale_vaapi={CANVAS_W}:{CANVAS_H},hwdownload,format=nv12,format=bgr24",
        "-r", str(FPS), "-f", "rawvideo", "-",
    ],
    "vorota": [
        FF, "-nostdin", "-loglevel", "warning", "-vaapi_device", "/dev/dri/renderD128",
        "-rtsp_transport", "tcp", "-i", "rtsp://127.0.0.1:8554/vorota",
        "-vf", f"format=nv12,hwupload,scale_vaapi={CANVAS_W}:{CANVAS_H},hwdownload,format=nv12,format=bgr24",
        "-r", str(FPS), "-f", "rawvideo", "-",
    ],
    "tambur": [
        FF, "-nostdin", "-loglevel", "warning", "-vaapi_device", "/dev/dri/renderD128",
        "-rtsp_transport", "tcp", "-i", "rtsp://127.0.0.1:8554/tambur",
        "-vf", f"format=nv12,hwupload,scale_vaapi={CANVAS_W}:{CANVAS_H},hwdownload,format=nv12,format=bgr24",
        "-r", str(FPS), "-f", "rawvideo", "-",
    ],
}

latest_frames: dict[str, np.ndarray] = {}
frames_lock = threading.Lock()


def camera_reader(name: str, cmd: list[str]) -> None:
    frame_size = CANVAS_W * CANVAS_H * 3
    with frames_lock:
        latest_frames[name] = np.zeros((CANVAS_H, CANVAS_W, 3), dtype=np.uint8)
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
                frame = np.frombuffer(bytes(data), dtype=np.uint8).reshape(CANVAS_H, CANVAS_W, 3)
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
        self.ai_move: dict[str, str] = {"p1": "right", "p2": "left"}
        self.last_input_ts: dict[str, float] = {"p1": 0.0, "p2": 0.0}
        self.mode: dict[str, str] = {"p1": "auto", "p2": "auto"}
        self.control: str | None = None
        self.difficulty = "normal"
        # Только для HUD (draw_hud) — "кто именно играет за бота", не влияет
        # на саму логику движка (той стратегию/провайдера не сообщают, это
        # знают только xonix_ai_agent.py/xonix_llm_strategist.py сами).
        self.strategy: dict[str, str] = {"p1": "smart", "p2": "smart"}
        self.llm_provider: dict[str, str] = {"p1": "off", "p2": "off"}
        self.llm_usage: dict[str, dict] = {"p1": {}, "p2": {}}  # последний вызов: input/output/cached
        self.llm_status: dict[str, str] = {"p1": "", "p2": ""}

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

    def get_ai_move(self, player: str) -> str:
        with self.lock:
            return self.ai_move[player]

    def pop_control(self) -> str | None:
        with self.lock:
            c = self.control
            self.control = None
            return c

    def get_difficulty(self) -> str:
        with self.lock:
            return self.difficulty


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
        elif topic == "xonix/game/p1/ai_move" and payload in DIRS:
            mqtt_state.ai_move["p1"] = payload
        elif topic == "xonix/game/p2/ai_move" and payload in DIRS:
            mqtt_state.ai_move["p2"] = payload
        elif topic == "xonix/game/p1/mode" and payload in ("auto", "ai", "human"):
            mqtt_state.mode["p1"] = payload
        elif topic == "xonix/game/p2/mode" and payload in ("auto", "ai", "human"):
            mqtt_state.mode["p2"] = payload
        elif topic == "xonix/game/control" and payload in ("start", "pause", "restart"):
            mqtt_state.control = payload
        elif topic == "xonix/game/difficulty" and payload in DIFFICULTY_PRESETS:
            mqtt_state.difficulty = payload
            mqtt_state.control = "restart"  # смена сложности на лету — новый раунд с новыми параметрами
        elif topic == "xonix/game/p1/strategy_active" and payload in ("smart", "simple"):
            mqtt_state.strategy["p1"] = payload
        elif topic == "xonix/game/p2/strategy_active" and payload in ("smart", "simple"):
            mqtt_state.strategy["p2"] = payload
        elif topic == "xonix/game/p1/llm_provider" and payload in ("off", "haiku", "kimi"):
            mqtt_state.llm_provider["p1"] = payload
        elif topic == "xonix/game/p2/llm_provider" and payload in ("off", "haiku", "kimi"):
            mqtt_state.llm_provider["p2"] = payload
        elif topic in ("xonix/game/p1/llm_usage", "xonix/game/p2/llm_usage"):
            player = "p1" if "p1" in topic else "p2"
            try:
                mqtt_state.llm_usage[player] = json.loads(payload)
            except json.JSONDecodeError:
                pass
        elif topic in ("xonix/game/p1/llm_status", "xonix/game/p2/llm_status"):
            mqtt_state.llm_status["p1" if "p1" in topic else "p2"] = payload


def start_mqtt() -> mqtt.Client:
    client = mqtt.Client()
    client.username_pw_set(MQTT_USER, MQTT_PASS)
    client.on_message = on_mqtt_message
    client.connect(MQTT_HOST, MQTT_PORT, keepalive=30)
    client.subscribe("xonix/game/p1/move", qos=1)
    client.subscribe("xonix/game/p2/move", qos=1)
    client.subscribe("xonix/game/p1/ai_move", qos=0)
    client.subscribe("xonix/game/p2/ai_move", qos=0)
    client.subscribe("xonix/game/p1/mode", qos=1)
    client.subscribe("xonix/game/p2/mode", qos=1)
    client.subscribe("xonix/game/control", qos=1)
    client.subscribe("xonix/game/difficulty", qos=1)
    client.subscribe("xonix/game/p1/strategy_active", qos=0)
    client.subscribe("xonix/game/p2/strategy_active", qos=0)
    client.subscribe("xonix/game/p1/llm_provider", qos=0)
    client.subscribe("xonix/game/p2/llm_provider", qos=0)
    client.subscribe("xonix/game/p1/llm_usage", qos=0)
    client.subscribe("xonix/game/p2/llm_usage", qos=0)
    client.subscribe("xonix/game/p1/llm_status", qos=0)
    client.subscribe("xonix/game/p2/llm_status", qos=0)
    client.loop_start()
    return client


# --- игровое поле ---------------------------------------------------------

class Ball:
    __slots__ = ("x", "y", "vx", "vy")

    def __init__(self, speed_range: tuple[float, float]) -> None:
        self.x = random.uniform(2, GRID_W - 2)
        self.y = random.uniform(2, GRID_H - 2)
        theta = random.uniform(0, 2 * math.pi)
        speed = random.uniform(*speed_range)
        self.vx = speed * math.cos(theta)
        self.vy = speed * math.sin(theta)


class Field:
    """Общее поле на двоих: одна сетка, два игрока отнимают территорию у
    одного нейтрального пула. Стартуют с маленьких баз в противоположных
    углах."""

    def __init__(self, difficulty: str) -> None:
        self.difficulty = difficulty
        self.reset()

    def reset(self) -> None:
        preset = DIFFICULTY_PRESETS[self.difficulty]
        self.grid = np.zeros((GRID_W, GRID_H), dtype=np.uint8)
        b = BASE_SIZE
        self.grid[1:1 + b, 1:1 + b] = P1_TERRITORY
        self.grid[GRID_W - 1 - b:GRID_W - 1, GRID_H - 1 - b:GRID_H - 1] = P2_TERRITORY
        self.cursor = {
            "p1": (1 + b // 2, 1 + b // 2),
            "p2": (GRID_W - 1 - b // 2, GRID_H - 1 - b // 2),
        }
        self.last_dir = {"p1": "right", "p2": "left"}
        self.trail: dict[str, list[tuple[int, int]]] = {"p1": [], "p2": []}
        self.raid_len = {"p1": 0, "p2": 0}  # длина текущего следа — публикуется в board для агента
        self.balls = [Ball(preset["speed"]) for _ in range(preset["num_balls"])]

    def target_percent(self) -> float:
        return DIFFICULTY_PRESETS[self.difficulty]["target_percent"]

    def percent(self, player: str) -> float:
        total = GRID_W * GRID_H
        return 100.0 * int((self.grid == TERRITORY_OF[player]).sum()) / total

    def walkable_for_ball(self, ix: int, iy: int) -> bool:
        if ix < 0 or iy < 0 or ix >= GRID_W or iy >= GRID_H:
            return False
        return self.grid[ix, iy] in (EMPTY, P1_TRAIL, P2_TRAIL)

    def step_balls(self) -> None:
        for ball in self.balls:
            tx, ty = ball.x + ball.vx, ball.y + ball.vy
            if not self.walkable_for_ball(int(tx), int(ball.y)):
                ball.vx = -ball.vx
            if not self.walkable_for_ball(int(ball.x), int(ty)):
                ball.vy = -ball.vy
            ball.x += ball.vx
            ball.y += ball.vy

    def ball_touches(self, cx: int, cy: int) -> bool:
        for ball in self.balls:
            if int(ball.x) == cx and int(ball.y) == cy:
                return True
        return False

    def die(self, player: str) -> None:
        for (tx, ty) in self.trail[player]:
            self.grid[tx, ty] = EMPTY
        self.trail[player] = []
        self.raid_len[player] = 0
        xs, ys = np.where(self.grid == TERRITORY_OF[player])
        i = random.randrange(len(xs))
        self.cursor[player] = (int(xs[i]), int(ys[i]))

    def claim_trail_and_flood(self, player: str) -> None:
        code = TERRITORY_OF[player]
        for (tx, ty) in self.trail[player]:
            self.grid[tx, ty] = code
        self.trail[player] = []
        self.raid_len[player] = 0
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
                        self.grid[cx, cy] = code

    def step(self, player: str, direction: str) -> None:
        self.last_dir[player] = direction
        dx, dy = DIRS[direction]
        cx, cy = self.cursor[player]
        nx, ny = cx + dx, cy + dy
        if nx < 0 or ny < 0 or nx >= GRID_W or ny >= GRID_H:
            if self.trail[player]:
                # след дошёл до ВНЕШНЕЙ ГРАНИЦЫ поля — она тоже считается
                # "безопасной землёй" для замыкания контура, как в
                # оригинальном Ксониксе (там весь периметр поля изначально
                # занят территорией). Здесь старт — маленькая база в углу,
                # не весь периметр, но сама граница экрана работает так же:
                # без этого след, упёршийся в противоположную стену, просто
                # блокировался бы навсегда, не завершая захват (баг,
                # найденный на живом потоке — "жёлтый уперся в стену").
                self.claim_trail_and_flood(player)
            return
        opp = OPPONENT[player]
        cell = self.grid[nx, ny]

        if cell == TRAIL_OF[opp]:
            # наступили на след соперника — он гибнет, клетка становится нейтральной
            self.die(opp)
            cell = EMPTY

        if cell == TERRITORY_OF[player]:
            if self.trail[player]:
                self.claim_trail_and_flood(player)
            self.cursor[player] = (nx, ny)
        elif cell in (EMPTY, TERRITORY_OF[opp]):
            # чужая территория теперь ТОЖЕ отвоёвывается — наступил, клетка
            # сразу становится твоим следом (соперник теряет её немедленно,
            # не дожидаясь замыкания контура). Если погибнешь раньше, чем
            # замкнёшь контур, клетка вернётся не сопернику, а в нейтральную
            # (die() всегда откатывает в EMPTY) — реальный риск вторжения,
            # не бесплатный захват.
            self.grid[nx, ny] = TRAIL_OF[player]
            self.trail[player].append((nx, ny))
            self.raid_len[player] += 1
            self.cursor[player] = (nx, ny)
            if self.ball_touches(nx, ny):
                self.die(player)
        elif cell == TRAIL_OF[player]:
            self.die(player)

    def step_balls_and_check(self) -> None:
        self.step_balls()
        for player in ("p1", "p2"):
            for (tx, ty) in self.trail[player]:
                if self.ball_touches(tx, ty):
                    self.die(player)
                    break


def load_wins() -> dict:
    try:
        with open(WINS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return {"p1": int(data.get("p1", 0)), "p2": int(data.get("p2", 0))}
    except (FileNotFoundError, json.JSONDecodeError, ValueError, TypeError):
        return {"p1": 0, "p2": 0}


def save_wins(wins: dict) -> None:
    try:
        with open(WINS_FILE, "w", encoding="utf-8") as f:
            json.dump(wins, f)
    except OSError:
        pass


# --- рендер ---------------------------------------------------------------

def upsample_mask(grid_bool: np.ndarray) -> np.ndarray:
    return np.repeat(np.repeat(grid_bool, CELL, axis=0), CELL, axis=1)


_hud_fonts: dict[int, ImageFont.FreeTypeFont | bool] = {}


def _get_hud_font(size: int) -> ImageFont.FreeTypeFont | None:
    cached = _hud_fonts.get(size)
    if cached is None:
        try:
            cached = ImageFont.truetype(HUD_FONT_PATH, size)
        except OSError:
            cached = False  # шрифта нет — не пытаться заново каждый кадр
        _hud_fonts[size] = cached
    return cached or None


def _player_hud_lines(player: str, field: "Field", wins: dict) -> list[str]:
    """Основная строка (кто именно играет + процент + победы) плюс, если
    это бот на LLM-стратеге, статистика последнего запроса — КАЖДОЕ
    значение отдельной строкой ("столбик", не всё в одну строку через
    запятые — по прямому запросу пользователя, раньше было плотной
    единой строкой)."""
    name = "Голубой" if player == "p1" else "Жёлтый"
    if mqtt_state.is_human(player):
        who = "человек"
    else:
        strat = "умный" if mqtt_state.strategy[player] == "smart" else "простой"
        provider = mqtt_state.llm_provider[player]
        who = f"бот: {strat}" if provider == "off" else f"бот: {strat}+{provider.capitalize()}"
    lines = [f"{name} ({who}) {field.percent(player):.1f}%  побед: {wins[player]}"]

    provider = mqtt_state.llm_provider[player]
    if not mqtt_state.is_human(player) and provider != "off":
        usage = mqtt_state.llm_usage[player]
        if usage:
            lines.append(f"    вход: {usage.get('input', '?')} токенов")
            lines.append(f"    выход: {usage.get('output', '?')} токенов")
            cached = usage.get("cached", 0)
            if cached:
                lines.append(f"    кэш: {cached} токенов")
            lines.append(f"    цена: {usage.get('cost_cents', 0.0):.4f}¢")
            lines.append(f"    всего: {usage.get('total_cost_cents', 0.0):.4f}¢")
            if "advantage_cents" in usage:
                adv = usage["advantage_cents"]
                lines.append(f"    выгода: +{adv:.4f}¢" if adv >= 0 else f"    дороже: {-adv:.4f}¢")
        else:
            status = mqtt_state.llm_status[player] or "жду первый ответ…"
            lines.append(f"    {status}")
    return lines


def _render_player_block(lines: list[str], font: ImageFont.FreeTypeFont, small_font: ImageFont.FreeTypeFont, color: tuple[int, int, int]) -> Image.Image:
    rows = [(lines[0], font, (*color, 255))]
    rows += [(t, small_font, (*color, 200)) for t in lines[1:]]

    pad_x, pad_y, line_gap = 14, 9, 4
    tmp = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    widths = [tmp.textlength(text, font=f) for text, f, _ in rows]
    heights = [f.size + 4 for _, f, _ in rows]
    box_w = int(max(widths)) + pad_x * 2
    box_h = sum(heights) + line_gap * (len(rows) - 1) + pad_y * 2

    block = Image.new("RGBA", (box_w, box_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(block)
    draw.rectangle([0, 0, box_w, box_h], fill=(0, 0, 0, 130))
    y = pad_y
    for (text, f, c), h in zip(rows, heights):
        draw.text((pad_x, y), text, font=f, fill=c)
        y += h + line_gap
    return block


def _blend_block(canvas: np.ndarray, block: Image.Image, x0: int, y0: int) -> None:
    block_rgba = np.array(block, dtype=np.float32)
    alpha = block_rgba[:, :, 3:4] / 255.0
    block_bgr = block_rgba[:, :, [2, 1, 0]]
    w, h = block.size
    region = canvas[y0:y0 + h, x0:x0 + w].astype(np.float32)
    canvas[y0:y0 + h, x0:x0 + w] = (block_bgr * alpha + region * (1 - alpha)).astype(np.uint8)


def draw_hud(canvas: np.ndarray, field: "Field", wins: dict) -> np.ndarray:
    """Два независимых полупрозрачных столбика — Голубой СЛЕВА, Жёлтый
    СПРАВА (по прямой просьбе пользователя: "покажи данные столбик по
    каждому игроку справа и слева" — раньше был один общий блок по центру
    с обоими игроками друг под другом). Каждый центрирован в своей четверти
    экрана по вертикали независимо от другого. Проценты территории, счёт
    побед и (для ботов на LLM) статистика токенов/стоимости последнего
    запроса. Рисуется прямо на кадре (не карточкой на дашборде HA) —
    по прямому запросу "информацию... показывать в потоке", полупрозрачно,
    чтобы не закрывать игровое поле под собой."""
    font = _get_hud_font(18)
    small_font = _get_hud_font(14)
    if font is None or small_font is None:
        return canvas  # шрифта нет — тихо пропускаем HUD, не роняем поток

    p1_block = _render_player_block(_player_hud_lines("p1", field, wins), font, small_font, (0, 200, 255))
    p2_block = _render_player_block(_player_hud_lines("p2", field, wins), font, small_font, (255, 200, 0))

    quarter = CANVAS_W // 4
    p1_x0 = quarter - p1_block.size[0] // 2
    p1_y0 = (CANVAS_H - p1_block.size[1]) // 2
    p2_x0 = (CANVAS_W - quarter) - p2_block.size[0] // 2
    p2_y0 = (CANVAS_H - p2_block.size[1]) // 2

    _blend_block(canvas, p1_block, p1_x0, p1_y0)
    _blend_block(canvas, p2_block, p2_x0, p2_y0)
    return canvas


def flash_win(stdout, game_frame: np.ndarray, color: tuple[int, int, int]) -> None:
    """Три моргания цветом победителя во весь экран в момент победы —
    по прямому запросу пользователя. Блокирует игровой цикл на доли
    секунды (разовое событие раз в партию, не каждый кадр), между
    цветными кадрами показывает уже отрисованный финальный кадр партии,
    а не чёрный экран."""
    period = 1.0 / FPS
    solid = np.full((CANVAS_H, CANVAS_W, 3), color, dtype=np.uint8)
    on_frames = max(1, FPS // 4)
    off_frames = max(1, FPS // 4)
    for _ in range(3):
        for _ in range(on_frames):
            stdout.write(solid.tobytes())
            stdout.flush()
            time.sleep(period)
        for _ in range(off_frames):
            stdout.write(game_frame.tobytes())
            stdout.flush()
            time.sleep(period)


def render(field: Field) -> np.ndarray:
    with frames_lock:
        canvas = latest_frames["dvor"].copy()
        cam = {"p1": latest_frames["vorota"], "p2": latest_frames["tambur"]}

    for player in ("p1", "p2"):
        color = PLAYER_COLOR[player]
        mask = upsample_mask(field.grid.T == TERRITORY_OF[player])  # .T: grid[x,y] -> [y,x] под изображение
        canvas[mask] = cam[player][mask]

        # тонкий контур по цвету игрока вдоль границы захваченной территории
        # — по прямому запросу пользователя, поверх уже закрашенной маски.
        contours, _ = cv2.findContours(mask.astype(np.uint8) * 255, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        cv2.drawContours(canvas, contours, -1, color, 1)

        trail_mask = upsample_mask(field.grid.T == TRAIL_OF[player])
        canvas[trail_mask] = color

        # кубик игрока — окошко в его собственную камеру плюс цветная рамка команды
        cx, cy = field.cursor[player]
        px, py = cx * CELL, cy * CELL
        canvas[py:py + CELL, px:px + CELL] = cam[player][py:py + CELL, px:px + CELL]
        cv2.rectangle(canvas, (px, py), (px + CELL, py + CELL), color, 2)

    for ball in field.balls:
        bx, by = int(ball.x * CELL), int(ball.y * CELL)
        r = CELL
        y0, y1 = max(0, by - r // 2), min(CANVAS_H, by + r // 2)
        x0, x1 = max(0, bx - r // 2), min(CANVAS_W, bx + r // 2)
        if y1 > y0 and x1 > x0:
            canvas[y0:y1, x0:x1] = 255 - canvas[y0:y1, x0:x1]
            cv2.circle(canvas, (bx, by), r // 2, (0, 0, 255), 1)

    return canvas


# --- главный цикл -----------------------------------------------------------

def main() -> None:
    for name, cmd in CAMERAS.items():
        threading.Thread(target=camera_reader, args=(name, cmd), daemon=True).start()

    client = start_mqtt()

    field = Field(mqtt_state.get_difficulty())
    phase = "playing"
    phase_until = 0.0
    winner = None
    wins = load_wins()  # переживает рестарты — накопительный счёт побед

    stdout = sys.stdout.buffer
    period = 1.0 / FPS
    last_state_pub = 0.0
    last_board_pub = 0.0
    BOARD_PERIOD = 0.25  # ~4 раза в секунду — агенту незачем чаще, поле неспешное

    while True:
        t0 = time.monotonic()

        cur_difficulty = mqtt_state.get_difficulty()
        if cur_difficulty != field.difficulty:
            field.difficulty = cur_difficulty

        cmd = mqtt_state.pop_control()
        if cmd == "restart":
            field.reset()
            phase = "playing"
            winner = None
        elif cmd == "pause":
            phase = "paused" if phase == "playing" else "playing"

        if phase == "playing":
            for player in ("p1", "p2"):
                direction = mqtt_state.get_move(player) if mqtt_state.is_human(player) else mqtt_state.get_ai_move(player)
                field.step(player, direction)
            field.step_balls_and_check()
            target = field.target_percent()
            for player in ("p1", "p2"):
                if field.percent(player) >= target:
                    phase = "gameover"
                    winner = player
                    phase_until = t0 + 2.5
                    wins[player] += 1
                    save_wins(wins)
                    flash_win(stdout, draw_hud(render(field), field, wins), PLAYER_COLOR[player])
        elif phase == "gameover" and t0 >= phase_until:
            field.reset()
            phase = "playing"
            winner = None

        frame = render(field)
        frame = draw_hud(frame, field, wins)
        stdout.write(frame.tobytes())
        stdout.flush()

        if t0 - last_state_pub > 1.0:
            last_state_pub = t0
            state = {
                "phase": phase,
                "difficulty": field.difficulty,
                "target_percent": field.target_percent(),
                "p1_percent": round(field.percent("p1"), 1),
                "p2_percent": round(field.percent("p2"), 1),
                "p1_controller": "human" if mqtt_state.is_human("p1") else "ai",
                "p2_controller": "human" if mqtt_state.is_human("p2") else "ai",
                "winner": winner,
                "wins": wins,
            }
            client.publish("xonix/game/state", json.dumps(state), qos=0)

        if phase == "playing" and t0 - last_board_pub > BOARD_PERIOD:
            last_board_pub = t0
            board = {
                "grid_w": GRID_W,
                "grid_h": GRID_H,
                "grid": field.grid.tobytes().hex(),  # плоский массив uint8, GRID_W*GRID_H байт
                "cursor": {"p1": field.cursor["p1"], "p2": field.cursor["p2"]},
                "trail_len": {"p1": field.raid_len["p1"], "p2": field.raid_len["p2"]},
                "last_dir": {"p1": field.last_dir["p1"], "p2": field.last_dir["p2"]},
                "balls": [[round(b.x, 2), round(b.y, 2)] for b in field.balls],
            }
            client.publish("xonix/game/board", json.dumps(board), qos=0)

        dt = period - (time.monotonic() - t0)
        if dt > 0:
            time.sleep(dt)


if __name__ == "__main__":
    main()
