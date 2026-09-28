"""Testes da janela principal (KonectaIntelligenceHub).

Rodam em modo headless (offscreen) e evitam abrir câmera/hardware real.
"""

# pylint: disable=missing-function-docstring,protected-access,unnecessary-lambda,no-member,no-name-in-module,C1803,wrong-import-position

import asyncio
import os
from unittest.mock import AsyncMock, Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtWidgets import QApplication

import app_central.main as main_module
from app_central.main import (
    HIGH_CONFIDENCE_COLOR,
    HISTORY_LIMIT,
    LOW_CONFIDENCE_COLOR,
    MEDIUM_CONFIDENCE_COLOR,
    KonectaIntelligenceHub,
)
from app_central.pipeline.recognizer_pipeline import PipelineResult

_QAPP = QApplication.instance() or QApplication([])


_hubs_criados = []


def _hub() -> KonectaIntelligenceHub:
    """Instancia a janela sem tocar em câmera real."""
    with patch.object(main_module, "VideoCaptureWorker", Mock()):
        hub = KonectaIntelligenceHub()
    _hubs_criados.append(hub)
    return hub


@pytest.fixture(autouse=True)
def _encerrar_hubs():
    """Para os loops criados por cada teste.

    Cada hub sobe um event loop numa thread propria. Sem isto, 20 testes deixam
    20 loops vivos e o processo do pytest nao encerra ao fim da suite — foi o
    que travava a execucao completa.
    """
    yield
    while _hubs_criados:
        hub = _hubs_criados.pop()
        _encerrar_loop(hub)


def _encerrar_loop(hub) -> None:
    """Cancela o que estiver pendente e so' entao para o loop.

    Parar sem cancelar deixa a checagem de motores presa num socket (ela tenta
    falar com a porta 8300, que nao esta no ar nos testes) e o processo do
    pytest nunca encerra.
    """
    loop = getattr(hub, "_loop", None)
    if loop is None or loop.is_closed():
        return

    def _cancelar_e_parar():
        for tarefa in asyncio.all_tasks(loop):
            tarefa.cancel()
        loop.stop()

    try:
        loop.call_soon_threadsafe(_cancelar_e_parar)
    except RuntimeError:
        return
    thread = getattr(hub, "_loop_thread", None)
    if thread is not None:
        thread.join(timeout=3)
    # Fechar libera os sockets pendentes do Proactor: parar sozinho nao basta,
    # e o processo do pytest fica preso neles no fim da suite.
    try:
        if not loop.is_closed():
            loop.close()
    except Exception:
        pass


def _pipeline_result(confidence=0.9, signal="OLA", latency=10.0):
    return PipelineResult(
        signal=signal,
        confidence=confidence,
        latency_ms=latency,
        confidence_level="high" if confidence > 0.85 else "medium",
        validated_by="ensemble",
        recommendation="accept",
        user_history=[],
    )


# ── configuração ────────────────────────────────────────────────

def test_load_config_falls_back_to_default(monkeypatch):
    monkeypatch.setattr(
        "builtins.open", Mock(side_effect=FileNotFoundError("sem config"))
    )
    hub = _hub()
    config = hub._load_config()
    assert config["app"]["name"] == "KONECTA Intelligence Hub"
    assert config["motors"]["konecta_v3"]["enabled"] is True


def test_get_default_config():
    config = KonectaIntelligenceHub._get_default_config()
    assert config["pipeline"]["target_latency_ms"] == 1000
    assert "claude_logic" in config["motors"]


def test_hub_initializes_ui_and_pipeline():
    hub = _hub()
    assert hub.signal_label is not None
    assert hub.history_display is not None
    assert hub.start_btn is not None
    assert hub.stop_btn is not None
    assert hub.pipeline is not None
    assert hub.metrics is not None
    assert hub.tray is not None


def test_hub_initializes_camera_worker():
    with patch.object(main_module, "VideoCaptureWorker", Mock()) as worker_cls:
        hub = KonectaIntelligenceHub()
        worker_cls.return_value.frame_ready.connect.assert_called_once()
        worker_cls.return_value.start.assert_called_once()
    assert hub.camera_worker is not None


