#!/usr/bin/env python3
"""Сборщик телеметрии камеры ворот для оверлея (vorota_overlay.sh → vorota_overlay_render.py).

Аналог overlay_collect.sh с iMac и рендерера XPS, но данных о «машине» тут нет — вместо них сама камера
(XM530 по DVRIP: кодировщик, экспозиция, день/ночь, шумодав, цвет, прожектор, система), Frigate
(/api/stats: fps, инференс, CPU процессов, качество связи), go2rtc (живой битрейт по счётчику принятых
байт), наш ffmpeg (-progress → frame.txt: фактические к/с и кадры; signalstats → yavg.txt: освещённость)
и Home Assistant (подгруппа «Home Assistant» из /local/xps_overlay_extra.txt — тот же файл, что для XPS,
его раз в минуту кладёт автоматизация «Камеры: данные HA в оверлеи»).
Раз в 2 с пишет DIR/telemetry.txt (формат см. в рендерере); «медленные» DVRIP-запросы — раз в 10 с,
SystemInfo — один раз. Аргумент — PID родителя: исчез — выходим. ONCE=1 — одна итерация (отладка).
"""
import json
import os
import socket
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import xm_dvrip  # noqa: E402

DIR = os.environ.get("OV_DIR", "/tmp/vorota_overlay")
CAM_IP = xm_dvrip.HOST
FRIGATE = "http://127.0.0.1:5000/api"
GO2RTC = "http://127.0.0.1:1984/api/streams?src=vorota_cam"   # чистый поток камеры (с 2026-09-15 vorota — с оверлеем)
GO2RTC_AUTH = os.environ.get("GO2RTC_AUTH", "")   # "user:pass", передаёт vorota_overlay.sh
HA_EXTRA = "http://192.168.77.2:8123/local/xps_overlay_extra.txt"


def http(url, timeout=3, auth=""):
    req = urllib.request.Request(url)
    if auth:
        import base64
        req.add_header("Authorization", "Basic " + base64.b64encode(auth.encode()).decode())
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def tcp_ping_ms(host, port=554):
    t = time.monotonic()
    try:
        s = socket.create_connection((host, port), timeout=2)
        s.close()
        return f"{(time.monotonic() - t) * 1000:.3f} мс"
    except OSError:
        return "нет ответа"


def hexint(v):
    try:
        return int(v, 16) if isinstance(v, str) else int(v)
    except (TypeError, ValueError):
        return 0


def fmt_shutter(us):
    us = hexint(us)
    return f"{us / 1000:.2f} мс" if us >= 1000 else f"{us} мкс"


class Dvrip:
    def __init__(self):
        self.s = None
        self.sysinfo = None
        self.slow = {}
        self.fail_at = -1e9   # monotonic-время последнего неудачного логина

    def _ensure(self):
        if self.s is None:
            # 2026-09-15: учётка admin была найдена заблокированной (Ret 205 «пользователь заблокирован»).
            # Раньше при любом отказе refresh_slow делал до 6 новых логинов за цикл каждые 2 с — сам
            # подкармливал блокировку. Теперь после отказа — пауза 60 с, потом одна попытка.
            if time.monotonic() - self.fail_at < 60:
                raise RuntimeError("DVRIP login: пауза после отказа")
            try:
                s = xm_dvrip.DvripSession()
                r = s.login()
            except Exception:
                self.fail_at = time.monotonic()
                raise
            if r.get("Ret") != 100:
                try:
                    s.sock.close()
                except Exception:
                    pass
                self.fail_at = time.monotonic()
                raise RuntimeError(f"DVRIP login Ret {r.get('Ret')}")
            self.s = s

    def get(self, name):
        self._ensure()
        try:
            r = self.s.config_get(name)
        except Exception:
            self.s = None
            raise
        v = r.get(name)
        return v[0] if isinstance(v, list) and v and isinstance(v[0], (dict, list)) else v

    def system_info(self):
        self._ensure()
        r = self.s._send(1020, {"Name": "SystemInfo", "SessionID": self.s._hex_session()})
        return r.get("SystemInfo") or {}

    def refresh_slow(self):
        for key in ["Simplify.Encode", "Camera.Param", "Camera.ParamEx", "AVEnc.VideoColor", "Camera.ClearFog", "Camera.WhiteLight"]:
            try:
                self.slow[key] = self.get(key)
            except Exception as e:
                self.slow.setdefault(key, None)
                print("dvrip", key, e, file=sys.stderr)
        if self.sysinfo is None:
            try:
                self.sysinfo = self.system_info()
            except Exception as e:
                print("dvrip SystemInfo", e, file=sys.stderr)


