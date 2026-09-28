"""A palavra na tela nao pode oscilar entre sinais.

Reclamacao real da sessao com interprete: com limiar baixo, a palavra trocava
varias vezes por segundo entre sinais parecidos (filho/filha) e ficava
ilegivel. Estes testes fixam o comportamento esperado.
"""

import asyncio
from unittest.mock import Mock, patch

import pytest
from PyQt6.QtWidgets import QApplication

import app_central.main as main_module

# Criar um QWidget sem QApplication aborta o processo inteiro no Qt — nao e'
# excecao capturavel. Tem de vir antes de qualquer janela.
_QAPP = QApplication.instance() or QApplication([])
from app_central.main import KonectaIntelligenceHub
from app_central.providers.base import ResultadoTexto

_hubs = []


def _hub():
    with patch.object(main_module, "VideoCaptureWorker", Mock()):
        hub = KonectaIntelligenceHub()
    _hubs.append(hub)
    return hub


@pytest.fixture(autouse=True)
def _encerrar():
    yield
    while _hubs:
        hub = _hubs.pop()
        loop = getattr(hub, "_loop", None)
        if loop is None or loop.is_closed():
            continue

        def _parar():
            for tarefa in asyncio.all_tasks(loop):
                tarefa.cancel()
            loop.stop()

        try:
            loop.call_soon_threadsafe(_parar)
        except RuntimeError:
            continue
        thread = getattr(hub, "_loop_thread", None)
        if thread is not None:
            thread.join(timeout=3)
        if not loop.is_closed():
            loop.close()


class _MotorSequencia:
    """Devolve a sequencia de predicoes que o teste quiser."""

    nome = "teste"

    def __init__(self, respostas):
        self._respostas = list(respostas)

    async def reconhecer(self, _frame):
        texto, conf = self._respostas.pop(0)
        return ResultadoTexto(texto=texto, confianca=conf, latencia_ms=1.0, fonte=self.nome)


def _rodar(hub, n):
    async def _cenario():
        for _ in range(n):
            await hub._run_pipeline(None)

    asyncio.run(_cenario())


def test_limiar_padrao_e_alto():
    """0.45 deixava sinal fraco passar e a palavra piscava.

    O hold não é verificado aqui: ele depende da modalidade do modelo
    (temporal usa hold curto, ver test_hold_temporal.py).
    """
    hub = _hub()
    assert hub.estabilizador.limiar_confianca >= 0.70


def test_palavra_nao_muda_com_predicao_oscilante():
    """filho/filha alternando nao pode trocar a palavra na tela."""
    hub = _hub()
    hub.estabilizador.tempo_hold_s = 0.0
    alternado = [("filho", 0.9), ("filha", 0.9)] * 6
    hub.motores.sinais_para_texto = _MotorSequencia(alternado)

    _rodar(hub, len(alternado))

    # com hold, alternancia a cada frame nunca confirma nada
    assert hub._sinal_exibido == ""


def test_palavra_persiste_apos_confirmar():
    """Confirmado o sinal, ele fica na tela — nao volta a vazio."""
    hub = _hub()
    hub.estabilizador.tempo_hold_s = 0.0
    respostas = [("pai", 0.9), ("pai", 0.9)] + [("", 0.0)] * 5
    hub.motores.sinais_para_texto = _MotorSequencia(respostas)

    _rodar(hub, len(respostas))

    assert hub._sinal_exibido == "pai"


def test_sinal_fraco_nao_derruba_o_confirmado():
    """Predicao abaixo do limiar nao pode apagar a palavra ja confirmada."""
    hub = _hub()
    hub.estabilizador.tempo_hold_s = 0.0
    hub.estabilizador.limiar_confianca = 0.75
    respostas = [("mae", 0.9), ("mae", 0.9), ("cachorro", 0.3), ("filha", 0.2)]
    hub.motores.sinais_para_texto = _MotorSequencia(respostas)

    _rodar(hub, len(respostas))

    assert hub._sinal_exibido == "mae"


def test_troca_quando_outro_sinal_se_sustenta():
    """A palavra deve mudar quando outro sinal realmente se confirma."""
    hub = _hub()
    hub.estabilizador.tempo_hold_s = 0.0
    respostas = [("pai", 0.9), ("pai", 0.9), ("mae", 0.9), ("mae", 0.9)]
    hub.motores.sinais_para_texto = _MotorSequencia(respostas)

    _rodar(hub, len(respostas))

    assert hub._sinal_exibido == "mae"


def test_tela_mostra_a_acuracia_junto():
    hub = _hub()
    from app_central.pipeline.recognizer_pipeline import PipelineResult

    hub._on_recognition_updated(
        PipelineResult(
            signal="pai",
            confidence=0.87,
            latency_ms=10.0,
            confidence_level="high",
            validated_by="teste",
            recommendation="accept",
            user_history=[],
        )
    )
    texto = hub.signal_label.text()
    assert "pai" in texto
    assert "87%" in texto


def test_historico_nao_repete_o_mesmo_sinal():
    """Segurar a mao nao pode encher o historico com a mesma palavra."""
    hub = _hub()
    from app_central.pipeline.recognizer_pipeline import PipelineResult

    def _resultado():
        return PipelineResult(
            signal="pai",
            confidence=0.9,
            latency_ms=10.0,
            confidence_level="high",
            validated_by="teste",
            recommendation="accept",
            user_history=[],
        )

    for _ in range(5):
        hub._on_recognition_updated(_resultado())

    linhas = [l for l in hub.history_display.toPlainText().split("\n") if l.strip()]
    assert len(linhas) == 1
