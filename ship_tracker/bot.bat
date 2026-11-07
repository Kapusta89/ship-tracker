@echo off
rem Файл должен оставаться с переводами строк CRLF (см. .gitattributes).
chcp 65001 >nul
setlocal EnableExtensions
title Ship Radar Bot
cd /d "%~dp0"

set "PY="
call "%~dp0env.bat"
if errorlevel 1 goto failed

echo.
echo Запускаю бота. Остановить — Ctrl+C.
echo.
"%PY%" -X utf8 telegram_bot.py %*
set "CODE=%ERRORLEVEL%"
if not "%CODE%"=="0" (
    echo.
    echo Бот завершился с кодом %CODE%.
    pause
)
exit /b %CODE%

:failed
echo.
echo Не удалось подготовить Python — смотрите сообщение выше.
pause
exit /b 1
