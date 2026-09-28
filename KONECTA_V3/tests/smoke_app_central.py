"""Sobe o app_central de verdade com camera falsa e conta frames processados.

Roda como processo separado (ver tests/test_app_central_smoke.py) porque a falha
que este roteiro cobre nao e' uma excecao capturavel: o PyQt5 aborta o processo
inteiro quando um slot levanta excecao. Um teste in-process morreria junto com o
runner em vez de falhar.

Saida: imprime SMOKE_OK:<n> e sai com 0 quando processou frames de fato.
"""

import sys
import time
from pathlib import Path

import numpy as np
from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtWidgets import QApplication

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app_central.main as principal
from app_central.pipeline.recognizer_pipeline import PipelineResult

FRAMES = 10
processados = []


class CameraFalsa(QThread):
    """Substitui VideoCaptureWorker: emite frames sinteticos e encerra o app."""

    frame_ready = pyqtSignal(np.ndarray)
    error_occurred = pyqtSignal(str)

    def __init__(self, *_args, **_kwargs):
        super().__init__()

    def run(self):
        time.sleep(0.5)  # deixa a janela e o loop subirem
        for _ in range(FRAMES):
            self.frame_ready.emit(np.zeros((48, 64, 3), dtype=np.uint8))
            time.sleep(0.05)
        time.sleep(1.5)  # tempo para as corrotinas rodarem
        app = QApplication.instance()
        if app is not None:
            app.quit()

    def stop(self):
        self.wait()


class PipelineFalso:
    """Substitui RecognizerPipeline: nao precisa de modelo em disco."""

    def __init__(self, *_args, **_kwargs):
        pass

    async def process_frame(self, _frame, user_id="default"):
        processados.append(user_id)
        return PipelineResult(
            signal="TESTE",
            confidence=0.9,
            latency_ms=1.0,
            confidence_level="high",
            validated_by="fake",
            recommendation="accept",
            user_history=[],
        )


principal.VideoCaptureWorker = CameraFalsa
principal.RecognizerPipeline = PipelineFalso

# Guardamos a instancia: depois do quit() a janela some de topLevelWidgets e os
# contadores ficariam inacessiveis.
_criada = []
_HubOriginal = principal.KonectaIntelligenceHub


class HubEspiao(_HubOriginal):
    def __init__(self):
        super().__init__()
        # este roteiro testa o encanamento do app, nao o reconhecimento:
        # sem provider, _reconhecer_frame cai no pipeline falso injetado
        self.motores.sinais_para_texto = None
        _criada.append(self)


principal.KonectaIntelligenceHub = HubEspiao

try:
    principal.main()
except SystemExit:
    pass

janela = _criada[0] if _criada else None
descartados = getattr(janela, "frames_descartados", 0)

# Todo frame emitido tem de ter um destino conhecido: processado ou descartado
# de proposito. O que nao pode existir e' frame que sumiu sem explicacao.
print(
    f"RESULTADO emitidos={FRAMES} processados={len(processados)} descartados={descartados}",
    flush=True,
)

if not processados:
    print("SMOKE_FALHOU: nenhum frame chegou ao pipeline", flush=True)
    sys.exit(1)

if len(processados) + descartados != FRAMES:
    print(
        f"SMOKE_FALHOU: {FRAMES - len(processados) - descartados} frames sumiram",
        flush=True,
    )
    sys.exit(1)

print(f"SMOKE_OK:{len(processados)}", flush=True)
sys.exit(0)
