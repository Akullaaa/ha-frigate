#!/usr/bin/env python3
"""Третий, самый медленный уровень управления ботом — НЕ решает ходы (это
делает xonix_ai_agent.py каждые 0.25с), а раз в DECIDE_INTERVAL секунд
спрашивает настоящую LLM "как в целом сейчас играть" и публикует:
- числовые веса (aggression/caution) — как и раньше, подмешиваются в оценку
  хода эвристики;
- order (expand/raid/defend/regroup) — НАСТОЯЩИЙ новый канал управления:
  xonix_ai_agent.py реально меняет по нему поведение (дальность и
  агрессивность набегов), это не просто текст поверх старого механизма;
- private_message — приказ своему боту человеческим текстом, публикуется в
  приватный канал чата (xonix_chat_hub.py, private_p{N}) — виден только
  пользователю и самому этому стратегу, LLM другого игрока его не видит
  (физически не подписана на этот топик). Вместе с ним — usage (входные/
  выходные/закешированные токены этого конкретного запроса), хаб сохраняет
  его как есть в записи сообщения, дашборд рендерит рядом с репликой;
  плюс отдельный ретейн-топик xonix/game/p{N}/llm_usage — тот же снимок
  без прокрутки чата;
- general_message — необязательная реплика в ОБЩИЙ чат (пользователь + оба
  LLM-стратега), пусто, если сказать нечего — не спамим партию раз в 20с.

Текст-"характер" бота (xonix/game/p{N}/persona, редактируется с дашборда —
mqtt: text:) передаётся LLM как есть при каждом вызове, плюс свежий срез
обоих каналов чата (xonix_chat_hub.py) как разговорный контекст.

Почему не решает ходы напрямую: LLM отвечает секунду-другую, ходы нужны
4 раза в секунду — на порядок быстрее. Вместо этого LLM управляет
"характером" эвристики (через order/aggression/caution), а не заменяет её.

Два провайдера на выбор (xonix/game/p{N}/llm_provider — off/haiku/kimi,
переключается на лету; именно "off", не "none" — последнее зарезервировано
MQTT-интеграцией HA как сентинел "нет значения" и ломает отображение select,
проверено вживую 2026-08-20):
- haiku — Claude Haiku 4.5, ключ из device_credentials/anthropic_api_key.txt
  (нет файла — просто пропускает вызовы, как provider=off).
- kimi  — тот же Moonshot-ключ, что уже используется для Roo Code
  (device_credentials/moonshot_api_key.txt), НАПРЯМУЮ к api.moonshot.ai —
  локальный кэш-прокси на порту 8124 живёт в ДРУГОМ контейнере
  (Studio Code Server), из контейнера Frigate недоступен по localhost
  (та же сетевая изоляция аддонов, что уже не раз ловили в этом проекте).
  Вместо прокси — тот же приём кэширования (стабильный prompt_cache_key
  по хэшу системного промпта) реализован здесь напрямую.

По умолчанию provider=off — ни одного вызова API, ни одной копейки,
пока пользователь явно не выберет провайдера с дашборда.

Раздельные ключи и креды — по запросу пользователя, не хранить/угадывать
самостоятельно.
"""
import argparse
import hashlib
import json
import threading
import time
import urllib.error
import urllib.request

import paho.mqtt.client as mqtt

MQTT_HOST = "core-mosquitto"
MQTT_PORT = 1883
MQTT_USER = "frigateu"
MQTT_PASS = "qqqqqqq7"

DECIDE_INTERVAL = 20.0  # секунд между обращениями к LLM — не на каждый тик
STATE_STALE_TIMEOUT = 30.0  # если xonix/game/state молчит дольше — движка нет, выходим

