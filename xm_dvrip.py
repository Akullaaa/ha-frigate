#!/usr/bin/env python3
# Копия /config/.local/bin/vorota_dvrip.py для контейнера Frigate (ему не виден /config HA): DVRIP-клиент XM530 для
# сборщика оверлея vorota_overlay_collect.py. Правки — синхронно с оригиналом.
"""Клиент бинарного SDK-протокола DVRIP/NetSurveillance ("Sofia") камеры vorota
(192.168.77.30:34567, до 2026-09-15 — 10.0.0.12) — по образцу /config/.local/bin/dvor_dvrip.py (та же платформа
камеры, тот же протокол). В отличие от dvor, отдельную DVRIP-учётку здесь не
заводили — подошли штатные ONVIF-креды (ahuser/bbbbbbb7), они же общие для
нескольких камер в этом конфиге.

2026-09-09: ConfigGet/ConfigSet (1042/1040) читают и пишут "Camera" целиком.
Найдено Camera.Param[0].PictureFlip/PictureMirror ("0x00000000"/"0x00000001").
Запись PictureFlip=1 подтверждена (Ret:100, значение сохраняется при повторном
чтении, переживает даже перезагрузку камеры через ONVIF reboot) — но реального
эффекта на видео (RTSP stream=0) не даёт ни сразу, ни после переподключения,
ни после полной перезагрузки камеры. Причина не выяснена (возможно, эта модель/
прошивка не применяет параметр к каналу кодирования, либо связано со сдвоенным
кадром 1920x2160 камеры — см. project_vorota_dvrip_flip.md). Оставлено включённым
как есть, реальный переворот сделан софтверно (-vf vflip в config.yaml Frigate).

Write-операции пользователей (Add/ModifyUser и т.д., msgId 1476+) НЕ проверялись
и не нужны — в отличие от dvor, здесь такой задачи не было.
"""
import hashlib
import json
import socket
import struct

HOST = "192.168.77.30"  # камера ворот в сети 77 (с 2026-09-15)
PORT = 34567
USERNAME = "admin"
PASSWORD = ""  # после сброса 2026-09-15 пароль admin пустой (см. device_credentials/vorota.txt в /config)

MSG_LOGIN_REQ = 1000
MSG_CONFIG_SET_REQ = 1040
MSG_CONFIG_GET_REQ = 1042


def sofia_hash(password: str) -> str:
    md5 = hashlib.md5(password.encode("utf-8")).digest()
    chars = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
    result = ""
    for i in range(8):
        n = (md5[2 * i] + md5[2 * i + 1]) % len(chars)
        result += chars[n]
    return result


class DvripSession:
    def __init__(self, host=HOST, port=PORT):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(8)
        self.sock.connect((host, port))
        self.session_id = 0
        self.seq = 0

    def _pack(self, msg_id: int, obj: dict) -> bytes:
        # ensure_ascii=False обязателен — прошивка тихо стирает поле в пустую строку
        # при стандартном \uXXXX-экранировании кириллицы (та же грабля, что и с OSD dvor).
        body = (json.dumps(obj, ensure_ascii=False) + "\x0a").encode("utf-8")
        header = struct.pack(
            "<BBHIIBBHI",
            0xFF, 0x00, 0x00, self.session_id,
            self.seq, 0, 0, msg_id, len(body),
        )
        self.seq += 1
        return header + body

    def _send(self, msg_id: int, obj: dict) -> dict:
        pkt = self._pack(msg_id, obj)
        self.sock.sendall(pkt)
        header = self._recv_exact(20)
        (head, ver, _res, session_id, seq, _tot, _cur, resp_msg_id, data_len) = \
            struct.unpack("<BBHIIBBHI", header)
        data = self._recv_exact(data_len) if data_len else b""
        text = data.rstrip(b"\x0a\x00").decode("utf-8", errors="replace")
        try:
            resp = json.loads(text) if text else {}
        except json.JSONDecodeError:
            resp = {"_raw": text}
        resp["_msgId"] = resp_msg_id
        return resp

    def _recv_exact(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError(f"socket closed, got {len(buf)}/{n} bytes")
            buf += chunk
        return buf

    def login(self, username=USERNAME, password=PASSWORD):
        obj = {
            "EncryptType": "MD5",
            "LoginType": "DVRIP-Web",
            "PassWord": sofia_hash(password),
            "UserName": username,
        }
        resp = self._send(MSG_LOGIN_REQ, obj)
        sid = resp.get("SessionID")
        if sid:
            self.session_id = int(sid, 16) if isinstance(sid, str) else sid
        return resp

    def _hex_session(self):
        return "0x%08x" % self.session_id

    def config_get(self, name):
        obj = {"Name": name, "SessionID": self._hex_session()}
        return self._send(MSG_CONFIG_GET_REQ, obj)

    def config_set(self, name, value):
        obj = {"Name": name, name: value, "SessionID": self._hex_session()}
        return self._send(MSG_CONFIG_SET_REQ, obj)


if __name__ == "__main__":
    s = DvripSession()
    print("login:", s.login())
    print("Camera:", json.dumps(s.config_get("Camera"), ensure_ascii=False, indent=2))
