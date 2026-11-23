"""Автономная версия Ship Tracker для Android.

На телефоне работают и сбор данных, и интерфейс:

  * `core.RadarCore` из настольной версии сам подключается к AISStream,
    читает открытые сайты (Goradar, MarineTraffic.live, ShipFinder,
    AIS-catcher, Ship Radar 24, Maritime Optima, ShipAtlas, MarineLink,
    Global Fishing Watch) и пишет всё в свою базу на телефоне;
  * локальный сервер отдаёт страницу с вкладками «Радар», «В море»,
    «Прогноз 4 дня», «Прогноз всех судов», «Стоянки», «Подключения».

Компьютер и Wi-Fi не нужны: нужен только интернет. Ключи вводит сам
пользователь на экране настроек и они остаются в памяти приложения.
"""

import json
import net
import threading
import time
from datetime import datetime
from pathlib import Path

PORT = 8080

BASE: Path | None = None
_core = None
_store = None
_server = None
_core_lock = threading.Lock()

# Каналы, появившиеся позже базового примера настроек: включаем по умолчанию
# те, что работают без ключа.
DEFAULT_EXTRA = {
    "use_mo": True,
    "mo_minutes": 30,
    "use_sa": True,
    "sa_minutes": 40,
    "use_mlink": True,
    "use_sn": False,
    "sn_key": "",
    "sn_minutes": 60,
    "telegram_token": "",
    "telegram_chat_id": "",
    "telegram_commands": False,
}


def data_dir() -> Path:
    return BASE / "data"


def config_path() -> Path:
    return data_dir() / "config.json"


def db_path() -> Path:
    return data_dir() / "ship_tracker.db"


def log_path() -> Path:
    return data_dir() / "app.log"


def log_line(text: str) -> None:
    try:
        data_dir().mkdir(parents=True, exist_ok=True)
        with log_path().open("a", encoding="utf-8") as handle:
            handle.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {text}\n")
    except OSError:
        pass


def load_config() -> dict:
    try:
        data = json.loads(config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_config(config: dict) -> None:
    data_dir().mkdir(parents=True, exist_ok=True)
    tmp = config_path().with_suffix(".json.tmp")
    tmp.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(config_path())


def ensure_config() -> dict:
    """Настройки лежат в памяти приложения, ключи — только введённые вручную."""
    data_dir().mkdir(parents=True, exist_ok=True)
    config = load_config()
    changed = False
    try:
        import android_defaults

        for key, value in (getattr(android_defaults, "CONFIG", {}) or {}).items():
            if key not in config:
                config[key] = value
                changed = True
    except Exception as error:  # шаблон настроек не обязателен
        log_line(f"Настройки по умолчанию не прочитаны: {net.brief_error(error)}")
    for key, value in DEFAULT_EXTRA.items():
        if key not in config:
            config[key] = value
            changed = True
    if changed or not config_path().exists():
        save_config(config)
    return config


def has_key() -> bool:
    return bool(str(load_config().get("api_key") or "").strip())


def _patch_paths():
    """Переносит папки данных настольных модулей в память приложения.

    Код настольной версии лежит внутри APK (только для чтения), поэтому
    база, кэши и журналы должны писаться в filesDir.
    """
    import core
    import portcalls
    import services_feed
    import services_optima

    core.APP_DIR = BASE
    core.DATA_DIR = data_dir()
    core.CONFIG_PATH = config_path()
    core.DB_PATH = db_path()

    portcalls.BASE_DIR = BASE
    portcalls.DATA_DIR = data_dir()
    portcalls.EXPECTED_PATH = data_dir() / "expected.json"
    portcalls.GFW_PATH = data_dir() / "gfw.json"
    portcalls.SOURCES_PATH = data_dir() / "sources.json"

    services_feed.BASE_DIR = BASE
    services_feed.SHIPRADAR_STATE = data_dir() / "shipradar_cards.json"
    services_feed.SHIPRADAR_PORTCALLS_PATH = data_dir() / "portcalls.json"

    services_optima.BASE_DIR = BASE
    services_optima.MARITIMEOPTIMA_STATE = data_dir() / "maritimeoptima.json"
    services_optima.SHIPATLAS_STATE = data_dir() / "shipatlas.json"
    return core


def _on_alert(alert) -> None:
    try:
        log_line(f"АЛЕРТ: {alert.get('name')} {alert.get('origin')} → "
                 f"{alert.get('destination')}")
    except Exception:
        pass


def start_core() -> None:
    """Поднимает сбор данных. Вызывается один раз при запуске приложения."""
    global _core, _store
    config = ensure_config()
    if not str(config.get("api_key") or "").strip():
        log_line("Ключ AISStream не задан — сбор данных ждёт настройки.")
        return
    with _core_lock:
        if _core is not None:
            return
        core = _patch_paths()
        from store import Store

        _store = Store(db_path())
        _core = core.RadarCore(config, _store, log=lambda text: log_line(str(text)),
                               on_alert=_on_alert)
        _core.start()
        log_line("Сбор данных на телефоне запущен.")


def restart_core() -> None:
    """Перезапуск после сохранения ключей или смены настроек."""
    global _core, _store
    with _core_lock:
        if _core is not None:
            try:
                _core.stop()
            except Exception as error:
                log_line(f"Остановка сбора: {net.brief_error(error)}")
            _core = None
        if _store is not None:
            try:
                _store.close()
            except Exception:
                pass
            _store = None
    start_core()


def core_running() -> bool:
    return _core is not None


def refresh_vessel(mmsi) -> dict:
    """Обновить одно судно сейчас — как кнопка «Обновить карточку» на ПК."""
    mmsi = str(mmsi or "").strip()
    if not mmsi:
        return {"text": "судно не выбрано"}
    if _core is None:
        return {"text": "сбор данных не запущен"}
    try:
        return dict(_core.refresh_vessel_now(mmsi))
    except Exception as error:
        return {"text": f"ошибка: {net.brief_error(error)}"}


def core_status() -> dict:
    """Что сбор говорит о каждом источнике: ключ — имя сервиса."""
    if _core is None:
        return {}
    status = getattr(_core, "service_status", None) or {}
    try:
        return {str(key): str(value) for key, value in status.items()}
    except Exception:
        return {}


def _start_server() -> None:
    global _server
    import android_server

    _server = android_server.serve(PORT)
    log_line(f"Локальный сервер: http://127.0.0.1:{PORT}")


def start(files_dir: str) -> None:
    """Точка входа из Java (MainActivity) — Chaquopy зовёт её в своём потоке."""
    global BASE
    if BASE is not None:
        return
    BASE = Path(files_dir)
    data_dir().mkdir(parents=True, exist_ok=True)
    log_line("Запуск автономной версии Ship Tracker")
    _start_server()
    threading.Thread(target=start_core, name="core-start", daemon=True).start()


def shutdown() -> None:
    global _core
    if _core is not None:
        try:
            _core.stop()
        except Exception:
            pass
        _core = None
