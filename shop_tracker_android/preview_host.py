"""Предпросмотр автономного Android-приложения на компьютере.

Запускает те же модули, что лежат в APK (`android_main`): локальный сервер
и сбор данных. Данные пишутся в отдельную папку `.preview`, ключи берутся
из рабочей копии настольной версии; Telegram в предпросмотре отключён,
чтобы не мешать боту настольной версии.

Использование: python preview_host.py [порт]
"""

import json
import shutil
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "app" / "src" / "main" / "python"))

BASE = HERE / ".preview"
DATA = BASE / "data"


def seed_config() -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    target = DATA / "config.json"
    source = HERE.parent / "ship_tracker" / "data" / "config.json"
    config = {}
    if target.exists():
        config = json.loads(target.read_text(encoding="utf-8"))
    elif source.exists():
        config = json.loads(source.read_text(encoding="utf-8"))
    config["telegram_token"] = ""
    config["telegram_chat_id"] = ""
    config["telegram_commands"] = False
    target.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
    seed_config()
    import android_main

    android_main.PORT = port
    android_main.start(str(BASE))
    print(f"Предпросмотр: http://127.0.0.1:{port}/", flush=True)
    while True:
        time.sleep(60)


if __name__ == "__main__":
    main()
