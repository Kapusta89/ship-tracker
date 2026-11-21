"""Локальный сервер автономной версии на телефоне.

Страница и расчёты берутся из мобильной версии (`mobile_server`), но данные
читаются из базы, которую на этом же телефоне наполняет `core.RadarCore`.
Добавлены экран настроек с ключами, вкладки настольной версии (трафик района,
ожидаемые, алерты, сервисы, настройки-фильтры, журнал) и перезапуск сбора.
"""

import json
import threading
from http.server import ThreadingHTTPServer
from urllib.parse import parse_qs

import android_main
import mobile_server
import portcalls
import setup_page

VERSION = "3.1.0"


class Server(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


GEAR = (
    '<a href="/setup" style="position:absolute;right:12px;top:'
    'calc(10px + env(safe-area-inset-top));color:#7d95ad;text-decoration:none;'
    'font-size:20px;line-height:1" aria-label="Настройки">&#9881;</a>'
)

# Источники как на настольной вкладке «Подключение»: флаг в настройках,
# подпись, ключ (если нужен) и что источник даёт.
SOURCES = [
    ("use_gor", "Goradar", None, "открытая карта, позиции судов"),
    ("use_mlive", "MarineTraffic.live", None, "открытая карта, позиции судов"),
    ("use_sfp", "ShipFinder", None, "открытая карта, позиции судов"),
    ("use_srd", "Ship Radar 24", None, "карточки судов с деталями"),
    ("use_aci", "AIS-catcher", None, "ожидаемые прибытия по портам"),
    ("use_snapshot", "Open Waters — снимок района", None,
     "позиции всех судов вокруг портов, ключ не нужен"),
    ("use_mo", "Maritime Optima", None, "паспорт судна"),
    ("use_sa", "ShipAtlas", None, "рейс и статус судна"),
    ("use_mlink", "MarineLink", None, "тип судна из справочника"),
    ("use_gfw", "Global Fishing Watch", "gfw_key", "заходы в порты, паспорта"),
    ("use_sn", "ShipNext", "sn_key", "каталог судов по токену"),
]

# Числовые настройки настольной вкладки «Фильтры».
NUMBERS = [
    ("min_speed_knots", "Мин. ход судна, уз", 2.0),
    ("port_watch_km", "Радиус у порта, км", 50.0),
    ("alert_cooldown_hours", "Повтор алерта, ч", 6.0),
    ("history_days", "Глубина истории, дней", 7.0),
    ("position_interval_seconds", "Запись трека, с", 60.0),
    ("stale_minutes", "Сигнал устарел через, мин", 15.0),
]

_expected_loading = False
_expected_lock = threading.Lock()


def setup_html() -> str:
    config = android_main.load_config()
    return setup_page.render(
        api_key=str(config.get("api_key") or ""),
        gfw_key=str(config.get("gfw_key") or ""),
        telegram_token=str(config.get("telegram_token") or ""),
        telegram_chat=str(config.get("telegram_chat_id") or ""),
    )


def _key_present(config: dict, key: str | None) -> bool:
    if not key:
        return True
    return bool(str(config.get(key) or "").strip())


def services_payload() -> dict:
    """Состояние всех источников: включён, есть ли ключ, что говорит сбор."""
    config = android_main.load_config()
    live = android_main.core_status()
    rows = []
    for flag, title, key, detail in SOURCES:
        enabled = bool(config.get(flag, True))
        has_key = _key_present(config, key)
        status = live.get(flag.replace("use_", "")) or ""
        if not enabled:
            state, state_text = "off", "выключено"
        elif not has_key:
            state, state_text = "warn", "нужен ключ"
        elif status:
            bad = any(word in status.lower() for word in
                      ("ошиб", "нет данных", "не удал", "401", "403", "не отвеч"))
            state, state_text = ("warn" if bad else "on"), status
        else:
            state, state_text = "on", "включено"
        rows.append({"title": title, "detail": detail, "state": state,
                     "state_text": state_text, "flag": flag,
                     "checked": enabled, "needs_key": bool(key),
                     "has_key": has_key})
    return {"rows": rows, "version": VERSION,
            "running": android_main.core_running(),
            "note": ("Источники включаются и выключаются здесь же; после сохранения "
                     "сбор данных перезапускается сам.")}


def expected_payload() -> dict:
    cache = portcalls.load_cache()
    rows = portcalls.expected_rows(cache)
    for row in rows:
        row["stale"] = portcalls.expected_is_stale(row)
    rows.sort(key=lambda row: (row.get("stale"), row.get("port") or ""))
    return {
        "rows": rows[:400],
        "updated": cache.get("updated") or "",
        "stale": bool(cache.get("stale")),
        "stale_reason": cache.get("stale_reason") or "",
        "loading": _expected_loading,
        "note": ("Кто указал наш порт пунктом назначения — таблица портовых страниц "
                 "AIS-catcher (открытая сеть приёмников, без ключа). Страницы есть "
                 "только у части портов, ETA передаёт сам капитан и обновляет его "
                 "не всегда; серые строки — старый сигнал или прошедший ETA."),
    }


def refresh_expected() -> bool:
    global _expected_loading
    with _expected_lock:
        if _expected_loading:
            return False
        _expected_loading = True

    def worker():
        global _expected_loading
        try:
            config = android_main.load_config()
            android_main.log_line("AIS-catcher: запрашиваю ожидаемые прибытия…")
            data = portcalls.fetch_expected(config.get("ports"),
                                            log=android_main.log_line)
            portcalls.save_cache(data)
            total = sum(len(entry.get("vessels") or [])
                        for entry in (data.get("ports") or {}).values())
            android_main.log_line(f"AIS-catcher: ожидаемых судов {total}.")
        except Exception as error:
            android_main.log_line(f"AIS-catcher: ошибка — {error}")
        finally:
            with _expected_lock:
                _expected_loading = False

    threading.Thread(target=worker, name="expected", daemon=True).start()
    return True


def settings_payload() -> dict:
    config = android_main.load_config()
    chosen = {str(name).lower() for name in (config.get("filter_destinations") or [])}
    ports = []
    for port in config.get("ports") or []:
        if not isinstance(port, dict):
            continue
        name = str(port.get("name") or "")
        if not name:
            continue
        ports.append({"name": name, "label": port.get("label") or name,
                      "on": (not chosen) or name.lower() in chosen})
    numbers = [{"key": key, "label": label,
                "value": config.get(key, default)} for key, label, default in NUMBERS]
    sources = [{"flag": flag, "title": title,
                "on": bool(config.get(flag, True))} for flag, title, _, _ in SOURCES]
    return {"ports": ports, "numbers": numbers, "sources": sources,
            "database": str(android_main.db_path()),
            "version": VERSION}


def apply_settings(form: dict) -> dict:
    """Сохраняет фильтры и включённые источники, затем перезапускает сбор."""
    config = android_main.load_config()

    names = [name.strip() for name in form.get("port", []) if name.strip()]
    all_ports = [str(port.get("name")) for port in config.get("ports") or []
                 if isinstance(port, dict) and port.get("name")]
    # все отмечены — значит ограничения нет, пишем пустой список
    config["filter_destinations"] = [] if set(names) >= set(all_ports) else names

    for key, _, default in NUMBERS:
        raw = (form.get(key) or [""])[0].replace(",", ".").strip()
        if not raw:
            continue
        try:
            value = float(raw)
        except ValueError:
            continue
        config[key] = int(value) if float(value).is_integer() else value

    chosen = set(form.get("src", []))
    for flag, _, _, _ in SOURCES:
        config[flag] = flag in chosen

    android_main.save_config(config)
    threading.Thread(target=android_main.restart_core, name="core-restart",
                     daemon=True).start()
    return {"ok": True}


def log_tail(lines: int = 300) -> dict:
    path = android_main.log_path()
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    rows = text.splitlines()[-max(20, min(lines, 2000)):]
    return {"rows": rows, "path": str(path), "size": len(text)}


class Handler(mobile_server.Handler):
    data = None

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if self.data is None or self.data.database is None:
            # база появляется после старта сбора: подхватываем её без перезапуска
            try:
                fresh = mobile_server.Data()
                if fresh.database is not None:
                    Handler.data = fresh
                    self.data = fresh
            except Exception as error:
                android_main.log_line(f"База ещё не готова: {error}")
        if path == "/setup":
            self.send_bytes(setup_html().encode("utf-8"), "text/html; charset=utf-8")
            return
        if path == "/" and not android_main.has_key():
            self.send_bytes(setup_html().encode("utf-8"), "text/html; charset=utf-8")
            return
        if path == "/":
            page = mobile_server.PAGE
            if "<body>" in page:
                page = page.replace("<body>", "<body>" + GEAR, 1)
            else:
                page = page.replace("</header>", GEAR + "</header>", 1)
            self.send_bytes(page.encode("utf-8"), "text/html; charset=utf-8")
            return
        if path == "/api/setup-status":
            self.send_json({"key": android_main.has_key(),
                            "database": str(android_main.db_path()),
                            "version": VERSION})
            return
        if path == "/api/services":
            self.send_json(services_payload())
            return
        if path == "/api/expected":
            self.send_json(expected_payload())
            return
        if path == "/api/settings":
            self.send_json(settings_payload())
            return
        if path == "/api/log":
            self.send_json(log_tail())
            return
        super().do_GET()

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        allowed = {"/api/setup", "/api/expected/refresh", "/api/settings",
                   "/api/restart"}
        if path not in allowed:
            self.send_bytes(b"not found", "text/plain; charset=utf-8", status=404)
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length).decode("utf-8", "replace") if length else ""
            form = parse_qs(raw, keep_blank_values=True)
        except Exception as error:
            self.send_json({"ok": False, "error": f"форма не прочитана: {error}"})
            return

        if path == "/api/expected/refresh":
            started = refresh_expected()
            self.send_json({"ok": started,
                            "error": "" if started else "обновление уже идёт"})
            return
        if path == "/api/restart":
            threading.Thread(target=android_main.restart_core,
                             name="core-restart", daemon=True).start()
            self.send_json({"ok": True})
            return
        if path == "/api/settings":
            try:
                self.send_json(apply_settings(form))
            except OSError as error:
                self.send_json({"ok": False, "error": f"настройки не сохранены: {error}"})
            return

        def field(name: str) -> str:
            return (form.get(name) or [""])[0].strip()

        api_key = field("api_key")
        config = android_main.load_config()
        config["api_key"] = api_key
        config["gfw_key"] = field("gfw_key")
        config["telegram_token"] = field("telegram_token")
        config["telegram_chat_id"] = field("telegram_chat_id")
        config["telegram_commands"] = bool(config["telegram_token"])
        config["use_gfw"] = bool(config["gfw_key"])
        try:
            android_main.save_config(config)
        except OSError as error:
            self.send_json({"ok": False, "error": f"настройки не сохранены: {error}"})
            return

        # база и настройки могли появиться только сейчас — пересобираем Data
        try:
            Handler.data = mobile_server.Data()
        except Exception as error:
            android_main.log_line(f"Данные не перечитаны: {error}")

        threading.Thread(target=android_main.restart_core, name="core-restart",
                         daemon=True).start()
        self.send_json({"ok": True, "key": bool(api_key)})


def serve(port: int = 8080) -> Server:
    """Поднимает сервер на localhost и отдаёт управление."""
    mobile_server.app_dir = lambda: android_main.BASE
    Handler.data = mobile_server.Data()
    server = None
    last_error = None
    for candidate in range(port, port + 10):
        try:
            server = Server(("127.0.0.1", candidate), Handler)
            android_main.PORT = candidate
            break
        except OSError as error:
            last_error = error
    if server is None:
        raise RuntimeError(f"порт занят: {last_error}")
    threading.Thread(target=server.serve_forever, name="http-server",
                     daemon=True).start()
    return server
