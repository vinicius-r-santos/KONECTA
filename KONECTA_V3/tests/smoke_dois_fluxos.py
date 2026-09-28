"""Sobe o app com os DOIS fluxos e verifica cada um ponta a ponta.

Fluxo do surdo:    camera -> sinal -> texto confirmado -> videochamada
Fluxo do ouvinte:  audio do PC -> texto -> avatar

O audio nao e' simulado: o sintetizador de voz do Windows fala uma frase
conhecida, o loopback captura e o Whisper transcreve. E' o unico jeito de provar
que a captura funciona de verdade.
"""

import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np
from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtWidgets import QApplication

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app_central.main as principal
from app_central.providers.base import ResultadoTexto

FRASE = "bom dia tudo bem com voce"
SINAL = "OBRIGADO"

recebidos_avatar = []
enviados_videochamada = []
transcricoes = []


class CameraFalsa(QThread):
    """Emite frames que o motor falso sempre reconhece como SINAL."""

    frame_ready = pyqtSignal(np.ndarray)
    error_occurred = pyqtSignal(str)

    def __init__(self, *_a, **_k):
        super().__init__()

    def run(self):
        time.sleep(1.0)
        for _ in range(40):  # tempo suficiente para o hold confirmar
            self.frame_ready.emit(np.zeros((48, 64, 3), dtype=np.uint8))
            time.sleep(0.05)

    def stop(self):
        self.wait()


class MotorSinaisFalso:
    """Reconhece sempre o mesmo sinal: isola o teste do modelo real."""

    nome = "falso"

    async def disponivel(self):
        return True

    async def reconhecer(self, _frame):
        return ResultadoTexto(texto=SINAL, confianca=0.95, latencia_ms=1.0, fonte=self.nome)

    async def encerrar(self):
        pass


class AvatarFalso:
    """Registra o que seria enviado ao avatar."""

    nome = "avatar_falso"
    injecao_direta = True

    async def disponivel(self):
        return True

    async def sinalizar(self, texto):
        recebidos_avatar.append(texto)
        from app_central.providers.base import ResultadoSinais

        return ResultadoSinais(glosa="", texto_origem=texto, latencia_ms=1.0, fonte=self.nome)

    async def encerrar(self):
        pass


class VideochamadaFalsa:
    nome = "falsa"
    injecao_direta = True

    async def enviar_legenda(self, texto):
        enviados_videochamada.append(texto)
        return True

    async def encerrar(self):
        pass


def falar():
    """Fala a frase pelo sintetizador do Windows, para o loopback capturar."""
    time.sleep(2.5)
    comando = (
        "Add-Type -AssemblyName System.Speech; "
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        f'$s.Speak("{FRASE}")'
    )
    subprocess.run(
        ["powershell", "-NoProfile", "-Command", comando], capture_output=True
    )


_HubOriginal = principal.KonectaIntelligenceHub
_criada = []


class HubEspiao(_HubOriginal):
    def __init__(self):
        super().__init__()
        _criada.append(self)
        # troca os motores por duplos depois da montagem
        self.motores.sinais_para_texto = MotorSinaisFalso()
        self.motores.texto_para_sinais = AvatarFalso()
        self.videochamada = VideochamadaFalsa()

    def _on_fala_transcrita(self, texto):
        transcricoes.append(texto)
        super()._on_fala_transcrita(texto)


principal.VideoCaptureWorker = CameraFalsa
principal.KonectaIntelligenceHub = HubEspiao

threading.Thread(target=falar, daemon=True).start()


def encerrar():
    time.sleep(22)
    app = QApplication.instance()
    if app is not None:
        app.quit()


threading.Thread(target=encerrar, daemon=True).start()

try:
    principal.main()
except SystemExit:
    pass

hub = _criada[0] if _criada else None

print(f"RESULTADO sinais_para_videochamada={enviados_videochamada}", flush=True)
print(f"          transcricoes={transcricoes}", flush=True)
print(f"          textos_para_avatar={recebidos_avatar}", flush=True)
print(f"          frames_processados={getattr(hub, 'frames_processados', 0)}", flush=True)

falhas = []

# fluxo do surdo: o sinal precisa ter sido confirmado e enviado
if SINAL not in enviados_videochamada:
    falhas.append("sinal nao chegou a videochamada")
# e o hold tem de ter evitado repeticao a cada frame
if enviados_videochamada.count(SINAL) > 2:
    falhas.append(f"sinal repetido {enviados_videochamada.count(SINAL)}x (hold nao segurou)")

# fluxo do ouvinte: a fala precisa ter virado texto e ido ao avatar
if not transcricoes:
    falhas.append("nenhuma fala foi transcrita")
elif not recebidos_avatar:
    falhas.append("texto transcrito nao chegou ao avatar")

if falhas:
    for f in falhas:
        print(f"FALHOU: {f}", flush=True)
    sys.exit(1)

print("DOIS_FLUXOS_OK", flush=True)
sys.exit(0)
