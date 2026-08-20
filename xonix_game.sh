#!/bin/bash
# Обёртка для xonix_game.py — движок сам порождает decode-процессы камер
# и MQTT-клиента изнутри, здесь финальная стадия (h264_vaapi encode ->
# RTSP push в go2rtc), тот же паттерн, что у xonix_layer_multicam.sh.
#
# Плюс — два процесса-агента ИИ (xonix_ai_agent.py), по одному на игрока.
# Это НАСТОЯЩИЕ отдельные процессы, подключающиеся по MQTT точно так же,
# как человек с дашборда (публикуют в p{N}/ai_move, читают board) — сам
# движок никакой игровой логики ИИ не содержит. Жизненный цикл агентов
# привязан к этому скрипту через trap: как только go2rtc останавливает
# продюсер (нет зрителей потока), пайп рвётся, скрипт завершается, trap
# убивает обоих агентов — не остаются висеть orphan-процессами.
#
# Плюс (третий уровень) — два процесса xonix_llm_strategist.py, по одному
# на игрока: раз в ~20с спрашивают настоящую LLM и тюнят параметры
# xonix_ai_agent.py (xonix/game/p{N}/llm_params). По умолчанию provider=off
# — простаивают без единого вызова API, пока не выбран провайдер с дашборда.
# Тот же trap их тоже убивает.
#
# Плюс xonix_chat_hub.py — единственный писатель истории трёхстороннего
# чата (пользователь + оба LLM-стратега), см. его докстринг. Без него
# xonix_llm_strategist.py просто не видит чат (подписан на топики chat/log/*,
# которые пишет только хаб) — молча работает без контекста чата, не падает.
#
# Плюс xonix_chat_alena.py — фоновая версия Алёны в общем чате: реагирует
# ТОЛЬКО на сообщения человека (не на ботов), provider=off по умолчанию.
# Отдельно от самой сессии Claude Code, которая пишет в чат вручную только
# пока пользователь реально разговаривает в терминале — этот процесс
# отвечает сам, без необходимости открывать Claude Code.
#
# Заведён как go2rtc exec-источник — см. go2rtc.streams.xonix_game в
# config.yaml.
set -e
FF=/usr/lib/ffmpeg/7.0/bin/ffmpeg
OUT="$1"

python3 /config/xonix_ai_agent.py p1 &
AGENT_P1=$!
python3 /config/xonix_ai_agent.py p2 &
AGENT_P2=$!
python3 /config/xonix_llm_strategist.py p1 &
LLM_P1=$!
python3 /config/xonix_llm_strategist.py p2 &
LLM_P2=$!
python3 /config/xonix_chat_hub.py &
CHAT_HUB=$!
python3 /config/xonix_chat_alena.py &
CHAT_ALENA=$!

cleanup() {
  kill "$AGENT_P1" "$AGENT_P2" "$LLM_P1" "$LLM_P2" "$CHAT_HUB" "$CHAT_ALENA" 2>/dev/null || true
}
trap cleanup EXIT

python3 /config/xonix_game.py \
  | "$FF" -nostdin -loglevel warning -vaapi_device /dev/dri/renderD128 \
      -f rawvideo -pix_fmt bgr24 -video_size 960x540 -framerate 12 -i - \
      -vf format=nv12,hwupload -c:v h264_vaapi -qp 20 -bf 0 -g 24 -keyint_min 24 -an \
      -f rtsp -rtsp_transport tcp "$OUT"