def camera_lines(d):
    L = []
    enc = (d.slow.get("Simplify.Encode") or {}).get("MainFormat", {}).get("Video", {})
    L.append("# Поток")
    L.append("Камера: XM530 IPG-X4C-WER")
    L.append("- Объективы: 2 (широкий + зум)")
    L.append("Разрешение: 1920x2160 пикс.")
    if enc:
        L.append(f"- Частота: {enc.get('FPS', '?')} к/с")
        L.append(f"Кодек: {enc.get('Compression', '?')}")
        L.append(f"- Кодер: камера, {enc.get('BitRateControl', '?')}")
        L.append(f"Потолок: {hexint(enc.get('BitRate', 0)) / 1024:.1f} Мбит/с")
        L.append(f"- Качество: {enc.get('Quality', '?')} из 6")
        L.append(f"- GOP: {enc.get('GOP', '?')} с")
    return L


def camera_param_lines(d):
    L = []
    p = d.slow.get("Camera.Param") or {}
    px = d.slow.get("Camera.ParamEx") or {}
    vc = d.slow.get("AVEnc.VideoColor")
    fog = d.slow.get("Camera.ClearFog") or {}
    wl = d.slow.get("Camera.WhiteLight") or {}
    if p:
        L.append("# Камера (DVRIP)")
        ep = p.get("ExposureParam", {})
        lvl = hexint(ep.get("Level", 0))
        L.append("Экспозиция: " + ("авто" if lvl == 0 else f"ручная, ур. {lvl}"))
        L.append(f"- Выдержка: {fmt_shutter(ep.get('LeastTime', 0))} … {fmt_shutter(ep.get('MostTime', 0))}")
        gp = p.get("GainParam", {})
        L.append("Усиление: " + ("авто" if gp.get("AutoGain") == 1 else "ручное") + f", до {gp.get('Gain', '?')}")
        L.append("Баланс белого: " + ("авто" if hexint(p.get("WhiteBalance", 0)) == 0 else f"режим {hexint(p.get('WhiteBalance'))}"))
        dn = hexint(p.get("DayNightColor", 0))
        L.append("День/ночь: " + {0: "авто", 1: "цвет", 2: "ч/б", 3: "авто"}.get(dn, f"режим {dn}"))
        L.append("- ИК-фильтр: " + ("авто" if p.get("IRCUTMode") == 0 else f"режим {p.get('IRCUTMode')}"))
        L.append(f"- Порог день/ночь: {p.get('DncThr', '?')}")
        L.append(f"Шумодав день: {p.get('Day_nfLevel', '?')} из 5")
        L.append(f"- Ночь: {p.get('Night_nfLevel', '?')} из 5")
        L.append("Засветка (BLC): " + ("выкл" if hexint(p.get("BLCMode", 0)) == 0 else "вкл"))
        L.append("- Антипересвет: " + ("вкл" if px.get("PreventOverExpo") else "выкл"))
        L.append("- Антитуман: " + (f"вкл, {fog.get('level', '?')}" if fog.get("enable") else "выкл"))
        L.append("- Дисторсия: " + ("вкл" if px.get("Ldc") else "выкл"))
        L.append("Отражение: " + ("нет" if hexint(p.get("PictureFlip", 0)) == 0 and hexint(p.get("PictureMirror", 0)) == 0 else "да"))
    if isinstance(vc, list) and vc:
        c = vc[0].get("VideoColorParam", {}) if isinstance(vc[0], dict) else {}
        if c:
            L.append(f"Яркость: {c.get('Brightness', '?')} ед.")
            L.append(f"- Контраст: {c.get('Contrast', '?')} ед.")
            L.append(f"- Насыщенность: {c.get('Saturation', '?')} ед.")
            L.append(f"- Оттенок: {c.get('Hue', '?')} ед.")
            L.append(f"- Резкость (код): {c.get('Acutance', '?')}")
    if wl:
        mode = {"Intelligent": "по движению", "Close": "выкл", "Open": "вкл", "Auto": "авто"}.get(wl.get("WorkMode"), wl.get("WorkMode", "?"))
        L.append(f"Прожектор: {mode}")
        wp = wl.get("WorkPeriod", {})
        if wp.get("Enable"):
            L.append(f"- Период: {wp.get('SHour', 0):02d}:{wp.get('SMinute', 0):02d}–{wp.get('EHour', 0):02d}:{wp.get('EMinute', 0):02d}")
        L.append(f"- Яркость: {wl.get('Brightness', '?')} %")
    return L


