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
"""
import base64
import sys
import time
import urllib.request

URL = "http://10.0.0.49/axis-cgi/mjpg/video.cgi"
USER, PASSWORD = "ahuserview", "bbbbbbb7"
SOI = b"\xff\xd8"
EOI = b"\xff\xd9"


def stream_frames() -> None:
    out = sys.stdout.buffer
    token = base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()
    req = urllib.request.Request(URL, headers={"Authorization": f"Basic {token}"})
    with urllib.request.urlopen(req, timeout=15) as resp:
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


def main() -> None:
    while True:
        try:
            stream_frames()
        except Exception as exc:
            print(f"axis_2100_bridge: {exc}", file=sys.stderr, flush=True)
        time.sleep(1)


if __name__ == "__main__":
    main()