# Цены USD за 1M токенов — тот же формат/источник, что и openAiCustomModelInfo
# у Roo Code для Kimi (см. алёна.md, раздел про Moonshot-прокси: вход 1.67,
# выход 8.33, чтение кэша 0.28 — курс уже заложен в исходных ¥/1M). Haiku 4.5
# подтверждён через skill claude-api (2026-08-21): $1.00/$5.00 за 1M,
# cache_read — стандартные ~0.1x входной цены (Anthropic), на практике не
# используется — кэш для Haiku тут всегда 0 (см. project_xonix_game.md).
PRICING_USD_PER_1M = {
    "haiku": {"input": 1.00, "output": 5.00, "cache_read": 0.10},
    "kimi": {"input": 1.67, "output": 8.33, "cache_read": 0.28},
}

COST_FILE = "/config/xonix_llm_cost.json"


def compute_cost_cents(provider: str, usage: dict) -> float:
    """Цена ОДНОГО запроса в центах — "как в Roo Code" (там тоже считают
    по USD/1M токенов из openAiCustomModelInfo). У Kimi input ВКЛЮЧАЕТ
    закешированные токены (OpenAI-стиль учёта, cached — подмножество input,
    не платить дважды), у Anthropic input УЖЕ БЕЗ кэша (отдельный счётчик
    cache_read) — эта асимметрия учтена явно, не единая формула на оба."""
    prices = PRICING_USD_PER_1M.get(provider)
    if not prices:
        return 0.0
    input_tok = usage.get("input", 0) or 0
    output_tok = usage.get("output", 0) or 0
    cached_tok = usage.get("cached", 0) or 0
    fresh_input = max(0, input_tok - cached_tok) if provider == "kimi" else input_tok
    usd = (
        fresh_input / 1_000_000 * prices["input"]
        + cached_tok / 1_000_000 * prices["cache_read"]
        + output_tok / 1_000_000 * prices["output"]
    )
    return usd * 100


