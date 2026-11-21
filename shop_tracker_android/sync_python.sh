#!/usr/bin/env bash
# Копирует слой данных настольной версии в Android-проект.
# Запускать после правок ship_tracker/*.py или shop_tracker_mob/*.py.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
PY="$HERE/app/src/main/python"

MODULES=(core store ais_feed services_feed services_optima forecast metrics \
         portcalls analysis geo places net flags cargo)

for name in "${MODULES[@]}"; do
  cp "$ROOT/ship_tracker/$name.py" "$PY/$name.py"
done

cp "$ROOT/shop_tracker_mob/shop_tracker_mob.py" "$PY/mobile_server.py"
cp "$ROOT/shop_tracker_mob/mobile_page.py" "$PY/mobile_page.py"

# Шаблон настроек: берём рабочие порты и фильтры, ключи вычищаем полностью.
python - "$ROOT/ship_tracker/data/config.json" "$PY/android_defaults.py" <<'PY'
import json
import sys
from pathlib import Path

source, target = Path(sys.argv[1]), Path(sys.argv[2])
config = json.loads(source.read_text(encoding="utf-8"))
secrets = {"api_key", "openwaters_token", "vf_key", "vfa_key", "mt_key", "mst_key",
           "vesselapi_key", "telegram_token", "telegram_chat_id", "gfw_key",
           "dat_key", "aishub_key", "sn_key"}
for key in secrets:
    if key in config:
        config[key] = "" if not isinstance(config[key], bool) else False
config["telegram_commands"] = False
config["use_sn"] = False
target.write_text(
    '"""Настройки по умолчанию для автономной версии (без ключей).\n\n'
    'Файл создаётся скриптом sync_python.sh из ship_tracker/data/config.json:\n'
    'ключи и токены здесь всегда пустые.\n"""\n\n'
    "CONFIG = " + json.dumps(config, ensure_ascii=False, indent=2) + "\n",
    encoding="utf-8")
print(f"настройки по умолчанию: {target.name}, полей {len(config)}")
PY

echo "готово: модули в $PY"
