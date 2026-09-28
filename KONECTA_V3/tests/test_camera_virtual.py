"""Câmera virtual KONECTA: o que o KONECTA escreve é o que o Meet/Zoom recebe.

Integração de verdade com o serviço de câmera do Windows: só roda com a DLL de
vcam/ registrada (REGISTRAR_CAMERA.bat). Abre apenas a câmera KONECTA, nunca a
webcam física.
"""
import time
import winreg

import numpy as np
import pytest

from app_central.capture import camera_virtual as cv_mod

cv2 = pytest.importorskip("cv2")


def _registrada() -> bool:
    try:
        winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                       rf"SOFTWARE\Classes\CLSID\{cv_mod.CLSID_KONECTA}\InprocServer32")
        return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(not _registrada(), reason="DLL da câmera virtual não registrada")


def _abrir_konecta():
    for _ in range(20):
        nomes = cv_mod.listar_cameras()
        idx = next((i for i, n in enumerate(nomes) if n.startswith("KONECTA")), None)
        if idx is not None:
            return cv2.VideoCapture(idx, cv2.CAP_MSMF)
        time.sleep(0.5)
    pytest.fail(f"câmera KONECTA não apareceu: {nomes}")


def test_quadro_escrito_chega_e_some_quando_para():
    fundo = (90, 40, 10)  # BGR
    saida = cv_mod.SaidaDeQuadros()
    try:
        with cv_mod.CameraVirtual():
            cap = _abrir_konecta()
            ok, q = cap.read()
            assert ok and q.shape[:2] == (720, 1280), "a DLL nova entrega 16:9"

            quadro = np.full((720, 1280, 3), fundo, np.uint8)
            t0 = time.time()
            while time.time() - t0 < 4:
                saida.escrever(quadro)
                ok, q = cap.read()
            assert saida.conectada
            assert np.allclose(q[50:150, 50:150].reshape(-1, 3).mean(axis=0), fundo, atol=3)

            # sem quadros novos, volta ao aviso em ~2s em vez de congelar
            t1 = time.time()
            while time.time() - t1 < 3.5:
                ok, q = cap.read()
            assert not np.allclose(q[50:150, 50:150].reshape(-1, 3).mean(axis=0), fundo, atol=10)
            cap.release()
    finally:
        saida.fechar()
    assert not any(n.startswith("KONECTA") for n in cv_mod.listar_cameras()), "câmera não sumiu ao desligar"


def test_segunda_camera_explica_o_erro():
    with cv_mod.CameraVirtual():
        with pytest.raises(cv_mod.ErroCameraVirtual, match="outra câmera KONECTA"):
            cv_mod.CameraVirtual()


def test_konecta_manda_webcam_com_legenda_que_expira():
    """Pelo KONECTA de verdade: botão liga, quadro da webcam chega com a
    legenda do sinal confirmado, e a legenda some depois de _LEGENDA_S."""
    from unittest.mock import Mock, patch

    from PyQt6.QtWidgets import QApplication

    import app_central.main as main_module
    from app_central.main import KonectaIntelligenceHub

    _app = QApplication.instance() or QApplication([])
    with patch.object(main_module, "VideoCaptureWorker", Mock()), \
         patch.object(main_module, "listar_cameras", lambda: []):
        hub = KonectaIntelligenceHub()
    webcam = np.full((480, 640, 3), (60, 110, 60), np.uint8)  # BGR verde

    def quadro_final(segundos):
        t0 = time.time()
        while time.time() - t0 < segundos:
            hub._enviar_camera_virtual(webcam)
            ok, q = cap.read()
        return q

    try:
        hub._alternar_camera_virtual()
        assert hub._camera_virtual is not None
        assert "ligada" in hub.botao_camera_virtual.text()
        cap = _abrir_konecta()

        hub._sinal_exibido, hub._momento_confirmado = "não entendi", time.monotonic()
        q = quadro_final(2.5)
        centro = q[250:350, 560:720].reshape(-1, 3).mean(axis=0)
        faixa = q[600:670, 380:900].reshape(-1, 3).mean(axis=0)
        assert np.allclose(centro, (60, 110, 60), atol=6), "webcam não chegou"
        # faixa escura com texto branco: a média não é "escura", mas fica longe
        # do verde da webcam (medido: ~[108 117 108] contra [60 110 60])
        assert not np.allclose(faixa, (60, 110, 60), atol=15), f"legenda não apareceu: {faixa}"

        hub._momento_confirmado = time.monotonic() - hub._LEGENDA_S - 1
        q = quadro_final(1.5)
        faixa = q[600:670, 380:900].reshape(-1, 3).mean(axis=0)
        assert np.allclose(faixa, (60, 110, 60), atol=8), "legenda antiga continuou na imagem"
        cap.release()
    finally:
        hub._desligar_camera_virtual()
        loop = getattr(hub, "_loop", None)
        if loop and not loop.is_closed():
            loop.call_soon_threadsafe(loop.stop)
    assert "desligada" in hub.botao_camera_virtual.text()
    assert not any(n.startswith("KONECTA") for n in cv_mod.listar_cameras())
