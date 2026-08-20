#!/usr/bin/env python3
"""Фоновая версия Алёны (агента Claude Code) в общем чате Ксоникса —
в отличие от самой сессии Claude Code (которая пишет в чат вручную, только
пока пользователь реально разговаривает в терминале), этот процесс крутится
постоянно вместе с партией и реагирует САМ, без необходимости открывать
Claude Code.

Не управляет ботами — читает только общий канал чата (xonix_chat_hub.py,
xonix/chat/log/general) и отвечает ТОЛЬКО на сообщения человека
(sender == "user"), реплики ботов p1/p2 полностью игнорирует — не встревает
в их переписку между собой. Раз в CHECK_INTERVAL секунд проверяет, не
появилось ли новое сообщение пользователя с прошлой проверки.

Провайдер (xonix/chat/alena_provider — off/haiku/kimi) по умолчанию off —
ни одного вызова API, пока не выбрано явно с дашборда, тот же принцип,
что и у xonix_llm_strategist.py. Функции вызова API/ключей переиспользованы
оттуда напрямую (read_key/call_claude_haiku/call_kimi/stable_cache_key) —
не дублируем реализацию.

Ответ публикуется в общий чат через xonix/chat/post/user с sender="claude"
(тот же контракт, которым сама сессия Claude Code пользуется вручную —
см. докстринг xonix_chat_hub.py) — в ленте подписывается как "Алёна",
неотличимо для пользователя от ручных сообщений агента.
"""
import argparse
import json
import threading
import time
import urllib.error

import paho.mqtt.client as mqtt

from xonix_llm_strategist import ANTHROPIC_KEY_PATH, MOONSHOT_KEY_PATH, call_claude_haiku, call_kimi, read_key

MQTT_HOST = "core-mosquitto"
MQTT_PORT = 1883
MQTT_USER = "frigateu"
MQTT_PASS = "qqqqqqq7"

CHECK_INTERVAL = 8.0  # секунд между проверками "не написал ли человек что-то новое"
STATE_STALE_TIMEOUT = 30.0  # как у остальных процессов партии — движка нет, выходим

SYSTEM_PROMPT = (
    "Ты — Алёна, домашний ИИ-агент (Claude Code) в доме пользователя. Сейчас "
    "ты участвуешь как независимый наблюдатель в общем чате игры Ксоникс "
    "(двое ботов-игроков захватывают территорию через камеры двора, "
    "пользователь наблюдает и иногда пишет в чат). Отвечай только на "
    "сообщения пользователя — реплики ботов-игроков не комментируй, если "
    "пользователь явно не попросил. Тон — дружелюбный, короткий, по-русски, "
    "без длинных монологов (1-3 предложения). Можешь использовать текущий "
    "счёт партии как контекст, если это уместно."
)


class Shared:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.provider = "off"
        self.state: dict = {}
        self.general_log: list[dict] = []
        self.last_state_ts: float | None = None
        self.last_seen_user_ts: float = 0.0


def _format_log(entries: list[dict], limit: int) -> str:
    if not entries:
        return "(пока пусто)"
    who = {"user": "Пользователь", "claude": "Ты (Алёна)", "p1": "Голубой", "p2": "Жёлтый", "system": "Система"}
    lines = []
    for m in entries[-limit:]:
        if m.get("kind") == "status":
            continue
        lines.append(f"{who.get(m.get('sender'), m.get('sender'))}: {m.get('text', '')}")
    return "\n".join(lines) if lines else "(пока пусто)"


