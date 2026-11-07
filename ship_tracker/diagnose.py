"""Самопроверка Ship Tracker: что мешает запуску на этом компьютере.

Запуск: check.bat (он же готовит окружение) или
    python -X utf8 diagnose.py

Отчёт пишется в data/diagnose.txt обычным текстом — его можно переслать
разработчику, если приложение не открывается.
"""

from __future__ import annotations

import importlib
import json
import os
import platform
import socket
import sys
import traceback
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent
DATA = BASE / "data"
OUT = DATA / "diagnose.txt"


def _version(package: str) -> str:
    try:
        from importlib.metadata import version
        return version(package)
    except Exception:
        return "не установлен"


def _net(host: str, port: int = 443, timeout: float = 5.0) -> str:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return "доступен"
    except OSError as error:
        return f"НЕТ СВЯЗИ ({error.__class__.__name__}: {error})"


def main() -> int:
    lines: list[str] = []
    problems: list[str] = []

    def add(text: str = "") -> None:
        lines.append(text)

    def problem(text: str) -> None:
        problems.append(text)
        add(f"  ⚠ {text}")

    add("Ship Tracker — отчёт о запуске")
    add(f"Дата: {datetime.now():%d.%m.%Y %H:%M:%S}")
    add(f"Компьютер: {platform.node()} · пользователь: {os.environ.get('USERNAME', '—')}")
    add(f"Система: {platform.platform()}")
    add()

    add("--- Путь программы ---")
    add(f"Папка: {BASE}")
    add(f"Длина пути: {len(str(BASE))} символов"
        + ("  ⚠ близко к пределу Windows (260)" if len(str(BASE)) > 200 else ""))
    if "onedrive" in str(BASE).lower():
        add("Папка внутри OneDrive: файлы могут быть «только онлайн» — "
            "проверьте, что они скачаны на диск.")
    add()

    add("--- Python ---")
    add(f"Версия: {platform.python_version()} ({platform.architecture()[0]})")
    add(f"Файл: {sys.executable}")
    add(f"Окружение: {'отдельное (venv)' if sys.prefix != sys.base_prefix else 'системный Python'}")
    add(f"Префикс: {sys.prefix}")
    if sys.version_info < (3, 9):
        problem("Python старее 3.9 — нужен 3.9 или новее (лучше 3.12)")
    add()

    add("--- Библиотеки ---")
    for package, module in (("requests", "requests"), ("websockets", "websockets"),
                            ("pillow", "PIL"), ("aiogram", "aiogram")):
        try:
            importlib.import_module(module)
            add(f"  {package}: {_version(package)}")
        except Exception as error:
            problem(f"{package} не импортируется ({error.__class__.__name__}: {error})")

    try:
        import tkinter
        add(f"  tkinter: {tkinter.TkVersion}")
        if sys.platform.startswith("win"):
            try:
                import ctypes
                ctypes.windll.shcore.SetProcessDpiAwareness(1)
            except Exception:
                pass
        window = tkinter.Tk()
        window.withdraw()
        window.update_idletasks()
        window.destroy()
        add("  окно Tk создаётся: да")
    except Exception as error:
        problem(f"tkinter не работает ({error.__class__.__name__}: {error}) — "
                "переустановите Python с python.org и отметьте «tcl/tk and IDLE»")
    add()

    add("--- Настройки и база ---")
    config_path = DATA / "config.json"
    if config_path.exists():
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
            add(f"  config.json: есть, ключей {len(config)}")
            for key, title in (("api_key", "AISStream"), ("telegram_token", "Telegram-бот"),
                               ("telegram_chat_id", "чат бота"), ("gfw_key", "Global Fishing Watch"),
                               ("openwaters_token", "Open Waters")):
                value = str(config.get(key) or "").strip()
                add(f"    {title}: {'задан' if value else 'не задан'}")
            ports = config.get("ports") or []
            add(f"    портов наблюдения: {len(ports)}")
        except Exception as error:
            problem(f"config.json не читается ({error})")
    else:
        add("  config.json: нет — приложение создаст настройки по умолчанию")

    db_path = DATA / "ship_tracker.db"
    if db_path.exists():
        add(f"  база: {db_path.stat().st_size / 1024:.0f} КБ")
    else:
        add("  базы ещё нет — она появится при запуске")
    try:
        DATA.mkdir(parents=True, exist_ok=True)
        probe = DATA / ".write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        add("  запись в папку data: да")
    except OSError as error:
        problem(f"нет записи в папку data ({error}) — перенесите программу, "
                "например, в C:\\ShipTracker")
    add()

    add("--- Сеть (порты 443) ---")
    for host in ("api.telegram.org", "www.aiscatcher.org",
                 "gateway.api.globalfishingwatch.org", "stream.aisstream.io"):
        state = _net(host)
        add(f"  {host}: {state}")
        if state != "доступен":
            problem(f"нет доступа к {host} — проверьте интернет/VPN/антивирус")
    add()

    add("--- Итог ---")
    if problems:
        add(f"Найдено проблем: {len(problems)}")
        for index, text in enumerate(problems, 1):
            add(f"  {index}. {text}")
    else:
        add("Проблем не найдено: окружение готово, приложение должно открыться.")
    add()
    add("Если окно всё равно не появляется — пришлите этот файл вместе с")
    add("data/errors.log (там traceback последнего сбоя).")

    DATA.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
    print("\n".join(lines))
    print(f"\nОтчёт сохранён: {OUT}")
    return 1 if problems else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        DATA.mkdir(parents=True, exist_ok=True)
        OUT.write_text(traceback.format_exc(), encoding="utf-8-sig")
        print(traceback.format_exc())
        print(f"Отчёт сохранён: {OUT}")
        sys.exit(1)
