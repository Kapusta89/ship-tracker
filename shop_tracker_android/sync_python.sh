#!/usr/bin/env bash
# Копирует слой данных настольной версии в Android-проект.
# Запускать после правок ship_tracker/*.py.
# Страница и сервер мобильной версии правятся прямо здесь:
# app/src/main/python/mobile_page.py и mobile_server.py.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
PY="$HERE/app/src/main/python"

MODULES=(core store ais_feed services_feed services_optima forecast metrics \
         portcalls analysis geo places net flags cargo)

for name in "${MODULES[@]}"; do
  cp "$ROOT/ship_tracker/$name.py" "$PY/$name.py"
done


# Шаблон настроек: берём рабочие порты и фильтры, ключи вычищаем полностью.
#
# В APK остаётся только ключ, который работает на любом устройстве и не
# привязан к аккаунту с лимитом на устройство: токен Global Fishing Watch
# (бесплатный, общий). Ключи с ограничениями — AISStream, VesselFinder,
# MarineTraffic, MyShipTracking, Datalastic, AISHub, ShipNext, Open Waters,
# Telegram — остаются пустыми: их вводит сам пользователь на экране настроек.
python - "$ROOT/ship_tracker/data/config.json" "$PY/android_defaults.py" <<'PY'
import json
import pprint
import sys
from pathlib import Path

source, target = Path(sys.argv[1]), Path(sys.argv[2])
config = json.loads(source.read_text(encoding="utf-8"))

# ключи без привязки к устройству — можно оставить в сборке
PUBLIC_KEYS = {"gfw_key"}
secrets = {
    "api_key", "openwaters_token", "vf_key", "vfa_key", "mt_key", "mst_key",
    "vesselapi_key", "telegram_token", "telegram_chat_id", "dat_key",
    "aishub_key", "sn_key",
} - PUBLIC_KEYS
for key in secrets:
    if key in config:
        config[key] = "" if not isinstance(config[key], bool) else False
config["telegram_commands"] = False
config["use_sn"] = False
target.write_text(
    '"""Настройки по умолчанию для автономной версии.\n\n'
    'Файл создаётся скриптом sync_python.sh из ship_tracker/data/config.json.\n'
    'Остаётся только ключ без привязки к устройству (gfw_key); ключи с\n'
    'ограничениями пустые — пользователь вводит их на экране настроек.\n"""\n\n'
    "CONFIG = " + pprint.pformat(config, indent=1, width=100, sort_dicts=False) + "\n",
    encoding="utf-8")
print(f"настройки по умолчанию: {target.name}, полей {len(config)}")
PY

echo "готово: модули в $PY"