def build_user_prompt(shared: Shared, new_text: str) -> str:
    with shared.lock:
        s = shared.state
        log = list(shared.general_log)
    score = f"Голубой {s.get('p1_percent', '?')}%, Жёлтый {s.get('p2_percent', '?')}%, сложность {s.get('difficulty', '?')}"
    return (
        f"Счёт партии: {score}.\n\n"
        f"Последние сообщения в чате:\n{_format_log(log, 10)}\n\n"
        f"Новое сообщение пользователя, на которое нужно ответить: \"{new_text}\""
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.parse_args()

    shared = Shared()
    general_log_received = threading.Event()

    def on_message(client, userdata, msg) -> None:
        if msg.topic == "xonix/game/state":
            try:
                with shared.lock:
                    shared.state = json.loads(msg.payload)
                    shared.last_state_ts = time.monotonic()
            except json.JSONDecodeError:
                pass
        elif msg.topic == "xonix/chat/alena_provider":
            payload = msg.payload.decode("utf-8", errors="ignore").strip()
            if payload in ("off", "haiku", "kimi"):
                with shared.lock:
                    shared.provider = payload
        elif msg.topic == "xonix/chat/log/general":
            try:
                with shared.lock:
                    shared.general_log = json.loads(msg.payload).get("messages", [])
                general_log_received.set()
            except (json.JSONDecodeError, AttributeError):
                pass

    client = mqtt.Client()
    client.username_pw_set(MQTT_USER, MQTT_PASS)
    client.on_message = on_message
    client.connect(MQTT_HOST, MQTT_PORT, keepalive=30)
    client.subscribe("xonix/game/state", qos=0)
    client.subscribe("xonix/chat/alena_provider", qos=1)
    client.subscribe("xonix/chat/log/general", qos=0)
    client.loop_start()

    anthropic_key = read_key(ANTHROPIC_KEY_PATH)
    moonshot_key = read_key(MOONSHOT_KEY_PATH)

    # Ждём РЕАЛЬНОГО прихода ретейн-истории (xonix/chat/log/general), не
    # фиксированную паузу — засев по неполной/ещё не долетевшей истории
    # проверен вживую как источник бага: старое сообщение, отсутствующее
    # в момент засева, потом "проскакивало" как новое на первом же тике.
    # Таймаут — на случай пустого канала (истории ещё вообще не было).
    general_log_received.wait(timeout=10.0)
    with shared.lock:
        shared.last_seen_user_ts = max((m.get("ts", 0) for m in shared.general_log if m.get("sender") == "user"), default=0.0)

    start_ts = time.monotonic()
    while True:
        t0 = time.monotonic()

        with shared.lock:
            last_state_ts = shared.last_state_ts
        if last_state_ts is None:
            if t0 - start_ts > STATE_STALE_TIMEOUT:
                return
        elif t0 - last_state_ts > STATE_STALE_TIMEOUT:
            return

        with shared.lock:
            provider = shared.provider
            log = list(shared.general_log)
            last_seen = shared.last_seen_user_ts

        # Ищем самое новое сообщение человека, которое ещё не обработано —
        # если пользователь написал несколько сообщений подряд между
        # проверками, отвечаем только на последнее (не заваливаем чат
        # отдельным ответом на каждое).
        new_user_msg = None
        for m in reversed(log):
            if m.get("sender") == "user" and m.get("ts", 0) > last_seen:
                new_user_msg = m
                break

        if provider != "off" and new_user_msg is not None:
            with shared.lock:
                shared.last_seen_user_ts = new_user_msg["ts"]
            user_prompt = build_user_prompt(shared, new_user_msg.get("text", ""))
            try:
                if provider == "haiku":
                    if not anthropic_key:
                        raise RuntimeError("no anthropic key")
                    text, usage = call_claude_haiku(anthropic_key, SYSTEM_PROMPT, user_prompt)
                else:
                    if not moonshot_key:
                        raise RuntimeError("no moonshot key")
                    text, usage = call_kimi(moonshot_key, SYSTEM_PROMPT, user_prompt)
                text = text.strip()
                if text:
                    client.publish(
                        "xonix/chat/post/user",
                        json.dumps(
                            {"channel": "general", "text": text[:400], "sender": "claude", "usage": {"provider": provider, **usage}},
                            ensure_ascii=False,
                        ),
                        qos=1,
                    )
                client.publish("xonix/chat/alena_status", "ok", qos=0, retain=True)
            except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, RuntimeError) as e:
                client.publish("xonix/chat/alena_status", f"ошибка: {e}", qos=0, retain=True)
            except Exception as e:
                client.publish("xonix/chat/alena_status", f"неожиданная ошибка: {e}", qos=0, retain=True)

        dt = CHECK_INTERVAL - (time.monotonic() - t0)
        if dt > 0:
            time.sleep(dt)


if __name__ == "__main__":
    main()
