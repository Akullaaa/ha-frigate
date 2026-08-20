#!/usr/bin/env python3
"""Единственный писатель истории чата Ксоникса. Сам не думает и не решает
ходы — просто принимает сообщения от трёх источников (пользователь с
дашборда/из чата Claude Code, LLM-стратег p1, LLM-стратег p2), копит их в
три канала и переиздаёт полную историю каждого как ретейн-JSON, откуда её
читает и дашборд (markdown-карточка), и сами LLM-стратеги (как контекст
для следующего хода диалога).

Каналы:
- general    — общий: пользователь + оба LLM-стратега видят и пишут сюда.
  Статус партии сюда НЕ подмешивается (было так раньше — убрано 2026-08-20:
  боты общаются каждые ~15-20с, статус раз в STATUS_INTERVAL быстро вытеснял
  редкие реплики пользователя из ограниченного окна истории — сообщение
  реально уходило за HISTORY_LIMIT, не баг рендера, а вытеснение). Счёт и
  так дублируется на вкладках xonix-p1/p2/video через sensor.ksoniks_sostoianie
  — дашборд рисует счёт статично сверху ленты чата тем же сенсором, не
  тратя слоты истории общего канала на то, что и так видно рядом.
- private_p1 — приватный канал LLM-стратега p1 к своему боту. Читает
  ТОЛЬКО xonix_llm_strategist.py p1 (просто не подписывается на private_p2)
  и пользователь на дашборде (как "третий независимый эксперт", видит оба
  приватных канала). LLM p2 физически не подписан на этот топик — приватность
  обеспечена на уровне того, кто на что подписывается, а не шифрованием.
- private_p2 — симметрично для p2.

Входные топики (события, НЕ ретейн — хаб единственный, кто пишет ретейн-лог):
  xonix/chat/post/user  {"channel":"general","text":"...","sender":"user"?}
  xonix/chat/post/p1    {"channel":"general"|"private","text":"..."}
  xonix/chat/post/p2    {"channel":"general"|"private","text":"..."}
"sender" в post/user необязателен (по умолчанию "user") — добавлен, чтобы
отличать реального человека с дашборда от сообщений, которые в тот же общий
канал пишет пользователь через ЭТОТ САМЫЙ чат Claude Code (сессия агента
Алёна), публикуя вручную с sender="claude" — тот же топик, та же история,
просто другая подпись в ленте.

Выходные топики (ретейн, объект {"messages": [...]} — не голый список,
чтобы работать через json_attributes_topic как и sensor.ksoniks_sostoianie):
  xonix/chat/log/general
  xonix/chat/log/private_p1
  xonix/chat/log/private_p2

История ограничена HISTORY_LIMIT записей на канал (не растёт бесконечно —
и RAM, и MQTT-payload не резиновые) и переживает рестарт процесса через
JSON-файл на диске.
"""
import json
import threading
import time

import paho.mqtt.client as mqtt

MQTT_HOST = "core-mosquitto"
MQTT_PORT = 1883
MQTT_USER = "frigateu"
MQTT_PASS = "qqqqqqq7"

HISTORY_FILE = "/config/xonix_chat_history.json"
HISTORY_LIMIT = 60  # сообщений на канал

STARTUP_GRACE = 30.0
STATE_STALE_TIMEOUT = 30.0  # как у xonix_llm_strategist.py — движка нет, выходим

CHANNELS = ("general", "private_p1", "private_p2")


