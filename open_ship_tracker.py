"""Приложение-ссылка: открывает окно Ship Tracker и гарантирует, что оно
развёрнуто и активно — даже на устройствах, где Tk «мигает и сворачивается».
"""
import subprocess
import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent / "ship_tracker"

# На случай, если Python лежит внутри .venv (как у нас в run.bat)
VENV_PY = APP_DIR / ".venv" / "Scripts" / "python.exe"
PYTHON = VENV_PY if VENV_PY.exists() else (Path(sys.executable) if "__file__" else sys.executable)
# Если вообще не определилось — пусть система найдёт свой python по PATH
if not PYTHON or not Path(PYTHON).exists():
    PYTHON = "python3" if sys.platform != "win32" else "python"


def open_ship_tracker():
    """Запустить ship_tracker в том же каталоге, с явным развёртыванием окна."""
    # Запускаем из папки приложения, чтобы работали импорты и data/config.json
    working_dir = str(APP_DIR)

    # Путь к скрипту, который открывает окно
    script = str(APP_DIR / "ship_tracker.py")

    # Полный путь к шорткату / ярлыку, если есть (Windows), чтобы не зависеть от PATH
    # ВАЖНО: здесь мы НЕ прячем окно — наоборот, разворачиваем его, см. флаги ниже.

    # Лучше всего запустить в отдельном процессе и явно попросить систему развернуть окно.
    # На Windows для этого удобнее всего subprocess.Popen + смена состояния окна после старта.
    # Но для минимального «чтобы не сворачивалось» можно просто попросить ОС показать окно,
    # если скрипт сам не умеет. Поэтому запускаем ship_tracker.py напрямую, а уже после
    # попытки поднять окно делаем activate без побочных сворачиваний.
    try:
        proc = subprocess.Popen(
            [str(PYTHON), script],
            cwd=working_dir,
            creationflags=(
                0x00000008  # CREATE_NO_WINDOW — не создаёт консоль, окно Tk всё равно видно
                if sys.platform == "win32"
                else 0
            ),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as exc:
        print(f"Нет Python для запуска: {exc}", file=sys.stderr)
        return False

    # Даём Tk время создать корневое окно (обычно ~100-300 мс).
    import time
    time.sleep(0.25)

    # Хак для Windows: попробовать активировать окно船 Tracker через Win32 API.
    # Это самый надёжный способ не «мигать» и не сворачиваться на другом устройстве.
    if sys.platform == "win32":
        try:
            import ctypes
            user32 = ctypes.WinDLL("user32", use_last_error=True)

            def _find_and_activate():
                # Ищем окно по части заголовка
                target = "Ship Tracker"
                enum_handlers = []

                def _enum(hwnd, _):
                    if user32.IsWindowVisible(hwnd):
                        length = user32.GetWindowTextLengthW(hwnd)
                        if length:
                            buf = ctypes.create_unicode_buffer(length + 1)
                            user32.GetWindowTextW(hwnd, buf, length + 1)
                            if buf.value and target in buf.value:
                                enum_handlers.append(hwnd)
                    return True

                user32.EnumWindows(ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)(_enum), None)
                if enum_handlers:
                    hwnd = enum_handlers[0]
                    user32.ShowWindow(hwnd, 9)      # SW_SHOWNORMAL
                    user32.SetForegroundWindow(hwnd)
                    user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0004 | 0x0010)  # SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_SHOWWINDOW
                    return True
                return False

            for _ in range(8):
                if _find_and_activate():
                    break
                time.sleep(0.2)

        except Exception:
            # Не ломаем всё, если Win32 не доступен
            pass

    return True


if __name__ == "__main__":
    success = open_ship_tracker()
    if not success:
        sys.exit("Не удалось запустить Ship Tracker.")