def frigate_lines(stats, ptz):
    L = ["# Frigate"]
    c = (stats or {}).get("cameras", {}).get("vorota", {})
    if c:
        L.append(f"Захват: {c.get('camera_fps', '?')} к/с")
        L.append(f"- Детекция: {c.get('detection_fps', '?')} к/с")
        L.append(f"- Пропущено: {c.get('skipped_fps', '?')} к/с")
        L.append("Связь: " + {"excellent": "отличная", "good": "хорошая", "fair": "средняя", "poor": "плохая"}.get(c.get("connection_quality"), str(c.get("connection_quality", "?"))))
        L.append(f"- Обрывов за час: {c.get('reconnects_last_hour', '?')}")
        L.append(f"- Задержек за час: {c.get('stalls_last_hour', '?')}")
        L.append(f"CPU записи: {c.get('ffmpeg_cpu', '?')} %")
        L.append(f"- Захват: {c.get('capture_cpu', '?')} %")
        L.append(f"- Детект: {c.get('detect_cpu', '?')} %")
    det = (stats or {}).get("detectors", {})
    for name, dv in det.items():
        L.append(f"Инференс ({name}): {dv.get('inference_speed', '?')} мс")
        break
    st = (stats or {}).get("service", {}).get("storage", {}).get("/media/frigate/recordings", {})
    if st:
        L.append(f"Диск свободно: {st.get('free', 0) / 1024:.1f} ГБ")
    if ptz:
        L.append("# PTZ")
        feats = ptz.get("features", [])
        L.append("Поворот/наклон: " + ("есть" if "pt" in feats else "нет"))
        L.append("- Зум: " + ("есть" if "zoom" in feats else "нет"))
        L.append("- Фокус: " + ("есть" if "focus" in feats else "нет"))
        L.append(f"Пресетов: {len(ptz.get('presets', []))}")
    return L


