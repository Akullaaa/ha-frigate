#!/usr/bin/env python3
"""Мост для axis_2100: единственное HTTP-подключение к камере (MJPEG,
multipart без Content-Length и с "голыми" \n вместо \r\n у Axis 2100 —
именно это, судя по всему, ломает нативный демультиплексор ffmpeg/go2rtc,
см. project_xonix_cameras_composite и device_credentials/... заметки).

Вместо того чтобы полагаться на multipart-заголовки, ищем сами JPEG-кадры
по маркерам SOI/EOI (0xFFD8...0xFFD9) прямо в потоке байт и пишем их в
stdout подряд, без обёртки — это стандартный "raw MJPEG"-формат, который
ffmpeg (-f mjpeg -i pipe:0) читает надёжно, без проб на Content-Length.

Один процесс = ровно одно физическое соединение к камере (правило проекта —
не нагружать камеры вторым потоком, см. project_xonix_cameras_composite).

События (подключился/первый кадр/переподключение) публикуются в MQTT
("axis_2100/bridge/event") — HA слушает топик и пишет их в Logbook, см.
mqtt.yaml/automations.yaml в основном репо ("ha"). Публикация — свой минимальный
MQTT-клиент на сокетах (CONNECT+PUBLISH QoS0), без paho-mqtt: наличие пакета
в этом окружении не подтверждено (в отличие от numpy — тот уже использовался
в xonix_compositor_*.py), а протокол для fire-and-forget публикации простой
и не стоит тянуть внешнюю зависимость ради него.
"""
import base64
import os
import socket
import sys
import time
import urllib.request

URL = "http://10.0.0.49/axis-cgi/mjpg/video.cgi"
USER, PASSWORD = "ahuserview", "bbbbbbb7"
SOI = b"\xff\xd8"
EOI = b"\xff\xd9"

MQTT_HOST, MQTT_PORT = "core-mosquitto", 1883
MQTT_USER, MQTT_PASSWORD = "frigateu", "qqqqqqq7"
MQTT_TOPIC = "axis_2100/bridge/event"
HEARTBEAT_EVERY = 40  # кадров (~30-35с при апсемпленных 1.2fps)


def _remaining_length(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n % 128
        n //= 128
        if n > 0:
            b |= 0x80
        out.append(b)
        if n == 0:
            return bytes(out)


def _mqtt_connect(sock: socket.socket, client_id: str) -> bool:
    payload = b"".join(
        len(s).to_bytes(2, "big") + s.encode()
        for s in (client_id, MQTT_USER, MQTT_PASSWORD)
    )
    variable_header = b"\x00\x04MQTT\x04\xc2\x00\x1e"  # v3.1.1, user+pass+clean session, keepalive=30s
    remaining = variable_header + payload
    sock.sendall(b"\x10" + _remaining_length(len(remaining)) + remaining)
    resp = sock.recv(4)
    return len(resp) >= 4 and resp[0] == 0x20 and resp[3] == 0


def mqtt_log(msg: str) -> None:
    """Публикует событие в MQTT; любая ошибка тут не должна ронять сам мост."""
    try:
        payload = f"{msg} ({time.strftime('%H:%M:%S')})".encode("utf-8")
        with socket.create_connection((MQTT_HOST, MQTT_PORT), timeout=3) as sock:
            if not _mqtt_connect(sock, f"axis2100bridge-{os.getpid()}"):
                return
            topic_b = MQTT_TOPIC.encode()
            variable_header = len(topic_b).to_bytes(2, "big") + topic_b
            remaining = variable_header + payload
            sock.sendall(b"\x30" + _remaining_length(len(remaining)) + remaining)
    except OSError:
        pass


def stream_frames() -> None:
    out = sys.stdout.buffer
    mqtt_log("подключаюсь к камере")
    token = base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()
    req = urllib.request.Request(URL, headers={"Authorization": f"Basic {token}"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        mqtt_log("подключился к камере")
        first_frame = True
        frame_count = 0
        buf = bytearray()
        while True:
            chunk = resp.read(8192)
            if not chunk:
                break
            buf.extend(chunk)
            while True:
                start = buf.find(SOI)
                if start == -1:
                    # оставляем последний байт на случай, если 0xff разорван между чанками
                    if len(buf) > 1:
                        del buf[:-1]
                    break
                end = buf.find(EOI, start + 2)
                if end == -1:
                    if start > 0:
                        del buf[:start]
                    break
                frame_end = end + 2
                out.write(bytes(buf[start:frame_end]))
                out.flush()
                del buf[:frame_end]
                frame_count += 1
                if first_frame:
                    mqtt_log("первый кадр получен")
                    first_frame = False
                elif frame_count % HEARTBEAT_EVERY == 0:
                    mqtt_log(f"жив, кадров получено: {frame_count}")


def main() -> None:
    while True:
        try:
            stream_frames()
        except Exception as exc:
            mqtt_log(f"переподключение: {exc}")
            print(f"axis_2100_bridge: {exc}", file=sys.stderr, flush=True)
        time.sleep(1)


if __name__ == "__main__":
    main()
