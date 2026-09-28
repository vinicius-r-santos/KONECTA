"""Troca de camera nao pode travar a janela nem misturar as duas fontes.

Reclamacao real da sessao: ao escolher outra webcam a tela congelava (medido no
log: 51s) e a imagem alternava entre as duas cameras.
"""

import time
from unittest.mock import Mock, patch

import pytest
from PyQt6.QtWidgets import QApplication

import app_central.main as main_module
from app_central.capture.cameras import Camera
from app_central.main import KonectaIntelligenceHub

_QAPP = QApplication.instance() or QApplication([])
_hubs = []


class _WorkerFalso:
    """Substitui VideoCaptureWorker sem tocar em hardware."""

    instancias = []

    def __init__(self, camera_id=0, **_kw):
        self.camera_id = camera_id
        self.parado = False
        self.desconectado = False
        self.frame_ready = Mock()
        self.frame_ready.disconnect = self._marcar_desconexao
        self.error_occurred = Mock()
        self.error_occurred.disconnect = Mock()
        _WorkerFalso.instancias.append(self)

    def _marcar_desconexao(self, *_a):
        self.desconectado = True

    def start(self):
        pass

    def stop(self, espera_ms=2000):
        self.parado = True


@pytest.fixture(autouse=True)
def _limpar():
    _WorkerFalso.instancias.clear()
    yield
    while _hubs:
        hub = _hubs.pop()
        loop = getattr(hub, "_loop", None)
        if loop and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(loop.stop)
            except RuntimeError:
                pass


def _hub_com_duas_cameras():
    duas = [Camera(0, 640, 480), Camera(1, 1280, 720)]
    with patch.object(main_module, "VideoCaptureWorker", _WorkerFalso), \
         patch.object(main_module, "listar_cameras", lambda: duas):
        hub = KonectaIntelligenceHub()
    _hubs.append(hub)
    return hub


def test_seletor_lista_as_cameras_encontradas():
    hub = _hub_com_duas_cameras()
    rotulos = [hub.camera_combo.itemText(i) for i in range(hub.camera_combo.count())]
    assert len(rotulos) == 2
    assert "Webcam interna" in rotulos[0]


def test_troca_cria_worker_na_camera_escolhida():
    hub = _hub_com_duas_cameras()
    with patch.object(main_module, "VideoCaptureWorker", _WorkerFalso):
        hub._trocar_camera(1)
    assert hub.camera_worker.camera_id == 1


def test_worker_anterior_e_desconectado_e_parado():
    """Sem desconectar, as duas cameras entregam frames e a imagem alterna."""
    hub = _hub_com_duas_cameras()
    anterior = hub.camera_worker
    with patch.object(main_module, "VideoCaptureWorker", _WorkerFalso):
        hub._trocar_camera(1)
    assert anterior.desconectado, "worker antigo continuou conectado"
    assert anterior.parado, "worker antigo continuou rodando"
    assert hub.camera_worker is not anterior


def test_escolher_a_mesma_camera_nao_reinicia():
    hub = _hub_com_duas_cameras()
    atual = hub.camera_worker
    with patch.object(main_module, "VideoCaptureWorker", _WorkerFalso):
        hub._trocar_camera(0)
    assert hub.camera_worker is atual


def test_troca_nao_bloqueia_a_interface():
    """A troca roda na thread da GUI: nao pode demorar."""
    hub = _hub_com_duas_cameras()
    with patch.object(main_module, "VideoCaptureWorker", _WorkerFalso):
        inicio = time.monotonic()
        hub._trocar_camera(1)
        decorrido = time.monotonic() - inicio
    assert decorrido < 1.0, f"troca levou {decorrido:.1f}s na thread da interface"


def test_stop_tem_prazo():
    """stop() sem prazo congelava a janela junto com a thread de captura."""
    import inspect

    from app_central.utils.video_capture import VideoCaptureWorker

    assinatura = inspect.signature(VideoCaptureWorker.stop)
    assert "espera_ms" in assinatura.parameters


def test_windows_usa_directshow():
    """Backend padrao levava 16s para abrir a camera 0; DSHOW leva 1s."""
    import inspect

    from app_central.utils import video_capture

    fonte = inspect.getsource(video_capture.VideoCaptureWorker._open_camera)
    assert "CAP_DSHOW" in fonte
