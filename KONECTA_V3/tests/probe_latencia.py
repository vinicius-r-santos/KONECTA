"""Confere que a latencia reportada corresponde ao tempo real.

Pipeline com atraso conhecido (LATENCIA_S). Se a medicao estiver certa, o total
reportado deve ficar proximo dele. Um total muito acima denuncia espera em fila
que a UI precisa mostrar.
"""

import asyncio
import sys
import time
from pathlib import Path

import numpy as np
from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtWidgets import QApplication

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app_central.main as principal
from app_central.pipeline.recognizer_pipeline import PipelineResult

FRAMES = 30
FPS = 15
LATENCIA_S = 0.10

medidas = []


class Camera(QThread):
    frame_ready = pyqtSignal(np.ndarray)
    error_occurred = pyqtSignal(str)

    def __init__(self, *_a, **_k):
        super().__init__()

    def run(self):
        time.sleep(0.5)
        for _ in range(FRAMES):
            self.frame_ready.emit(np.zeros((48, 64, 3), dtype=np.uint8))
            time.sleep(1.0 / FPS)
        time.sleep(1.5)
        app = QApplication.instance()
        if app is not None:
            app.quit()

    def stop(self):
        self.wait()


class Pipeline:
    def __init__(self, *_a, **_k):
        pass

    async def process_frame(self, _frame, user_id="default"):
        inicio = time.monotonic()
        await asyncio.sleep(LATENCIA_S)
        return PipelineResult(
            signal="X",
            confidence=0.9,
            latency_ms=(time.monotonic() - inicio) * 1000,
            confidence_level="high",
            validated_by="fake",
            recommendation="accept",
            user_history=[],
        )


principal.VideoCaptureWorker = Camera
principal.RecognizerPipeline = Pipeline

_hub_original = principal.KonectaIntelligenceHub


class HubEspiao(_hub_original):
    """Captura o que seria mostrado na UI."""

    def __init__(self):
        super().__init__()
        # testa o encanamento, nao o reconhecimento
        self.motores.sinais_para_texto = None

    def _on_latencia_medida(self, medida):
        medidas.append(medida)
        super()._on_latencia_medida(medida)


principal.KonectaIntelligenceHub = HubEspiao

try:
    principal.main()
except SystemExit:
    pass

if not medidas:
    print("FALHOU: nenhuma latencia foi medida", flush=True)
    sys.exit(1)

totais = [m["total_ms"] for m in medidas]
filas = [m["fila_ms"] for m in medidas]
ia = [m["ia_ms"] for m in medidas]
media_total = sum(totais) / len(totais)
esperado_ms = LATENCIA_S * 1000

print(
    f"RESULTADO amostras={len(medidas)} total_medio={media_total:.0f}ms "
    f"(esperado ~{esperado_ms:.0f}ms) fila_media={sum(filas)/len(filas):.0f}ms "
    f"ia_media={sum(ia)/len(ia):.0f}ms",
    flush=True,
)

# o total tem de conter o tempo do pipeline e nao pode inventar tempo do nada
if media_total < esperado_ms * 0.8:
    print("FALHOU: total menor que o tempo do pipeline - medicao errada", flush=True)
    sys.exit(1)
if media_total > esperado_ms * 3:
    print("FALHOU: total muito acima do pipeline - ha espera nao controlada", flush=True)
    sys.exit(1)

print("LATENCIA_OK", flush=True)
sys.exit(0)
