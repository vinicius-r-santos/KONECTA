@echo off
setlocal
title KONECTA - registrar camera virtual
REM Registra a DLL da camera virtual KONECTA no Windows. Precisa ser feito uma
REM vez, como administrador: o Windows so aceita fonte de camera virtual
REM registrada em HKLM, porque ela e' carregada pelos servicos de camera.
REM Para desfazer: regsvr32 /u "<esta pasta>\x64\Release\VCamSampleSource.dll"

net session >nul 2>&1
if errorlevel 1 (
    echo [ERRO] Precisa de administrador.
    echo        Clique com o botao direito neste arquivo e escolha
    echo        "Executar como administrador".
    pause
    exit /b 1
)

set DLL=%~dp0x64\Release\VCamSampleSource.dll
if not exist "%DLL%" (
    echo [ERRO] DLL nao encontrada: %DLL%
    pause
    exit /b 1
)

regsvr32 /s "%DLL%"
if errorlevel 1 (
    echo [ERRO] O Windows recusou o registro.
) else (
    echo [OK]   Camera KONECTA registrada.
)
pause
