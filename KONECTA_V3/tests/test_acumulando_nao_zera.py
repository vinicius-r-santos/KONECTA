"""Frames de acumulacao nao podem zerar a contagem do estabilizador.

Falha real: 119 predicoes com 100% de confianca, nenhuma confirmada. Causa: o
modelo temporal preve a cada N frames e devolve texto vazio nos frames do meio
(status 'acumulando'). O app tratava vazio como 'maos sairam de quadro' e
reiniciava o candidato — o hold nunca era alcancado.
"""

import asyncio
from unittest.mock import Mock, patch

import pytest
from PyQt6.QtWidgets import QApplication

import app_central.main as main_module
from app_central.main import KonectaIntelligenceHub
from app_central.providers.base import ResultadoTexto
from app_central.providers.signlab_sinais import SinaisSignlab, sinais_visiveis

_QAPP = QApplication.instance() or QApplication([])
_hubs = []


class _MotorTemporal:
    """Imita o provider temporal: preve a cada 3 frames, vazio no meio."""

    nome = "temporal"

    def __init__(self, sinal="mae", passo=3):
        self.sinal = sinal
        self.passo = passo
        self.chamadas = 0

    async def reconhecer(self, _frame):
        self.chamadas += 1
        if self.chamadas % self.passo:
            return ResultadoTexto(
                texto="",
                confianca=0.0,
                latencia_ms=1.0,
                fonte=self.nome,
                detalhes={"status": "acumulando", "janela": 30},
            )
        return ResultadoTexto(
            texto=self.sinal,
            confianca=1.0,
            latencia_ms=1.0,
            fonte=self.nome,
            detalhes={"temporal": True, "janela": 30},
        )


class _MotorSemMaos:
    nome = "vazio"

    async def reconhecer(self, _frame):
        return ResultadoTexto(
            texto="",
            confianca=0.0,
            latencia_ms=1.0,
            fonte=self.nome,
            detalhes={"status": "sem_maos", "janela": 0},
        )


@pytest.fixture(autouse=True)
def _limpar():
    yield
    while _hubs:
        hub = _hubs.pop()
        loop = getattr(hub, "_loop", None)
        if loop and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(loop.stop)
            except RuntimeError:
                pass


def _hub():
    with patch.object(main_module, "VideoCaptureWorker", Mock()), \
         patch.object(main_module, "listar_cameras", lambda: []):
        hub = KonectaIntelligenceHub()
    _hubs.append(hub)
    return hub


def _rodar(hub, n):
    async def _cenario():
        for _ in range(n):
            await hub._run_pipeline(None)

    asyncio.run(_cenario())


def test_acumulando_nao_reinicia_o_candidato():
    """O caso que falhava em producao."""
    hub = _hub()
    hub.estabilizador.tempo_hold_s = 0.0
    hub.motores.sinais_para_texto = _MotorTemporal(sinal="mae")

    _rodar(hub, 12)  # 4 predicoes reais entre frames de acumulacao

    assert hub._sinal_exibido == "mae", "predicao de 100% nunca confirmou"


def test_maos_fora_de_quadro_ainda_zera():
    """A distincao precisa continuar valendo para o caso legitimo."""
    hub = _hub()
    hub.estabilizador.tempo_hold_s = 0.0
    hub.motores.sinais_para_texto = _MotorTemporal(sinal="pai")
    _rodar(hub, 12)
    assert hub._sinal_exibido == "pai"

    # agora as maos somem de verdade
    hub.motores.sinais_para_texto = _MotorSemMaos()
    _rodar(hub, 3)
    assert hub.estabilizador._candidato is None
    assert hub.estabilizador._ultimo_confirmado is None


def test_sinal_repetido_apos_sair_de_quadro():
    """Tirar a mao e refazer o mesmo sinal deve registrar de novo."""
    hub = _hub()
    hub.estabilizador.tempo_hold_s = 0.0
    hub.motores.sinais_para_texto = _MotorTemporal(sinal="pai")
    _rodar(hub, 12)

    hub.motores.sinais_para_texto = _MotorSemMaos()
    _rodar(hub, 3)

    hub.motores.sinais_para_texto = _MotorTemporal(sinal="pai")
    _rodar(hub, 12)

    assert hub.estabilizador.historico.count("pai") == 2


class _MotorNenhum:
    """Prevê "(nenhum)" com 100%, passando pelo filtro de verdade do provider."""

    nome = "nenhum"

    async def reconhecer(self, _frame):
        return SinaisSignlab._filtrar_nenhum(ResultadoTexto(
            texto="(nenhum)", confianca=1.0, latencia_ms=1.0,
            fonte=self.nome, detalhes={"temporal": True},
        ))


def test_gesto_que_nao_e_sinal_nunca_vira_legenda():
    """Numa reunião a pessoa coça o rosto, digita: isso não pode aparecer."""
    hub = _hub()
    hub.estabilizador.tempo_hold_s = 0.0
    hub.motores.sinais_para_texto = _MotorNenhum()

    _rodar(hub, 30)

    assert hub._sinal_exibido != "(nenhum)"
    assert "(nenhum)" not in hub.estabilizador.historico


def test_nenhum_zera_o_candidato_como_mao_fora_de_quadro():
    """Parar de sinalizar e voltar ao mesmo sinal registra de novo."""
    hub = _hub()
    hub.estabilizador.tempo_hold_s = 0.0
    hub.motores.sinais_para_texto = _MotorTemporal(sinal="pai")
    _rodar(hub, 12)
    assert hub._sinal_exibido == "pai"

    hub.motores.sinais_para_texto = _MotorNenhum()
    _rodar(hub, 3)
    assert hub.estabilizador._candidato is None
    assert hub._sinal_exibido == "pai"  # a tela mantém o último sinal

    hub.motores.sinais_para_texto = _MotorTemporal(sinal="pai")
    _rodar(hub, 12)
    assert hub.estabilizador.historico.count("pai") == 2


def test_vocabulario_e_medicao_nao_listam_nenhum():
    classes = {0: "Sim", 1: "(nenhum)", 2: "Não", 3: " (Nenhum) "}
    assert sinais_visiveis(classes) == ["Não", "Sim"]

