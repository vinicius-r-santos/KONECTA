"""Testes dos workers de captura (vídeo e áudio).

Os testes evitam abrir hardware real: todas as dependências de captura são
substituídas por mocks. Nenhuma thread real é iniciada (métodos internos são
chamados diretamente).
"""

# pylint: disable=missing-function-docstring,protected-access,unused-argument,C1803,too-few-public-methods,unnecessary-lambda,no-member,no-name-in-module

import sys
from types import ModuleType
from unittest.mock import Mock, patch

import numpy as np
import pytest
from PyQt6.QtWidgets import QApplication

from app_central.utils.video_capture import (
    AUDIO_FRAMES_PER_BUFFER,
    FRAME_HEIGHT,
    FRAME_WIDTH,
    AudioCaptureWorker,
    VideoCaptureWorker,
)

_QAPP = QApplication.instance() or QApplication([])


# ════════════════════════════════════════════════════════════════
# VideoCaptureWorker
# ════════════════════════════════════════════════════════════════

def _fake_capture(reads):
    """Fake cv2.VideoCapture que entrega a lista de ``(ret, frame)``."""
    capture = Mock()
    capture.isOpened.return_value = True
    capture.read.side_effect = reads
    return capture


def test_open_camera_configures_props():
    worker = VideoCaptureWorker(camera_id=3, fps=15)
    capture = _fake_capture([])
    with patch(
        "app_central.utils.video_capture.cv2.VideoCapture", return_value=capture
    ):
        result = worker._open_camera()
    assert result is capture
    assert capture.set.call_count == 3
    set_args = [call.args[1] for call in capture.set.call_args_list]
    assert set_args == [FRAME_WIDTH, FRAME_HEIGHT, 15]


def test_open_camera_raises_when_not_opened():
    worker = VideoCaptureWorker()
    capture = Mock()
    capture.isOpened.return_value = False
    with patch(
        "app_central.utils.video_capture.cv2.VideoCapture", return_value=capture
    ):
        with pytest.raises(RuntimeError, match="Não conseguiu abrir câmera"):
            worker._open_camera()


def test_capture_loop_emits_frame_and_breaks_on_error():
    worker = VideoCaptureWorker()
    emitted = []
    worker.frame_ready.connect(lambda f: emitted.append(f))
    errors = []
    worker.error_occurred.connect(lambda e: errors.append(e))
    frame = np.zeros((FRAME_HEIGHT, FRAME_WIDTH, 3), dtype=np.uint8)
    worker.cap = _fake_capture([(True, frame), (False, None)])
    worker._capture_loop()
    assert len(emitted) == 1
    assert errors == ["Erro ao capturar frame"]


def test_capture_loop_respects_is_running():
    worker = VideoCaptureWorker()
    worker.is_running = False
    worker.cap = _fake_capture([(True, np.zeros((1, 1, 3), dtype=np.uint8))])
    emitted = []
    worker.frame_ready.connect(lambda f: emitted.append(f))
    worker._capture_loop()
    assert emitted == []


def test_capture_loop_without_cap_raises():
    worker = VideoCaptureWorker()
    with pytest.raises(RuntimeError, match="Câmera não inicializada"):
        worker._capture_loop()


def test_close_camera_releases():
    worker = VideoCaptureWorker()
    capture = _fake_capture([])
    worker.cap = capture
    worker._close_camera()
    capture.release.assert_called_once()
    assert worker.cap is None
    worker._close_camera()  # noop seguro


def test_run_emits_error_when_open_fails():
    worker = VideoCaptureWorker()
    errors = []
    worker.error_occurred.connect(lambda e: errors.append(e))
    with patch.object(worker, "_open_camera", side_effect=RuntimeError("sem câmera")):
        worker.run()
    assert errors == ["sem câmera"]


def test_stop_sets_flag():
    worker = VideoCaptureWorker()
    with patch.object(worker, "wait", return_value=None) as wait:
        worker.stop()
    assert worker.is_running is False
    wait.assert_called_once()


# ════════════════════════════════════════════════════════════════
# AudioCaptureWorker
# ════════════════════════════════════════════════════════════════

def _fake_pyaudio_module():
    """Módulo pyaudio fake (evita dependência real de hardware)."""
    module = ModuleType("pyaudio")
    module.paFloat32 = 1

    class _Stream:
        def __init__(self):
            self.closed = False
            self.stopped = False

        def read(self, frames, **kwargs):
            return b"\x00\x00\x00\x00" * frames

        def stop_stream(self):
            self.stopped = True

        def close(self):
            self.closed = True

    class _Audio:
        def __init__(self):
            self.stream = _Stream()
            self.terminated = False

        def open(self, **kwargs):
            return self.stream

        def terminate(self):
            self.terminated = True

    module.PyAudio = _Audio
    return module


def test_audio_open_stream_configures_input():
    worker = AudioCaptureWorker(sample_rate=8000)
    audio = _fake_pyaudio_module().PyAudio()
    stream = worker._open_stream(audio, _fake_pyaudio_module())
    assert stream is audio.stream


def test_audio_loop_emits_audio_ready():
    worker = AudioCaptureWorker()
    emitted = []
    worker.audio_ready.connect(lambda data: emitted.append(data))
    stream = Mock()
    data_bytes = b"\x00\x00\x00\x00" * 4

    def _read(*args, **kwargs):
        worker.is_running = False
        return data_bytes

    stream.read.side_effect = _read
    worker._audio_loop(stream)
    assert len(emitted) == 1
    assert emitted[0].dtype == np.float32
    assert emitted[0].shape == (4,)


def test_audio_loop_read_error_warns_and_continues():
    worker = AudioCaptureWorker()
    stream = Mock()
    calls = {"n": 0}
    data_bytes = b"\x00\x00\x00\x00" * 4

    def _read(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("dispositivo sumiu")
        worker.is_running = False
        return data_bytes

    stream.read.side_effect = _read
    worker._audio_loop(stream)  # erro é logado e o loop continua
    assert calls["n"] == 2


def test_audio_run_success_flow():
    module = _fake_pyaudio_module()
    worker = AudioCaptureWorker()
    worker.is_running = False  # evita loop infinito
    emitted = []
    worker.audio_ready.connect(lambda d: emitted.append(d))
    with patch.dict(sys.modules, {"pyaudio": module}):
        worker.run()
    assert worker._audio.terminated is True
    assert worker._audio.stream.closed is True
    assert worker._audio.stream.stopped is True


def test_audio_run_import_error_emits():
    worker = AudioCaptureWorker()
    errors = []
    worker.error_occurred.connect(lambda e: errors.append(e))
    # sys.modules["pyaudio"] = None força ImportError no import
    with patch.dict(sys.modules, {"pyaudio": None}):
        worker.run()
    assert errors == ["PyAudio não instalado"]


def test_audio_run_other_error_emits():
    module = _fake_pyaudio_module()

    class _Broken(module.PyAudio):
        def open(self, **kwargs):
            raise RuntimeError("mic bloqueado")

    worker = AudioCaptureWorker()
    errors = []
    worker.error_occurred.connect(lambda e: errors.append(e))
    with patch.dict(sys.modules, {"pyaudio": module}):
        module.PyAudio = _Broken
        worker.run()
    assert errors == ["mic bloqueado"]


def test_audio_stop_sets_flag():
    worker = AudioCaptureWorker()
    with patch.object(worker, "wait", return_value=None):
        worker.stop()
    assert worker.is_running is False


def test_constants():
    assert FRAME_WIDTH == 640
    assert FRAME_HEIGHT == 480
    assert AUDIO_FRAMES_PER_BUFFER == 4096
