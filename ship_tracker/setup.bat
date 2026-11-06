@echo off
chcp 65001 >nul
setlocal EnableExtensions
title Ship Tracker — подготовка окружения
cd /d "%~dp0"

set "PY="
call "%~dp0env.bat"
if errorlevel 1 goto failed

echo.
echo Готово: окружение .venv собрано для этого компьютера.
echo   run.bat — приложение с окном
echo   bot.bat — телеграм-бот
echo.
pause
exit /b 0

:failed
echo.
echo Подготовка не завершена — смотрите сообщение выше.
pause
exit /b 1
