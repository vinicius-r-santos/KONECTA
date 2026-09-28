"""Modelo temporal nao pode exigir hold longo.

Falha real da sessao: predicoes de 'mae' e 'filha' com **100% de confianca**
foram descartadas porque o hold de 0.8s nunca era alcancado — o sinal nao dura
tanto. O modelo temporal ja decide sobre 30 frames (~2s); pedir mais repeticao
em cima disso e' cobrar duas vezes pela mesma estabilidade.
"""

from unittest.mock import Mock, patch

import pytest
from PyQt6.QtWidgets import QApplication

import app_central.main as main_module
from app_central.core.estabilizador import Estabilizador
from app_central.main import KonectaIntelligenceHub

_QAPP = QApplication.instance() or QApplication([])
_hubs = []


class _ExportFalso:
    def __init__(self, temporal, classes):
        self.temporal = temporal
        self.classes = classes
        self.tamanho_sequencia = 30 if temporal else None


class _MotorFalso:
    nome = "falso"

    def __init__(self, temporal=True, classes=None):
        self._export = _ExportFalso(temporal, classes or {0: "pai", 1: "mae"})

    def _carregar(self):
        pass


@pytest.fixture(autouse=True)
def _limpar(monkeypatch):
    monkeypatch.delenv("KONECTA_HOLD_S", raising=False)
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


def test_modelo_temporal_encurta_o_hold():
    hub = _hub()
    hub.motores.sinais_para_texto = _MotorFalso(temporal=True)
    hub._ajustar_hold_para_o_modelo()
    assert hub.estabilizador.tempo_hold_s <= 0.3


def test_modelo_estatico_mantem_o_hold_longo():
    """Sem janela temporal, cada frame e' independente: o hold ainda importa."""
    hub = _hub()
    antes = hub.estabilizador.tempo_hold_s
    hub.motores.sinais_para_texto = _MotorFalso(temporal=False)
    hub._ajustar_hold_para_o_modelo()
    assert hub.estabilizador.tempo_hold_s == antes


def test_ajuste_manual_tem_prioridade(monkeypatch):
    """Quem esta testando pode fixar o hold e o app nao deve sobrescrever."""
    monkeypatch.setenv("KONECTA_HOLD_S", "2.0")
    hub = _hub()
    hub.motores.sinais_para_texto = _MotorFalso(temporal=True)
    hub._ajustar_hold_para_o_modelo()
    assert hub.estabilizador.tempo_hold_s == 2.0


def test_predicao_confiante_e_curta_confirma():
    """O caso que falhava: 100% de confianca por menos de 1s."""
    est = Estabilizador(limiar_confianca=0.30, tempo_hold_s=0.25)
    assert est.avaliar("mae", 1.0, agora=0.0) is None  # vira candidato
    confirmado = est.avaliar("mae", 1.0, agora=0.4)
    assert confirmado is not None
    assert confirmado.texto == "mae"


def test_vocabulario_aparece_na_tela():
    """A pessoa precisa saber quais sinais tentar."""
    hub = _hub()
    hub.motores.sinais_para_texto = _MotorFalso(
        temporal=True, classes={0: "pai", 1: "mae", 2: "cachorro"}
    )
    hub._mostrar_vocabulario()
    texto = hub.vocabulario_label.text()
    assert "3 sinais" in texto
    assert "cachorro" in texto and "mae" in texto
    assert "dinâmicos" in texto


def test_sem_modelo_avisa_na_tela():
    hub = _hub()
    hub.motores.sinais_para_texto = None
    hub._mostrar_vocabulario()
    assert "Nenhum modelo" in hub.vocabulario_label.text()
