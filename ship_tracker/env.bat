@echo off
rem Файл должен оставаться с переводами строк CRLF (см. .gitattributes).
chcp 65001 >nul
rem ===========================================================
rem  Подготовка Python для Ship Tracker (приложение и бот).
rem  Вызов: call env.bat   ->  при успехе PY = путь к python.exe
rem  Код возврата: 0 — готово, 1 — ошибка (текст уже напечатан).
rem  Папку .venv нельзя переносить между компьютерами: здесь она
rem  проверяется и при необходимости собирается заново.
rem ===========================================================
set "ROOT=%~dp0"
set "VENV_DIR=%ROOT%.venv"
set "VENV_PY=%VENV_DIR%\Scripts\python.exe"
set "REQ=%ROOT%requirements.txt"
set "PY="

if not exist "%VENV_PY%" goto create
"%VENV_PY%" -c "import sys" >nul 2>nul
if errorlevel 1 goto stale
"%VENV_PY%" -c "import tkinter, requests, websockets, PIL, aiogram" >nul 2>nul
if not errorlevel 1 goto ok

echo Обновляю зависимости в .venv...
"%VENV_PY%" -m pip install --quiet --disable-pip-version-check -r "%REQ%"
"%VENV_PY%" -c "import tkinter, requests, websockets, PIL, aiogram" >nul 2>nul
if not errorlevel 1 goto ok
echo Окружение .venv неполное — собираю заново.
goto stale

:stale
echo Окружение .venv не подходит этому компьютеру — собираю заново.
if exist "%ROOT%.venv_old" rd /s /q "%ROOT%.venv_old" >nul 2>nul
move "%VENV_DIR%" "%ROOT%.venv_old" >nul 2>nul
if exist "%VENV_DIR%" rd /s /q "%VENV_DIR%" >nul 2>nul

:create
set "BASE="
py -3 -c "import sys" >nul 2>nul
if not errorlevel 1 set "BASE=py -3"
if not defined BASE (
    python -c "import sys" >nul 2>nul
    if not errorlevel 1 set "BASE=python"
)
if not defined BASE (
    python3 -c "import sys" >nul 2>nul
    if not errorlevel 1 set "BASE=python3"
)
if not defined BASE goto nopython
%BASE% -c "import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 9) else 1)" >nul 2>nul
if errorlevel 1 goto oldpython

echo Первый запуск на этом компьютере: собираю окружение .venv...
%BASE% -m venv "%VENV_DIR%"
if errorlevel 1 goto venvfail
"%VENV_PY%" -m pip install --upgrade pip --quiet --disable-pip-version-check
"%VENV_PY%" -m pip install --quiet --disable-pip-version-check -r "%REQ%"
if errorlevel 1 goto pipfail
"%VENV_PY%" -c "import tkinter, requests, websockets, PIL, aiogram" >nul 2>nul
if errorlevel 1 goto pipfail

:ok
set "PY=%VENV_PY%"
exit /b 0

:nopython
echo.
echo На этом компьютере не найден Python 3.
echo 1. Скачайте Python с https://www.python.org/downloads/windows/
echo 2. При установке отметьте "Add python.exe to PATH"
echo 3. Запустите run.bat снова
exit /b 1

:oldpython
echo.
echo Версия Python слишком старая: нужен 3.9 или новее (лучше 3.11-3.13).
echo Скачайте новый Python с https://www.python.org/downloads/windows/
exit /b 1

:venvfail
echo.
echo Не удалось создать .venv. Проверьте установку Python 3.9+.
echo Если папка лежит в OneDrive, на время установки приостановите синхронизацию.
exit /b 1

:pipfail
echo.
echo Не удалось установить зависимости из requirements.txt.
echo Проверьте подключение к интернету (VPN/прокси) и запустите снова.
exit /b 1
