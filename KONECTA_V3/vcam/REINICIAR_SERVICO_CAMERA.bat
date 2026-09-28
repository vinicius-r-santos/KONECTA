@echo off
setlocal
title KONECTA - reiniciar servico de camera
REM Depois de recompilar a DLL da camera virtual: o servico de camera do
REM Windows (Frame Server) segura a versao antiga na memoria ate reiniciar.
REM Ele volta sozinho na proxima vez que um app abrir uma camera.
REM Qualquer app usando camera agora perde a imagem por um instante.

net session >nul 2>&1
if errorlevel 1 (
    echo [ERRO] Precisa de administrador.
    echo        Clique com o botao direito neste arquivo e escolha
    echo        "Executar como administrador".
    pause
    exit /b 1
)

net stop FrameServer /y
net stop FrameServerMonitor /y >nul 2>&1
echo.
echo [OK]   Servico de camera parado. Ele volta sozinho quando um app abrir a camera.
pause
