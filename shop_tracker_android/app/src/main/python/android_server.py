"""Локальный сервер автономной версии на телефоне.

Страница и расчёты берутся из мобильной версии (`mobile_server`), но данные
читаются из базы, которую на этом же телефоне наполняет `core.RadarCore`.
Добавлены экран настроек с ключами и перезапуск сбора после сохранения.
"""

import json
import threading
from http.server import ThreadingHTTPServer
from urllib.parse import parse_qs

import android_main
import mobile_server
import setup_page


class Server(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


GEAR = (
    '<a href="/setup" style="position:absolute;right:12px;top:'
    'calc(10px + env(safe-area-inset-top));color:#7d95ad;text-decoration:none;'
    'font-size:20px;line-height:1" aria-label="Настройки">&#9881;</a>'
)


def setup_html() -> str:
    config = android_main.load_config()
    return setup_page.render(
        api_key=str(config.get("api_key") or ""),
        gfw_key=str(config.get("gfw_key") or ""),
        telegram_token=str(config.get("telegram_token") or ""),
        telegram_chat=str(config.get("telegram_chat_id") or ""),
    )


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
                            "version": "3.0.1"})
            return
        super().do_GET()

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path != "/api/setup":
            self.send_bytes(b"not found", "text/plain; charset=utf-8", status=404)
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length).decode("utf-8", "replace") if length else ""
            form = parse_qs(raw, keep_blank_values=True)
        except Exception as error:
            self.send_json({"ok": False, "error": f"форма не прочитана: {error}"})
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
