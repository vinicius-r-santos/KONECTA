@echo off
title KONECTA - camera virtual (teste)
cd /d "%~dp0.."
.venv\Scripts\python.exe vcam\ligar_camera_teste.py
REM Qualquer erro inesperado: segura a janela para a mensagem nao sumir.
if errorlevel 1 pause
