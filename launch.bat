@echo off
rem Файл должен оставаться с переводами строк CRLF (см. .gitattributes).
rem Запуск текущей копии Ship Tracker из этой папки
chcp 65001 >nul
cd /d "%~dp0ship_tracker"
call "%~dp0ship_tracker\run.bat" %*
