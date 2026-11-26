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
         portcalls analysis geo places net flags cargo watch)

for name in "${MODULES[@]}"; do
  cp "$ROOT/ship_tracker/$name.py" "$PY/$name.py"
done


# Шаблон настроек: берём рабочие порты и фильтры, ключи вычищаем полностью.
#
# APK передаётся другому человеку, поэтому в нём нет ни одного ключа: и
# AISStream, и Global Fishing Watch, и Telegram, и остальные поля пустые.
# Свои ключи владелец телефона вводит на экране подключений (кнопка с
# шестерёнкой), там же стоят ссылки на сервисы, где эти ключи выдают.
python - "$ROOT/ship_tracker/data/config.json" "$PY/android_defaults.py" <<'PY'
import json
import pprint
import sys
from pathlib import Path

source, target = Path(sys.argv[1]), Path(sys.argv[2])
config = json.loads(source.read_text(encoding="utf-8"))

# ключи, которые нельзя оставлять в сборке (gfw_key тоже: он привязан
# к аккаунту владельца, и передавать его вместе с приложением нельзя)
PUBLIC_KEYS: set[str] = set()
secrets = {
    "api_key", "openwaters_token", "vf_key", "vfa_key", "mt_key", "mst_key",
    "vesselapi_key", "telegram_token", "telegram_chat_id", "dat_key",
    "aishub_key", "sn_key", "gfw_key",
} - PUBLIC_KEYS
for key in secrets:
    if key in config:
        config[key] = "" if not isinstance(config[key], bool) else False
config["telegram_commands"] = False
config["use_sn"] = False
config["use_gfw"] = bool(str(config.get("gfw_key") or "").strip())
target.write_text(
    '"""Настройки по умолчанию для автономной версии.\n\n'
    'Файл создаётся скриптом sync_python.sh из ship_tracker/data/config.json.\n'
    'Ключей в нём нет: приложение передаётся другому человеку, и все ключи\n'
    'владелец телефона вводит сам на экране подключений.\n"""\n\n'
    "CONFIG = " + pprint.pformat(config, indent=1, width=100, sort_dicts=False) + "\n",
    encoding="utf-8")
print(f"настройки по умолчанию: {target.name}, полей {len(config)}")
PY

echo "готово: модули в $PY"