def load_total_cost() -> dict:
    try:
        with open(COST_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return {k: float(v) for k, v in data.items()}
    except (FileNotFoundError, json.JSONDecodeError, ValueError, TypeError):
        return {}


def add_cost(consumer: str, cents: float) -> float:
    """Копит расход по потребителю (p1/p2/alena) в файле, переживающем
    рестарты — как и xonix_wins.json. Три процесса (p1/p2-стратеги +
    xonix_chat_alena.py) пишут в один файл без блокировки — гонка теоретически
    возможна (вызовы редкие, раз в ~8-20с, цена ошибки — потерянное
    обновление в статистике, не деньги), файловый лок ради этого не заводим."""
    data = load_total_cost()
    data[consumer] = data.get(consumer, 0.0) + cents
    try:
        with open(COST_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except OSError:
        pass
    return data[consumer]

ANTHROPIC_KEY_PATH = "/config/device_credentials/anthropic_api_key.txt"
MOONSHOT_KEY_PATH = "/config/device_credentials/moonshot_api_key.txt"

ORDERS = ("expand", "raid", "defend", "regroup")

SYSTEM_PROMPT_TEMPLATE = (
    "Ты управляешь ботом в игре захвата территории (упрощённый Ксоникс, "
    "общее поле на двоих, можно захватывать и уже занятую территорию "
    "соперника, а не только нейтральную). Ходы делает быстрая эвристика "
    "у тебя в подчинении — ты не решаешь каждый ход, а раз в ~20 секунд "
    "задаёшь ей общий приказ и настройки. "
    "Характер бота, заданный пользователем: \"{persona}\"\n\n"
    "Тебе показывают срез общего чата партии (пользователь + оба бота) и "
    "твою собственную переписку со своим ботом — учитывай их как контекст, "
    "если это уместно.\n\n"
    "Отвечай СТРОГО одним JSON-объектом, без пояснений и без markdown-"
    "разметки:\n"
    "{{\"order\": \"expand\"|\"raid\"|\"defend\"|\"regroup\", "
    "\"aggression\": <0..1>, \"caution\": <0..1>, "
    "\"private_message\": \"<до 20 слов по-русски — приказ своему боту, "
    "всегда непустой>\", "
    "\"general_message\": \"<до 20 слов по-русски — реплика в общий чат, "
    "или пустая строка, если сказать нечего>\"}}\n"
    "order: expand — обычная игра (баланс набегов и возврата); "
    "raid — дальние агрессивные набеги на территорию соперника; "
    "defend — короткие вылазки, быстрый возврат, минимум риска; "
    "regroup — почти не уходить с территории, переждать.\n"
    "aggression — насколько активно вторгаться на территорию соперника "
    "(0 = никогда, 1 = искать любую возможность).\n"
    "caution — насколько сильно избегать шариков и соперника "
    "(0 = игнорировать риск, 1 = максимально осторожно)."
)


def read_key(path: str) -> str | None:
    # Файлы в device_credentials/ обычно начинаются с пояснения на русском
    # (см. moonshot_api_key.txt) — сам ключ ищем по префиксу "sk-", а не
    # по первой непустой строке.
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("sk-"):
                    return line
    except FileNotFoundError:
        return None
    return None


def stable_cache_key(system_prompt: str, model: str) -> str:
    payload = json.dumps({"system": system_prompt, "model": model}, sort_keys=True, ensure_ascii=False)
    return "xonix-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def call_claude_haiku(api_key: str, system_prompt: str, user_prompt: str) -> tuple[str, dict]:
    # system_prompt = SYSTEM_PROMPT_TEMPLATE + persona — не меняется между
    # вызовами, пока пользователь не поправит характер бота (игровое
    # состояние/чат идут отдельно, в user_prompt) — по идее кандидат для
    # prompt-кэша. cache_control нужно ставить явно (в отличие от Kimi, тут
    # сервер сам ничего не кэширует без пометки) — блок system переведён в
    # форму списка ради этого маркера.
    # НО: минимальная кэшируемая длина у Haiku 4.5 — 4096 токенов (см. skill
    # claude-api/shared/prompt-caching.md, порог у Haiku аномально высокий
    # по сравнению с другими моделями), а наш system-промпт в разы короче —
    # реально не кэшируется НИКОГДА при текущем размере, маркер просто
    # молча бездействует (без ошибки и без штрафа). Оставлен на будущее,
    # если промпт когда-нибудь вырастет — специально раздувать его ради
    # кэша сейчас не имеет смысла, экономия не окупит закладку токенов.
    body = json.dumps({
        "model": "claude-haiku-4-5",
        "max_tokens": 500,
        "system": [{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": user_prompt}],
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read())
    text = "".join(b["text"] for b in data.get("content", []) if b.get("type") == "text")
    u = data.get("usage", {})
    usage = {
        "input": u.get("input_tokens", 0),
        "output": u.get("output_tokens", 0),
        "cached": u.get("cache_read_input_tokens", 0),
    }
    return text, usage


def call_kimi(api_key: str, system_prompt: str, user_prompt: str) -> tuple[str, dict]:
    model = "kimi-k2.7-code"
    body = json.dumps({
        "model": model,
        "temperature": 1,  # эта модель принимает только 1, см. алёна.md
        # Ризонинг-модель: часть токенов уходит в скрытые reasoning_content
        # ДО финального ответа (проверено вживую — 200 не хватало, ответ
        # обрывался пустым при finish_reason="length"; 1200 хватает с запасом).
        "max_tokens": 1200,
        "prompt_cache_key": stable_cache_key(system_prompt, model),
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.moonshot.ai/v1/chat/completions",
        data=body,
        headers={"Authorization": f"Bearer {api_key}", "content-type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read())
    text = data["choices"][0]["message"]["content"]
    u = data.get("usage", {})
    usage = {
        "input": u.get("prompt_tokens", 0),
        "output": u.get("completion_tokens", 0),
        "cached": u.get("cached_tokens", 0),
    }
    return text, usage


def parse_llm_json(text: str) -> dict | None:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    try:
        aggression = max(0.0, min(1.0, float(obj.get("aggression", 0.5))))
        caution = max(0.0, min(1.0, float(obj.get("caution", 0.5))))
    except (TypeError, ValueError):
        return None
    order = str(obj.get("order", "expand")).strip()
    if order not in ORDERS:
        order = "expand"
    private_message = str(obj.get("private_message", "")).strip()[:300]
    if not private_message:
        private_message = "Продолжай как раньше."
    general_message = str(obj.get("general_message", "")).strip()[:300]
    return {
        "order": order,
        "aggression": aggression,
        "caution": caution,
        "private_message": private_message,
        "general_message": general_message,
    }


class Shared:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.provider = "off"
        self.persona = "Играй сбалансированно — не слишком рискованно, не слишком пассивно."
        self.last_state_ts: float | None = None
        self.state: dict = {}
        self.general_log: list[dict] = []
        self.private_log: list[dict] = []


def _format_log(entries: list[dict], limit: int) -> str:
    if not entries:
        return "(пока пусто)"
    who = {"user": "Пользователь", "claude": "Алёна (эксперт)", "p1": "Голубой", "p2": "Жёлтый", "system": "Система"}
    lines = []
    for m in entries[-limit:]:
        if m.get("kind") == "status":
            continue
        lines.append(f"{who.get(m.get('sender'), m.get('sender'))}: {m.get('text', '')}")
    return "\n".join(lines) if lines else "(пока пусто)"


def build_user_prompt(player: str, shared: Shared) -> str:
    opp = "p2" if player == "p1" else "p1"
    with shared.lock:
        s = shared.state
        general_log = list(shared.general_log)
        private_log = list(shared.private_log)
    my_pct = s.get(f"{player}_percent", "?")
    opp_pct = s.get(f"{opp}_percent", "?")
    difficulty = s.get("difficulty", "?")
    return (
        f"Моя территория: {my_pct}%, территория соперника: {opp_pct}%, "
        f"сложность: {difficulty}.\n\n"
        f"Общий чат партии (последние сообщения):\n{_format_log(general_log, 8)}\n\n"
        f"Твоя переписка со своим ботом (последние сообщения):\n{_format_log(private_log, 5)}"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("player", choices=["p1", "p2"])
    args = parser.parse_args()
    player = args.player

    shared = Shared()

    def on_message(client, userdata, msg) -> None:
        if msg.topic == "xonix/game/state":
            try:
                with shared.lock:
                    shared.state = json.loads(msg.payload)
                    shared.last_state_ts = time.monotonic()
            except json.JSONDecodeError:
                pass
        elif msg.topic == f"xonix/game/{player}/llm_provider":
            payload = msg.payload.decode("utf-8", errors="ignore").strip()
            if payload in ("off", "haiku", "kimi"):
                with shared.lock:
                    shared.provider = payload
        elif msg.topic == f"xonix/game/{player}/persona":
            with shared.lock:
                shared.persona = msg.payload.decode("utf-8", errors="ignore").strip() or shared.persona
        elif msg.topic == "xonix/chat/log/general":
            try:
                with shared.lock:
                    shared.general_log = json.loads(msg.payload).get("messages", [])
            except (json.JSONDecodeError, AttributeError):
                pass
        elif msg.topic == f"xonix/chat/log/private_{player}":
            try:
                with shared.lock:
                    shared.private_log = json.loads(msg.payload).get("messages", [])
            except (json.JSONDecodeError, AttributeError):
                pass

    client = mqtt.Client()
    client.username_pw_set(MQTT_USER, MQTT_PASS)
    client.on_message = on_message
    client.connect(MQTT_HOST, MQTT_PORT, keepalive=30)
    client.subscribe("xonix/game/state", qos=0)
    client.subscribe(f"xonix/game/{player}/llm_provider", qos=1)
    client.subscribe(f"xonix/game/{player}/persona", qos=1)
    client.subscribe("xonix/chat/log/general", qos=0)
    # ТОЛЬКО свой приватный канал — чужой этот процесс физически не видит,
    # именно так и обеспечена приватность приказов (см. докстринг файла).
    client.subscribe(f"xonix/chat/log/private_{player}", qos=0)
    client.loop_start()

    anthropic_key = read_key(ANTHROPIC_KEY_PATH)
    moonshot_key = read_key(MOONSHOT_KEY_PATH)

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
            persona = shared.persona

        if provider != "off" and last_state_ts is not None:
            system_prompt = SYSTEM_PROMPT_TEMPLATE.format(persona=persona)
            user_prompt = build_user_prompt(player, shared)
            try:
                if provider == "haiku":
                    if not anthropic_key:
                        client.publish(f"xonix/game/{player}/llm_status", "нет ключа anthropic_api_key.txt", qos=0, retain=True)
                        raise RuntimeError("no anthropic key")
                    text, usage = call_claude_haiku(anthropic_key, system_prompt, user_prompt)
                elif provider == "kimi":
                    if not moonshot_key:
                        client.publish(f"xonix/game/{player}/llm_status", "нет ключа moonshot_api_key.txt", qos=0, retain=True)
                        raise RuntimeError("no moonshot key")
                    text, usage = call_kimi(moonshot_key, system_prompt, user_prompt)
                else:
                    text, usage = None, None

                if text is not None:
                    params = parse_llm_json(text)
                    if params is not None:
                        client.publish(
                            f"xonix/game/{player}/llm_params",
                            json.dumps({"order": params["order"], "aggression": params["aggression"], "caution": params["caution"]}),
                            qos=0, retain=True,
                        )
                        client.publish(f"xonix/game/{player}/llm_status", "ok", qos=0, retain=True)
                        # Цена этого конкретного запроса в центах — "как в Roo Code"
                        # (USD/1M из PRICING_USD_PER_1M) — плюс накопленный итог по
                        # этому игроку, переживающий рестарты (add_cost). Кладём прямо
                        # в usage, чтобы разошлось со всеми тремя публикациями ниже
                        # (llm_usage-топик, приватное и общее сообщения чата) одним куском.
                        cost_cents = compute_cost_cents(provider, usage)
                        total_cents = add_cost(player, cost_cents)
                        usage["cost_cents"] = round(cost_cents, 4)
                        usage["total_cost_cents"] = round(total_cents, 4)
                        # Цена этого конкретного запроса — отдельным ретейн-топиком
                        # (видно и без прокрутки чата) И прикреплена к самому приватному
                        # сообщению (usage), чтобы отображалась прямо рядом с репликой,
                        # как просил пользователь, а не только в статике.
                        client.publish(
                            f"xonix/game/{player}/llm_usage",
                            json.dumps({"provider": provider, **usage}, ensure_ascii=False),
                            qos=0, retain=True,
                        )
                        client.publish(
                            f"xonix/chat/post/{player}",
                            json.dumps(
                                {"channel": "private", "text": params["private_message"], "usage": {"provider": provider, **usage}},
                                ensure_ascii=False,
                            ),
                            qos=1,
                        )
                        if params["general_message"]:
                            client.publish(
                                f"xonix/chat/post/{player}",
                                json.dumps(
                                    {"channel": "general", "text": params["general_message"], "usage": {"provider": provider, **usage}},
                                    ensure_ascii=False,
                                ),
                                qos=1,
                            )
                    else:
                        client.publish(f"xonix/game/{player}/llm_status", f"не разобрал ответ: {text[:150]}", qos=0, retain=True)
            except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, RuntimeError) as e:
                client.publish(f"xonix/game/{player}/llm_status", f"ошибка: {e}", qos=0, retain=True)
            except Exception as e:  # не даём случайной ошибке парсинга убить процесс
                client.publish(f"xonix/game/{player}/llm_status", f"неожиданная ошибка: {e}", qos=0, retain=True)

        dt = DECIDE_INTERVAL - (time.monotonic() - t0)
        if dt > 0:
            time.sleep(dt)


if __name__ == "__main__":
    main()
