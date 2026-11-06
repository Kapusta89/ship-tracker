"""Запуск приложения командой python -m ship_tracker.

Обычный запуск идёт через run.bat («python ship_tracker.py»), этот файл нужен
для запуска из консоли как модуль: python -m ship_tracker [ключи].
"""

import importlib.util
import os
import sys

# Папка с приложением должна быть в sys.path, чтобы работали импорты
# ais_feed, cargo, core, geo, store.
_APP_DIR = os.path.abspath(os.path.dirname(__file__))
if _APP_DIR not in sys.path:
    sys.path.insert(0, _APP_DIR)


def _load_app():
    """Загружает ship_tracker.py по пути — так запуск не зависит от того,
    вызван файл как модуль пакета или напрямую."""
    path = os.path.join(_APP_DIR, "ship_tracker.py")
    spec = importlib.util.spec_from_file_location("_ship_tracker_app", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


if __name__ == "__main__":
    app = _load_app()
    try:
        app.main()
    except KeyboardInterrupt:
        # Корректный выход по Ctrl+C без отладочного traceback.
        sys.exit(130)
