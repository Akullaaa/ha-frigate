#!/bin/bash
# Обёртка ради старых записей go2rtc в config.yaml: аура камеры xm530 теперь в общем aura_overlay.sh (2026-09-15).
exec bash /config/aura_overlay.sh "$1" "$2" xm530