def test_init_camera_handles_failure():
    with patch.object(
        main_module, "VideoCaptureWorker", Mock(side_effect=RuntimeError("sem câmera"))
    ):
        hub = KonectaIntelligenceHub()
    assert hub.camera_worker is None


# ── cores de confiança ──────────────────────────────────────────

@pytest.mark.parametrize(
    ("confidence", "color"),
    [
        (0.86, HIGH_CONFIDENCE_COLOR),
        (0.9, HIGH_CONFIDENCE_COLOR),
        (0.71, MEDIUM_CONFIDENCE_COLOR),
        (0.85, MEDIUM_CONFIDENCE_COLOR),
        (0.69, LOW_CONFIDENCE_COLOR),
        (0.5, LOW_CONFIDENCE_COLOR),
    ],
)
def test_confidence_color(confidence, color):
    assert KonectaIntelligenceHub._confidence_color(confidence) == color


# ── fluxo de UI ─────────────────────────────────────────────────

def test_on_recognition_updated_updates_labels():
    hub = _hub()
    result = _pipeline_result(confidence=0.92, signal="OLA", latency=42.0)
    hub._on_recognition_updated(result)
    assert "OLA" in hub.signal_label.text()
    assert "Confiança: 92%" in hub.confidence_label.text()
    assert hub.metrics.get_stats()["total_processed"] == 1


def test_latencia_mostra_fila_e_ia_separadas():
    """A latência sentida pelo usuário inclui a espera em fila, não só o pipeline."""
    hub = _hub()
    hub._on_latencia_medida({"fila_ms": 30.0, "ia_ms": 100.0, "total_ms": 130.0})
    texto = hub.latency_label.text()
    assert "130ms" in texto
    assert "fila 30" in texto
    assert "IA 100" in texto


def test_add_to_history_limits_lines():
    hub = _hub()
    for i in range(HISTORY_LIMIT + 5):
        hub._add_to_history(f"SIG{i % 3}", 0.9, 10.0)
    lines = [line for line in hub.history_display.toPlainText().split("\n") if line]
    assert len(lines) <= HISTORY_LIMIT + 1  # primeira linha é a nova entrada


def test_start_stop_recognition_toggle():
    hub = _hub()
    hub._start_recognition()
    assert hub.is_running is True
    assert hub.start_btn.isEnabled() is False
    assert hub.stop_btn.isEnabled() is True
    hub._stop_recognition()
    assert hub.is_running is False
    assert hub.start_btn.isEnabled() is True
    assert hub.stop_btn.isEnabled() is False
    assert hub.signal_label.text() == "Pausado"


def test_clear_history():
    hub = _hub()
    hub.history_display.setText("linha1\nlinha2")
    hub._clear_history()
    assert hub.history_display.toPlainText() == ""


def test_quit_closes_window():
    hub = _hub()
    with patch.object(hub, "close") as close:
        hub._quit()
    close.assert_called_once()


def test_on_metrics_updated_logs(caplog):
    hub = _hub()
    with caplog.at_level("INFO"):
        hub._on_metrics_updated({"total_processed": 5})
    assert any("total_processed" in r.message for r in caplog.records)


def test_close_event_stops_camera():
    hub = _hub()
    hub.camera_worker = Mock()
    event = Mock()
    hub.closeEvent(event)
    hub.camera_worker.stop.assert_called_once()
    event.accept.assert_called_once()


def test_show_normal_raises_and_activates():
    hub = _hub()
    with patch.object(hub, "raise_"), patch.object(hub, "activateWindow"):
        hub.show_normal()


# ── pipeline assíncrono ─────────────────────────────────────────

def test_run_pipeline_emits_result():
    """Sem provider configurado, o pipeline antigo continua sendo usado."""
    hub = _hub()
    result = _pipeline_result()
    hub.motores.sinais_para_texto = None  # força o caminho de compatibilidade
    hub.pipeline = Mock()
    hub.pipeline.process_frame = AsyncMock(return_value=result)
    emitted = []
    hub.recognition_updated.connect(lambda r: emitted.append(r))

    asyncio.run(hub._run_pipeline(None))

    hub.pipeline.process_frame.assert_awaited_once()
    assert emitted == [result]


