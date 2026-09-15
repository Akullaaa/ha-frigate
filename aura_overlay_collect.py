#!/usr/bin/env python3
"""Сборщик телеметрии камеры ворот для оверлея (xm530_overlay.sh → xm530_overlay_render.py).

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

# 2026-09-15: сборщик общий для камер (термин пользователя — «аура»); имя камеры go2rtc — OV_CAM от aura_overlay.sh.
# DVRIP/PTZ есть только у xm530; у tambur (LookCam, Wi-Fi сеть 77) — только сеть, железо, Frigate и HA.
CAM = os.environ.get("OV_CAM", "xm530")
CAM_INFO = {
    "xm530": {"ip": xm_dvrip.HOST, "model": "IPG-X4C-WER", "link": "Wi-Fi, сеть 77", "dvrip": (xm_dvrip.HOST, xm_dvrip.USERNAME, xm_dvrip.PASSWORD)},
    "tambur": {"ip": "192.168.77.9", "model": "LookCam, HEVC 1080p", "link": "Wi-Fi, сеть 77"},
    # двор (бывш. dvor, 2026-09-15): чип XM530V200, DVRIP-учётка из device_credentials/x2_wq_bl.txt (бывш. dvor.txt)
    "x2_wq_bl": {"ip": "10.0.0.7", "model": "X2-WQ-BL (XM530)", "link": "Ethernet, сеть Bezeq", "dvrip": ("10.0.0.7", "алёна", "LULX93o72Wml")},
    "axis_2100": {"ip": "192.168.77.16", "model": "AXIS 2100, MJPEG через мост", "link": "Ethernet"},
}
DIR = os.environ.get("OV_DIR", f"/tmp/{CAM}_overlay")
# 2026-09-15: у камер 16:9 (три колонки в 720 px, ~20 строк на столбик) полный набор DVRIP-строк не влезает —
# в компактном режиме (не два списка) «Поток» и «Камера (DVRIP)» ужаты до главного
COMPACT = os.environ.get("OV_TWO_LISTS") != "1"
CAM_IP = CAM_INFO.get(CAM, CAM_INFO["xm530"])["ip"]
FRIGATE = "http://127.0.0.1:5000/api"
GO2RTC = f"http://127.0.0.1:1984/api/streams?src={CAM}_cam"   # чистый поток камеры (с 2026-09-15 xm530 — с оверлеем)
GO2RTC_AUTH = os.environ.get("GO2RTC_AUTH", "")   # "user:pass", передаёт xm530_overlay.sh
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
        self.backoff = 15     # пауза после отказа, с: 15 → 30 → … → 600 (сброс при успехе)

    def _ensure(self):
        if self.s is None:
            # 2026-09-15: учётка admin была найдена заблокированной (Ret 205 «пользователь заблокирован»).
            # Раньше при любом отказе refresh_slow делал до 6 новых логинов за цикл каждые 2 с — сам
            # подкармливал блокировку. Теперь после отказа — растущая пауза (15 с … 10 мин), потом одна попытка:
            # короткая в начале, потому что после быстрого перезапуска потока XM штатно отказывает несколько секунд.
            if time.monotonic() - self.fail_at < self.backoff:
                raise RuntimeError("DVRIP login: пауза после отказа")
            # файл-стоп: пока он есть, к DVRIP не ходим вообще (2026-09-15: чтобы дать блокировке admin
            # истечь без единой попытки; /config здесь — каталог конфига аддона Frigate)
            if os.path.exists(f"/config/{CAM}_dvrip_pause"):
                self.fail_at = time.monotonic()
                raise RuntimeError("DVRIP login: пауза после отказа")
            try:
                host, user, pw = CAM_INFO[CAM]["dvrip"]
                s = xm_dvrip.DvripSession(host=host)
                r = s.login(user, pw)
            except Exception:
                self.fail_at = time.monotonic()
                self.backoff = min(self.backoff * 2, 600)
                raise
            if r.get("Ret") != 100:
                try:
                    s.sock.close()
                except Exception:
                    pass
                self.fail_at = time.monotonic()
                self.backoff = min(self.backoff * 2, 600)
                print(f"dvrip login Ret {r.get('Ret')}, следующая попытка через {self.backoff} с", file=sys.stderr)
                raise RuntimeError(f"DVRIP login Ret {r.get('Ret')}")
            self.s = s
            self.backoff = 15

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
        if "dvrip" not in CAM_INFO.get(CAM, {}):   # DVRIP только у камер Xiongmai (xm530, x2_wq_bl)
            return
        for key in ["Simplify.Encode", "Camera.Param", "Camera.ParamEx", "AVEnc.VideoColor", "Camera.ClearFog", "Camera.WhiteLight"]:
            try:
                self.slow[key] = self.get(key)
            except Exception as e:
                self.slow.setdefault(key, None)
                if "пауза после отказа" not in str(e):   # сам отказ уже залогирован в _ensure, паузу не повторять
                    print("dvrip", key, e, file=sys.stderr)
        if self.sysinfo is None:
            try:
                self.sysinfo = self.system_info()
            except Exception as e:
                if "пауза после отказа" not in str(e):
                    print("dvrip SystemInfo", e, file=sys.stderr)


class HwStats:
    """Железо хоста (Оптиплекс, общий с HA/Frigate): CPU по /proc/stat, GPU i915 по rc6_residency_ms
    (занятость = 1 − Δпростоя/Δвремени), температура пакета, частоты, память. Всё — sysfs/procfs хоста,
    видны из контейнера Frigate. Разницы считаются между вызовами (раз в цикл, 2 с). 2026-09-15 по просьбе
    пользователя («остальные параметры по железу, GPU, нагрузка»)."""

    def __init__(self):
        self.p_stat = None
        self.p_rc6 = None
        self.p_t = None
        self.cpu = self.gpu = None

    @staticmethod
    def _read(path):
        with open(path) as f:
            return f.read().strip()

    def tick(self):
        t = time.monotonic()
        try:
            f = self._read("/proc/stat").split("\n")[0].split()[1:]
            v = list(map(int, f))
            idle, total = v[3] + v[4], sum(v)
            if self.p_stat:
                di, dt = idle - self.p_stat[0], total - self.p_stat[1]
                if dt > 0:
                    self.cpu = 100 * (1 - di / dt)
            self.p_stat = (idle, total)
        except (OSError, ValueError, IndexError):
            pass
        try:
            rc6 = int(self._read("/sys/class/drm/card0/power/rc6_residency_ms"))
            if self.p_rc6 is not None and t > self.p_t:
                self.gpu = max(0.0, min(100.0, 100 * (1 - (rc6 - self.p_rc6) / ((t - self.p_t) * 1000))))
            self.p_rc6, self.p_t = rc6, t
        except (OSError, ValueError):
            pass

    def lines(self):
        L = ["# Железо (Оптиплекс)"]
        try:
            la = self._read("/proc/loadavg").split()[0]
        except OSError:
            la = "?"
        # 2026-09-15: частоты CPU/GPU слиты в строки CPU/GPU ради шрифта 1.6 (два списка впритык по высоте)
        ghz = ""
        try:
            mhz = [float(l.split(":")[1]) for l in open("/proc/cpuinfo") if l.startswith("cpu MHz")]
            ghz = f", {sum(mhz) / len(mhz) / 1000:.2f} ГГц"
        except (OSError, ValueError, ZeroDivisionError):
            pass
        L.append("CPU: " + (f"{self.cpu:.1f} %" if self.cpu is not None else "…") + ghz)
        L.append(f"- Нагрузка: {la} / {os.cpu_count()} ядер")
        try:
            temps = {}
            for z in os.listdir("/sys/class/thermal"):
                if z.startswith("thermal_zone"):
                    temps[self._read(f"/sys/class/thermal/{z}/type")] = int(self._read(f"/sys/class/thermal/{z}/temp")) / 1000
            tv = temps.get("x86_pkg_temp") or max(temps.values())
            L.append(f"- Темп.: {tv:.0f} °C")
        except (OSError, ValueError):
            pass
        gmhz = ""
        try:
            gmhz = f", {self._read('/sys/class/drm/card0/gt_act_freq_mhz')} МГц"
        except OSError:
            pass
        # 2026-09-15: строка GPU убрана — параметры iGPU Оптиплекса приходят из HA подгруппой «GPU Оптиплекс» во все
        # ауры (sensor.gpu_optiplex_*: 3D/Video, такт, RC6, мощность, память), чтобы цифры везде были одни и те же
        try:
            mi = {}
            for l in open("/proc/meminfo"):
                k, v = l.split(":")
                mi[k] = int(v.split()[0])
            L.append(f"Память: {(mi['MemTotal'] - mi['MemAvailable']) / 2**20:.1f} из {mi['MemTotal'] / 2**20:.1f} ГБ")
        except (OSError, ValueError, KeyError):
            pass
        return L


def camera_lines(d):
    L = []
    enc = (d.slow.get("Simplify.Encode") or {}).get("MainFormat", {}).get("Video", {})
    L.append("# Поток")
    L.append(f"Камера: {CAM_INFO.get(CAM, CAM_INFO['xm530'])['model']}")   # xm530 — чип XM530
    # 2026-09-15: строки «Объективы: 2 (широкий + зум)» и «Разрешение: 1920x2160 пикс.» убраны ради шрифта 1.3 —
    # статичные, оба списка в base заполняют высоту впритык
    if enc and COMPACT:   # 16:9 (три колонки, ~20 строк): только кодек и потолок
        L.append(f"Кодек: {enc.get('Compression', '?')}, {enc.get('FPS', '?')} к/с")
        L.append(f"Потолок: {hexint(enc.get('BitRate', 0)) / 1024:.1f} Мбит/с, Q{enc.get('Quality', '?')}")
    elif enc:
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
    if COMPACT:   # 16:9: только главное — экспозиция, усиление, день/ночь, прожектор
        keep = ("# ", "Экспозиция", "- Выдержка", "Усиление", "День/ночь", "- ИК-фильтр", "Прожектор")
        L = [l for l in L if l.startswith(keep)]
    return L


def frigate_lines(stats, ptz):
    L = ["# Frigate"]
    c = (stats or {}).get("cameras", {}).get(CAM, {})
    if c:
        L.append(f"Захват: {c.get('camera_fps', '?')} к/с")
        L.append(f"- Детекция: {c.get('detection_fps', '?')} к/с")
        # «Пропущено к/с», «Захват %», «Детект %» убраны 2026-09-15 ради шрифта 1.3 (см. выше про высоту)
        L.append("Связь: " + {"excellent": "отличная", "good": "хорошая", "fair": "средняя", "poor": "плохая"}.get(c.get("connection_quality"), str(c.get("connection_quality", "?"))))
        L.append(f"- Обрывов/ч: {c.get('reconnects_last_hour', '?')}")
        L.append(f"- Задержек/ч: {c.get('stalls_last_hour', '?')}")
        L.append(f"CPU записи: {c.get('ffmpeg_cpu', '?')} %")
    det = (stats or {}).get("detectors", {})
    for name, dv in det.items():
        L.append(f"Инференс: {dv.get('inference_speed', '?')} мс")   # детектор {name}
        break
    st = (stats or {}).get("service", {}).get("storage", {}).get("/media/frigate/recordings", {})
    if st:
        L.append(f"Диск: {st.get('free', 0) / 1024:.1f} ГБ")   # свободно
    if ptz:
        L.append("# PTZ")
        feats = ptz.get("features", [])
        # 2026-09-15: три строки «есть/нет» слиты в одну ради шрифта 1.6
        axes = [n for f, n in (("pt", "PT"), ("zoom", "зум"), ("focus", "фокус")) if f in feats]
        L.append("Оси: " + (", ".join(axes) if axes else "нет"))
        L.append(f"Пресетов: {len(ptz.get('presets', []))}")
    return L


def main():
    parent = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 0
    os.makedirs(DIR, exist_ok=True)
    d = Dvrip()
    hw = HwStats()
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
                ptz = json.loads(http(f"{FRIGATE}/{CAM}/ptz/info")) if CAM == "xm530" else None
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
        # звук камеры — RMS-уровень по astats из того же ffmpeg (audio.txt через FIFO, окно ~1 с)
        sound = ""
        try:
            sound = f"{float(open(f'{DIR}/audio.txt').read().split()[0]):.1f} дБ"
        except (OSError, ValueError, IndexError):
            pass
        hw.tick()
        if time.monotonic() - extra_t > 60:
            try:
                extra = http(HA_EXTRA, timeout=4)
            except Exception as e:
                print("ha extra", e, file=sys.stderr)
            extra_t = time.monotonic()
        ping = tcp_ping_ms(CAM_IP)

        L = camera_lines(d)
        if bitrate:
            L.append(f"Битрейт: {bitrate}")
        if fps:
            L.append(f"Кадров/с: {fps}")
        if light:
            L.append(f"Свет: {light}")
        if sound:
            L.append(f"Звук: {sound}")
        L += camera_param_lines(d)
        L.append("[center]")
        L.append("# Сеть")
        L.append(f"IP: {CAM_IP}")
        L.append(f"- Линк: {CAM_INFO.get(CAM, CAM_INFO['xm530'])['link']}")
        # «MAC: 60:de:f4:1b:7b:2e» убран 2026-09-15 ради шрифта 1.3 (есть в device_credentials/xm530.txt)
        L.append(f"Отклик: {ping}")
        # «Путь: камера→go2rtc→ffmpeg», «Декод: программный (HEVC)», «Кодер оверлея: h264_vaapi» убраны 2026-09-15
        # ради шрифта 1.4 — статичные и самые длинные строки левого списка (ширина двух списков впритык к 960 px)
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
        L += hw.lines()
        L.append("[right]")
        if extra:
            # 2026-09-15: подписи HA укорочены здесь (файл общий с XPS/iMac, автоматизацию не трогаем) —
            # ради шрифта 1.5 в base ширина правого списка упиралась в «Температура на улице»
            short = {"Температура на улице": "На улице", "Потребление дома": "Потребление", "Люди в кадрах камер:": "Люди в кадрах:"}
            for ln in extra.split("\n"):
                if ln and not ln.startswith("["):
                    for a, b in short.items():
                        ln = ln.replace(a, b)
                    L.append(ln)
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
