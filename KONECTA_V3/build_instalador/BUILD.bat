@echo off
setlocal
title KONECTA - gerar instalador
cd /d "%~dp0.."
REM Gera build_instalador\Output\KONECTA_Setup_<versao>.exe. Leva ~15 min.
REM
REM A ordem importa: o build principal APAGA dist\KONECTA inteiro antes de
REM montar, entao o worker temporal tem de vir depois dele, nunca antes.
REM
REM Precisa nesta maquina: .venv e .venv-temporal com pyinstaller, o modelo
REM "small" do Whisper no cache do Hugging Face (ja fica la depois do primeiro
REM uso do audio) e o Inno Setup 6 (winget install JRSoftware.InnoSetup).

set PYTHONIOENCODING=
set PYTHONUTF8=

echo [1/3] KONECTA + whisper_worker + avatar_server...
.venv\Scripts\python.exe -m PyInstaller build_instalador\konecta.spec --distpath build_instalador\dist --workpath build_instalador\work --noconfirm --log-level WARN
if errorlevel 1 goto erro

echo [2/3] sinais_worker (TensorFlow, ambiente separado)...
.venv-temporal\Scripts\python.exe -m PyInstaller build_instalador\konecta_temporal.spec --distpath build_instalador\dist\KONECTA --workpath build_instalador\work_temporal --noconfirm --log-level WARN
if errorlevel 1 goto erro

echo [3/3] instalador...
set ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe
if not exist "%ISCC%" set ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe
"%ISCC%" /Q build_instalador\konecta.iss
if errorlevel 1 goto erro

echo.
echo [OK] Instalador pronto em build_instalador\Output\
exit /b 0

:erro
echo.
echo [ERRO] O build parou no passo acima.
exit /b 1