def main():
    parent = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 0
    os.makedirs(DIR, exist_ok=True)
    d = Dvrip()
    n = 0
    prev_bytes = None
    prev_t = None
    bitrate = ""
    fps = ""
    pfr = pot = None
    extra = ""
    extra_t = 0
    ptz = None
    while True:
        # раз в 10 с; пока камера не ответила ни разу (после быстрого перезапуска потока XM отказывает
        # в логине несколько секунд) — пробуем каждую итерацию
        if n % 5 == 0 or not d.slow.get("Simplify.Encode"):
            d.refresh_slow()
        if n % 30 == 0:
            try:
                ptz = json.loads(http(f"{FRIGATE}/vorota/ptz/info"))
            except Exception:
                ptz = None
        n += 1
        stats = None
        try:
            stats = json.loads(http(f"{FRIGATE}/stats"))
        except Exception as e:
            print("frigate stats", e, file=sys.stderr)
        # живой битрейт — по счётчику принятых байт у go2rtc-продюсера
        try:
            g = json.loads(http(GO2RTC, auth=GO2RTC_AUTH))
            b = (g.get("producers") or [{}])[0].get("bytes_recv")
            t = time.monotonic()
            if b is not None and prev_bytes is not None and b >= prev_bytes and t - prev_t >= 1.5:
                bitrate = f"{(b - prev_bytes) * 8 / (t - prev_t) / 1e6:.4f} Мбит/с"
                prev_bytes, prev_t = b, t
            elif prev_bytes is None and b is not None:
                prev_bytes, prev_t = b, t
        except Exception as e:
            print("go2rtc", e, file=sys.stderr)
        # фактические к/с нашего ffmpeg — по frame.txt («кадр микросекунды» от читателя -progress)
        try:
            fr, ot = open(f"{DIR}/frame.txt").read().split()
            fr, ot = int(fr), int(ot)
            if pfr is not None and ot > pot and ot - pot >= 1500000:
                fps = f"{(fr - pfr) / ((ot - pot) / 1e6):.4f} к/с"
                pfr, pot = fr, ot
            elif pfr is None:
                pfr, pot = fr, ot
        except (OSError, ValueError):
            pass
        light = ""
        try:
            light = f"{float(open(f'{DIR}/yavg.txt').read().split()[0]) / 255 * 100:.4f} %"
        except (OSError, ValueError, IndexError):
            pass
        if time.monotonic() - extra_t > 60:
            try:
                extra = http(HA_EXTRA, timeout=4)
            except Exception as e:
                print("ha extra", e, file=sys.stderr)
            extra_t = time.monotonic()
        ping = tcp_ping_ms(CAM_IP)

        L = camera_lines(d)
        if bitrate:
            L.append(f"Битрейт факт.: {bitrate}")
        if fps:
            L.append(f"К/с в оверлее: {fps}")
        if light:
            L.append(f"Освещённость: {light}")
        L += camera_param_lines(d)
        L.append("[center]")
        L.append("# Сеть")
        L.append(f"Адрес LAN: {CAM_IP}")
        L.append("- Линк: Wi-Fi, сеть 77")
        L.append("- MAC: 60:de:f4:1b:7b:2e")
        L.append(f"Отклик (TCP): {ping}")
        L.append("Путь: камера→go2rtc→ffmpeg")
        L.append("- Декод: программный (HEVC)")
        L.append("- Кодер оверлея: h264_vaapi")
        si = d.sysinfo or {}
        if si:
            L.append("# Система")
            L.append(f"Модель: {si.get('DeviceModel', '?')}")
            L.append(f"- Платформа: {si.get('HardWare', '?').split('_')[0]}")
            sw = si.get("SoftWareVersion", "?")
            L.append(f"Прошивка: {'.'.join(sw.split('.')[:3])}")
            L.append(f"- Сборка: {si.get('BuildTime', '?')[:10]}")
            L.append(f"Серийный: {si.get('SerialNo', '?')}")
            L.append(f"Аудио: вход {si.get('AudioInChannel', 0)}, разг. {si.get('TalkOutChannel', 0)}")
        L.append("[right]")
        if extra:
            L += [ln for ln in extra.split("\n") if ln and not ln.startswith("[")]
        # четвёртая плашка внизу слева (как GPU-блок на XPS): Frigate и PTZ — иначе при крупном шрифте
        # три верхних столбика не помещаются в 1920 px по ширине
        L.append("[bottomleft]")
        L += frigate_lines(stats, ptz)
        tmp = f"{DIR}/telemetry.txt.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("\n".join(L) + "\n")
        os.replace(tmp, f"{DIR}/telemetry.txt")
        if os.environ.get("ONCE") == "1":
            break
        if parent and not os.path.exists(f"/proc/{parent}"):
            break
        time.sleep(2)
    # закрыть DVRIP-сессию: XM держит незакрытые сессии и на быстрый повторный старт потока
    # (frame.jpeg стартует и гасит exec за секунды) отвечает отказом в логине
    try:
        if d.s is not None and getattr(d.s, "sock", None):
            d.s.sock.close()
    except Exception:
        pass


if __name__ == "__main__":
    main()
