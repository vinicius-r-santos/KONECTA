"""Mede o que acontece quando a camera produz mais rapido que o pipeline consome.

_process_frame agenda uma corrotina por frame sem limite. Este roteiro emite
frames em ritmo de camera com um pipeline lento de proposito e reporta quantas
tarefas ficaram pendentes.
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

FRAMES = 150
FPS = 30
LATENCIA_PIPELINE_S = 0.10  # 100ms por frame: 3x mais lento que a camera

agendados = []
concluidos = []
em_voo = [0]
pico = [0]


class CameraRapida(QThread):
    frame_ready = pyqtSignal(np.ndarray)
    error_occurred = pyqtSignal(str)

    def __init__(self, *_a, **_k):
        super().__init__()

    def run(self):
        time.sleep(0.5)
        for _ in range(FRAMES):
            self.frame_ready.emit(np.zeros((48, 64, 3), dtype=np.uint8))
            time.sleep(1.0 / FPS)
        time.sleep(3.0)
        app = QApplication.instance()
        if app is not None:
            app.quit()

    def stop(self):
        self.wait()


class PipelineLento:
    def __init__(self, *_a, **_k):
        pass

    async def process_frame(self, _frame, user_id="default"):
        agendados.append(1)
        em_voo[0] += 1
        pico[0] = max(pico[0], em_voo[0])
        time.sleep(LATENCIA_PIPELINE_S)  # BLOQUEANTE: imita motor CPU-bound
        em_voo[0] -= 1
        concluidos.append(1)
        return PipelineResult(
            signal="X",
            confidence=0.9,
            latency_ms=1.0,
            confidence_level="high",
            validated_by="fake",
            recommendation="accept",
            user_history=[],
        )


_HubOrig = principal.KonectaIntelligenceHub


class _HubSemProvider(_HubOrig):
    def __init__(self):
        super().__init__()
        # testa o encanamento, nao o reconhecimento
        self.motores.sinais_para_texto = None


principal.KonectaIntelligenceHub = _HubSemProvider
principal.VideoCaptureWorker = CameraRapida
principal.RecognizerPipeline = PipelineLento

inicio = time.time()
try:
    principal.main()
except SystemExit:
    pass
duracao = time.time() - inicio

enfileirados = len(agendados) - len(concluidos)

print(
    f"RESULTADO frames_emitidos={FRAMES} iniciados={len(agendados)} "
    f"concluidos={len(concluidos)} presos_na_fila={enfileirados} "
    f"pico_em_voo={pico[0]} duracao={duracao:.1f}s",
    flush=True,
)

# Capacidade real: durante os FRAMES/FPS segundos de emissao, um pipeline de
# LATENCIA_PIPELINE_S so' consegue processar esta quantidade. Processar muito
# mais que isso significa que, terminada a emissao, o sistema ainda estava
# consumindo frames velhos acumulados - exatamente o atraso que queremos evitar.
capacidade = (FRAMES / FPS) / LATENCIA_PIPELINE_S
teto = capacidade * 1.3  # folga para o tempo de drenagem no fim do roteiro

if enfileirados > 0:
    print(f"FALHOU: {enfileirados} frames ficaram na fila sem rodar", flush=True)
    sys.exit(1)
if pico[0] > 1:
    print(f"FALHOU: {pico[0]} frames em processamento simultaneo", flush=True)
    sys.exit(1)
if len(agendados) > teto:
    print(
        f"FALHOU: processou {len(agendados)} frames com capacidade para "
        f"~{capacidade:.0f} - estava consumindo backlog velho",
        flush=True,
    )
    sys.exit(1)

print("PROBE_OK", flush=True)
sys.exit(0)