class Hub:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.history: dict[str, list[dict]] = {c: [] for c in CHANNELS}
        self._load()
        self.last_state_ts: float | None = None

    def _load(self) -> None:
        try:
            with open(HISTORY_FILE, encoding="utf-8") as f:
                data = json.load(f)
            for c in CHANNELS:
                self.history[c] = data.get(c, [])[-HISTORY_LIMIT:]
        except (FileNotFoundError, json.JSONDecodeError):
            pass

    def _save(self) -> None:
        try:
            with open(HISTORY_FILE, "w", encoding="utf-8") as f:
                json.dump(self.history, f, ensure_ascii=False)
        except OSError:
            pass

    def append(self, channel: str, sender: str, text: str, kind: str = "chat", usage: dict | None = None) -> dict:
        text = text.strip()
        if not text:
            return {}
        entry = {"ts": time.time(), "sender": sender, "text": text[:400], "kind": kind}
        if usage:
            # Цена конкретного запроса к LLM (вход/выход/закешировано,
            # см. xonix_llm_strategist.py) — прикреплена к сообщению, чтобы
            # дашборд мог показать её прямо рядом с репликой, не только
            # в отдельном топике llm_usage.
            entry["usage"] = usage
        with self.lock:
            self.history[channel].append(entry)
            self.history[channel] = self.history[channel][-HISTORY_LIMIT:]
            snapshot = list(self.history[channel])
            self._save()
        return {"channel": channel, "messages": snapshot}


def main() -> None:
    hub = Hub()

    def publish_channel(client, channel: str, messages: list[dict]) -> None:
        topic = f"xonix/chat/log/{channel}"
        client.publish(topic, json.dumps({"messages": messages}, ensure_ascii=False), qos=0, retain=True)

    def on_message(client, userdata, msg) -> None:
        if msg.topic == "xonix/game/state":
            # Не парсим содержимое — оно нужно только как признак "движок жив"
            # для самоочистки по таймауту (см. STARTUP_GRACE/STATE_STALE_TIMEOUT
            # ниже), счёт партии в общий чат больше не подмешиваем.
            hub.last_state_ts = time.monotonic()
            return

        try:
            payload = json.loads(msg.payload)
            text = str(payload.get("text", ""))
            channel_hint = payload.get("channel", "general")
            usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else None
        except (json.JSONDecodeError, TypeError):
            return
        if not text:
            return

        if msg.topic == "xonix/chat/post/user":
            sender = str(payload.get("sender", "user")) or "user"
            channel = "general"  # пользователь пишет только в общий канал
            if sender == "user":
                # Поле ввода на дашборде без state_topic держит на экране
                # то, что было набрано, — повторный Enter с тем же текстом
                # ничего не публикует (HA не видит изменения, выглядит как
                # "отправка не работает"). Ставим пустую строку в отдельный
                # state_topic поля сразу после обработки — поле визуально
                # очищается, как в обычном чате.
                client.publish("xonix/chat/ui/general_send_ack", "", qos=0, retain=False)
        elif msg.topic == "xonix/chat/post/p1":
            sender = "p1"
            channel = "private_p1" if channel_hint == "private" else "general"
        elif msg.topic == "xonix/chat/post/p2":
            sender = "p2"
            channel = "private_p2" if channel_hint == "private" else "general"
        else:
            return

        result = hub.append(channel, sender, text, usage=usage)
        if result:
            publish_channel(client, channel, result["messages"])

    client = mqtt.Client()
    client.username_pw_set(MQTT_USER, MQTT_PASS)
    client.on_message = on_message
    client.connect(MQTT_HOST, MQTT_PORT, keepalive=30)
    client.subscribe("xonix/game/state", qos=0)
    client.subscribe("xonix/chat/post/user", qos=1)
    client.subscribe("xonix/chat/post/p1", qos=1)
    client.subscribe("xonix/chat/post/p2", qos=1)
    client.loop_start()

    # переиздать сохранённую историю сразу при старте — дашборд не должен
    # ждать первого нового сообщения, чтобы увидеть то, что уже было
    for c in CHANNELS:
        publish_channel(client, c, hub.history[c])

    start_ts = time.monotonic()
    while True:
        t0 = time.monotonic()

        if hub.last_state_ts is None:
            if t0 - start_ts > STARTUP_GRACE:
                return
        elif (t0 - hub.last_state_ts) > STATE_STALE_TIMEOUT:
            return

        time.sleep(1.0)


if __name__ == "__main__":
    main()