def test_provider_tem_preferencia_sobre_o_pipeline_antigo():
    """Havendo provider, é ele quem reconhece — o pipeline vira fallback."""
    from app_central.providers.base import ResultadoTexto

    hub = _hub()
    hub.pipeline = Mock()
    hub.pipeline.process_frame = AsyncMock(return_value=_pipeline_result())

    class _Motor:
        nome = "teste"

        async def reconhecer(self, _frame):
            return ResultadoTexto(texto="OI", confianca=0.9, latencia_ms=1.0, fonte="teste")

    hub.motores.sinais_para_texto = _Motor()

    asyncio.run(hub._run_pipeline(None))

    hub.pipeline.process_frame.assert_not_awaited()


def test_sinal_so_vai_para_videochamada_apos_confirmado():
    """O hold do V1 evita mandar a mesma palavra a cada frame."""
    from app_central.providers.base import ResultadoTexto

    hub = _hub()
    enviados = []

    class _Videochamada:
        nome = "falsa"
        injecao_direta = True

        async def enviar_legenda(self, texto):
            enviados.append(texto)
            return True

    class _Motor:
        nome = "teste"

        async def reconhecer(self, _frame):
            return ResultadoTexto(texto="OI", confianca=0.95, latencia_ms=1.0, fonte="teste")

    hub.motores.sinais_para_texto = _Motor()
    hub.videochamada = _Videochamada()
    hub.estabilizador.tempo_hold_s = 0.0

    async def _cenario():
        for _ in range(5):
            await hub._run_pipeline(None)

    asyncio.run(_cenario())

    # confirmou uma vez; as repetições seguintes não reenviam
    assert enviados == ["OI"]


def test_run_pipeline_swallows_errors():
    hub = _hub()
    hub.pipeline = Mock()
    hub.pipeline.process_frame = AsyncMock(side_effect=RuntimeError("falhou"))
    asyncio.run(hub._run_pipeline(None))  # não deve lançar


def test_process_frame_guard_conditions():
    hub = _hub()
    hub.is_running = False
    created = []

    def _spy(fn):
        created.append(fn)
        return Mock()

    hub._process_frame(None)
    assert created == []

    hub.is_running = True
    hub.pipeline = None
    hub._process_frame(None)
    assert created == []


def test_process_frame_guarda_o_frame_para_o_consumidor():
    """_process_frame nao processa: guarda o frame mais recente (contrapressao).

    Marcamos _processando para o consumidor real (que roda na thread do loop)
    nao drenar a fila no meio do teste — sem isso a asercao corre contra ele.
    """
    hub = _hub()
    hub.is_running = True
    hub.pipeline = Mock()
    hub._processando = True

    hub._process_frame("frame!")

    pendente = hub._frame_pendente
    assert pendente is not None
    assert pendente[0] == "frame!"


def test_frame_novo_substitui_o_anterior_em_espera():
    """Frame velho nao tem valor numa legenda ao vivo: o novo toma o lugar."""
    hub = _hub()
    hub.is_running = True
    hub.pipeline = Mock()
    hub._processando = True  # cenario real: chega frame com o consumidor ocupado

    hub._process_frame("velho")
    hub._process_frame("novo")

    assert hub._frame_pendente[0] == "novo"
    assert hub.frames_descartados == 1


def test_consumidor_processa_o_pendente():
    hub = _hub()
    hub.is_running = True
    hub.pipeline = Mock()
    rodados = []

    async def _fake_run(frame, capturado_em=None):
        rodados.append(frame)

    hub._run_pipeline = _fake_run
    hub._process_frame("frame!")

    asyncio.run(hub._consumir_frames())

    assert rodados == ["frame!"]
    assert hub._frame_pendente is None
    assert hub._processando is False
