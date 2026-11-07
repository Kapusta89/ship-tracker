@echo off
rem Файл должен оставаться с переводами строк CRLF (см. .gitattributes).
chcp 65001 >nul
rem ===========================================================
rem  Подготовка Python для Ship Tracker (приложение и бот).
rem  Вызов: call env.bat [app^|bot]   ->  при успехе PY = python.exe
rem  Код возврата: 0 — готово, 1 — ошибка (текст уже напечатан).
rem
rem  Окружение живёт ВНЕ папки проекта: %LOCALAPPDATA%\ShipTracker\venv.
rem  Папка в OneDrive синхронизируется, и .venv внутри неё приезжает на
rem  другой компьютер битым — из-за этого приложение «не открывалось».
rem ===========================================================
set "ROOT=%~dp0"
set "REQ=%ROOT%requirements.txt"
set "NEED_TK=1"
if /i "%~1"=="bot" set "NEED_TK=0"

set "STATE=%LOCALAPPDATA%"
if not defined STATE set "STATE=%USERPROFILE%"
if not defined STATE set "STATE=%TEMP%"
set "HOME_DIR=%STATE%\ShipTracker"
if not exist "%HOME_DIR%" mkdir "%HOME_DIR%" >nul 2>nul
set "VENV_DIR=%HOME_DIR%\venv"
set "VENV_PY=%VENV_DIR%\Scripts\python.exe"
set "LOG=%HOME_DIR%\env.log"
echo ==== %DATE% %TIME% ====>>"%LOG%"

rem ---------------------------------------------------------- 1. Python
set "BASE="
py -3 -c "import sys" >nul 2>>"%LOG%"
if not errorlevel 1 set "BASE=py -3"
if not defined BASE (
    python -c "import sys" >nul 2>>"%LOG%"
    if not errorlevel 1 set "BASE=python"
)
if not defined BASE (
    python3 -c "import sys" >nul 2>>"%LOG%"
    if not errorlevel 1 set "BASE=python3"
)
if not defined BASE goto nopython
%BASE% -c "import sys; print(sys.version)" >>"%LOG%" 2>&1
%BASE% -c "import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 9) else 1)" >nul 2>>"%LOG%"
if errorlevel 1 goto oldpython
%BASE% -c "import sys; print(sys.executable)" >>"%LOG%" 2>&1

if "%NEED_TK%"=="1" (
    %BASE% -c "import tkinter" >nul 2>>"%LOG%"
    if errorlevel 1 goto notkinter
)

rem ---------------------------------------------------------- 2. окружение
if not exist "%VENV_PY%" goto create
"%VENV_PY%" -c "import sys" >nul 2>>"%LOG%"
if errorlevel 1 goto recreate
"%VENV_PY%" -c "import requests, websockets, PIL, aiogram" >nul 2>>"%LOG%"
if errorlevel 1 goto deps
if "%NEED_TK%"=="1" (
    "%VENV_PY%" -c "import tkinter" >nul 2>>"%LOG%"
    if errorlevel 1 goto notkinter
)
goto ok

:deps
echo Проверяю зависимости Python-окружения...
"%VENV_PY%" -m pip install --disable-pip-version-check --no-input -r "%REQ%" >>"%LOG%" 2>&1
if errorlevel 1 goto piptrouble
"%VENV_PY%" -c "import requests, websockets, PIL, aiogram" >nul 2>>"%LOG%"
if errorlevel 1 goto piptrouble
goto ok

:create
echo Первый запуск на этом компьютере: готовлю Python-окружение.
echo Это может занять минуту, окно откроется само.
%BASE% -m venv "%VENV_DIR%" >>"%LOG%" 2>&1
if errorlevel 1 goto venvfail
"%VENV_PY%" -m pip install --upgrade pip --disable-pip-version-check --no-input >>"%LOG%" 2>&1
"%VENV_PY%" -m pip install --disable-pip-version-check --no-input -r "%REQ%" >>"%LOG%" 2>&1
if errorlevel 1 goto piptrouble
"%VENV_PY%" -c "import requests, websockets, PIL, aiogram" >nul 2>>"%LOG%"
if errorlevel 1 goto piptrouble
goto ok

:recreate
echo Окружение не подходит этому компьютеру — собираю заново...
if exist "%VENV_DIR%" rd /s /q "%VENV_DIR%" >nul 2>nul
goto create

:piptrouble
echo Не удалось поставить пакеты в отдельное окружение.
echo Пробую установить их в системный Python...
%BASE% -m pip install --user --disable-pip-version-check --no-input -r "%REQ%" >>"%LOG%" 2>&1
if errorlevel 1 goto pipfail
%BASE% -c "import requests, websockets, PIL, aiogram" >nul 2>>"%LOG%"
if errorlevel 1 goto pipfail
if "%NEED_TK%"=="1" (
    %BASE% -c "import tkinter" >nul 2>>"%LOG%"
    if errorlevel 1 goto notkinter
)
for /f "delims=" %%P in ('%BASE% -c "import sys;print(sys.executable)"') do set "PY=%%P"
if not defined PY goto pipfail
exit /b 0

:ok
set "PY=%VENV_PY%"
exit /b 0

:nopython
echo.
echo На этом компьютере не найден Python 3.
echo 1. Скачайте Python 3.12 с https://www.python.org/downloads/windows/
echo 2. При установке отметьте "Add python.exe to PATH" и "tcl/tk and IDLE"
echo 3. Запустите этот файл снова
echo.
echo Подробный журнал: %LOG%
exit /b 1

:oldpython
echo.
echo Версия Python слишком старая: нужна 3.9 или новее.
echo Скачайте Python 3.12 с https://www.python.org/downloads/windows/
echo Подробный журнал: %LOG%
exit /b 1

:notkinter
echo.
echo Python установлен без библиотеки tkinter, поэтому окно не открывается.
echo Переустановите Python с https://www.python.org/downloads/windows/
echo и при установке отметьте "tcl/tk and IDLE".
echo Если нужен только бот — запускайте bot.bat: ему tkinter не нужен.
echo Подробный журнал: %LOG%
exit /b 1

:venvfail
echo.
echo Не удалось создать отдельное окружение в %VENV_DIR%.
echo Проверьте, что на диске есть место и антивирус не блокирует Python.
echo Подробный журнал: %LOG%
exit /b 1

:pipfail
echo.
echo Не удалось установить пакеты из requirements.txt.
echo Проверьте интернет (VPN/прокси) и запустите снова.
echo Подробный журнал: %LOG%
exit /b 1
