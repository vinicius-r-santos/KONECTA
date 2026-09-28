"""Troca de modelo a quente: o .zip que a publicação automática do SIGNLAB
larga em models/ entra sem reiniciar, e o worker do modelo antigo morre."""
import shutil
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

import app_central.main as main_module
from app_central.main import KonectaIntelligenceHub
from app_central.providers.signlab_sinais import SinaisSignlab

MODELOS = Path(__file__).resolve().parents[1] / "models"
ZIP = next(iter(sorted(MODELOS.glob("*.zip"))), None)
VENV_TEMPORAL = Path(__file__).resolve().parents[1] / ".venv-temporal" / "Scripts" / "python.exe"

pytestmark = pytest.mark.skipif(ZIP is None, reason="nenhum .zip em models/")


class _Antigo:
    liberado = False

    def liberar(self):
        _Antigo.liberado = True


def _janela_falsa(modelo_atual, forcado=False):
    chamadas = []
    return SimpleNamespace(
        _modelo_forcado=forcado,
        _modelo_signlab=modelo_atual,
        motores=SimpleNamespace(sinais_para_texto=_Antigo()),
        estabilizador=SimpleNamespace(sem_maos=lambda: chamadas.append("sem_maos")),
        _ajustar_hold_para_o_modelo=lambda: chamadas.append("hold"),
        _mostrar_vocabulario=lambda: chamadas.append("vocabulario"),
        tray=None,
    ), chamadas


def test_zip_novo_entra_e_o_antigo_e_liberado(tmp_path, monkeypatch):
    novo = tmp_path / "signlab_coleta_exp9_20260923-1500.zip"
    shutil.copy(ZIP, novo)
    monkeypatch.setattr(main_module, "descobrir_modelo", lambda: novo)
    janela, chamadas = _janela_falsa(ZIP)
    _Antigo.liberado = False

    KonectaIntelligenceHub._verificar_modelo_novo(janela)

    assert isinstance(janela.motores.sinais_para_texto, SinaisSignlab)
    assert janela._modelo_signlab == novo
    assert chamadas == ["sem_maos", "hold", "vocabulario"]
    for _ in range(50):  # liberar() roda numa thread
        if _Antigo.liberado:
            break
        time.sleep(0.02)
    assert _Antigo.liberado


def test_mesmo_modelo_ou_forcado_nao_troca(monkeypatch):
    monkeypatch.setattr(main_module, "descobrir_modelo", lambda: ZIP)
    janela, _ = _janela_falsa(ZIP)
    antigo = janela.motores.sinais_para_texto
    KonectaIntelligenceHub._verificar_modelo_novo(janela)
    assert janela.motores.sinais_para_texto is antigo

    monkeypatch.setattr(main_module, "descobrir_modelo", lambda: Path("outro.zip"))
    janela, _ = _janela_falsa(ZIP, forcado=True)
    antigo = janela.motores.sinais_para_texto
    KonectaIntelligenceHub._verificar_modelo_novo(janela)
    assert janela.motores.sinais_para_texto is antigo


def test_zip_quebrado_nao_derruba_nem_repete(tmp_path, monkeypatch):
    quebrado = tmp_path / "quebrado.zip"
    quebrado.write_bytes(b"nao e zip")
    monkeypatch.setattr(main_module, "descobrir_modelo", lambda: quebrado)
    janela, _ = _janela_falsa(ZIP)
    antigo = janela.motores.sinais_para_texto
    KonectaIntelligenceHub._verificar_modelo_novo(janela)
    assert janela.motores.sinais_para_texto is antigo
    assert janela._modelo_signlab == quebrado  # não tenta de novo a cada 10s


@pytest.mark.skipif(not VENV_TEMPORAL.exists(), reason="sem .venv-temporal")
def test_worker_com_acento_e_liberar_encerra_o_processo(monkeypatch):
    # O ambiente do duplo clique no .bat: sem isto o worker herdaria UTF-8 de
    # quem roda o teste e o bug do "Mãe" passaria despercebido.
    monkeypatch.delenv("PYTHONIOENCODING", raising=False)
    monkeypatch.delenv("PYTHONUTF8", raising=False)
    motor = SinaisSignlab(caminho_modelo=str(ZIP))
    motor._carregar()
    if not motor._export.temporal:
        pytest.skip("modelo estático não usa worker")
    nomes = set(motor._export.classes.values())
    # Uma janela de zeros cai em "Mãe" neste modelo; qualquer classe serve,
    # desde que a resposta volte decodificada.
    texto, _ = motor._prever_no_processo(np.zeros((30, 128), np.float32))
    assert texto in nomes
    processo = motor._processo
    assert processo is not None and processo.poll() is None

    motor.liberar()

    assert processo.poll() is not None, "worker do modelo antigo continuou vivo"
    assert motor._processo is None


def test_preaquecimento_sobe_o_worker_antes_do_primeiro_sinal():
    """O worker precisa estar de pé quando a 1a janela de verdade fechar."""
    from unittest.mock import Mock, patch

    from PyQt6.QtWidgets import QApplication

    import app_central.main as main_module
    from app_central.main import KonectaIntelligenceHub
    from app_central.providers.signlab_sinais import SinaisSignlab

    _app = QApplication.instance() or QApplication([])
    with patch.object(main_module, "VideoCaptureWorker", Mock()), \
         patch.object(main_module, "listar_cameras", lambda: []), \
         patch.dict("os.environ", {"KONECTA_MODELO_SIGNLAB": str(ZIP)}):
        hub = KonectaIntelligenceHub()
    motor = hub.motores.sinais_para_texto
    assert isinstance(motor, SinaisSignlab)

    for _ in range(100):  # o aquecimento roda numa thread; espera terminar
        if motor._processo is not None and motor._export is not None:
            break
        time.sleep(0.1)
    assert motor._processo is not None, "worker não subiu no pré-aquecimento"
    assert motor._processo.poll() is None, "worker subiu e já morreu"
    motor.liberar()
