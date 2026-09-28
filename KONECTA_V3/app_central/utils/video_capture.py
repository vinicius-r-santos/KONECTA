"""Captura de vídeo e áudio da câmera em threads separadas."""

import logging
from typing import Any, Optional

import cv2
import numpy as np
from PyQt6.QtCore import QThread, pyqtSignal

logger = logging.getLogger(__name__)

FRAME_WIDTH = 640
FRAME_HEIGHT = 480
AUDIO_FRAMES_PER_BUFFER = 4096


class VideoCaptureWorker(QThread):
    """Thread separada para captura de vídeo da câmera."""

    frame_ready = pyqtSignal(np.ndarray)
    error_occurred = pyqtSignal(str)

    def __init__(self, camera_id: int = 0, fps: int = 30):
        super().__init__()
        self.camera_id = camera_id
        self.target_fps = fps
        self.is_running = True
        self.cap: Optional[cv2.VideoCapture] = None

    def run(self) -> None:
        """Executa o loop de captura até ser interrompido."""
        try:
            self.cap = self._open_camera()
            self._capture_loop()
        except Exception as error:
            logger.error("Erro na captura: %s", error)
            self.error_occurred.emit(str(error))
        finally:
            self._close_camera()

    def _open_camera(self) -> cv2.VideoCapture:
        """Abre a câmera e configura resolução/FPS alvo.

        Usa DirectShow no Windows: com o backend padrão (MSMF) a abertura chegou
        a levar **51 segundos** nesta máquina, e a troca de câmera parecia
        travamento. Com DSHOW é praticamente imediato.
        """
        import sys as _sys

        if _sys.platform == "win32":
            capture = cv2.VideoCapture(self.camera_id, cv2.CAP_DSHOW)
            if not capture.isOpened():
                capture.release()
                capture = cv2.VideoCapture(self.camera_id)  # último recurso
        else:
            capture = cv2.VideoCapture(self.camera_id)
        if not capture.isOpened():
            raise RuntimeError(f"Não conseguiu abrir câmera {self.camera_id}")

        capture.set(cv2.CAP_PROP_FRAME_WIDTH, FRAME_WIDTH)
        capture.set(cv2.CAP_PROP_FRAME_HEIGHT, FRAME_HEIGHT)
        capture.set(cv2.CAP_PROP_FPS, self.target_fps)

        logger.info("Câmera %s aberta", self.camera_id)
        return capture

    def _capture_loop(self) -> None:
        """Lê frames da câmera e emite o sinal ``frame_ready``."""
        if self.cap is None:
            raise RuntimeError("Câmera não inicializada")
        while self.is_running:
            ret, frame = self.cap.read()
            if not ret:
                self.error_occurred.emit("Erro ao capturar frame")
                break
            self.frame_ready.emit(frame)

    def _close_camera(self) -> None:
        """Libera o recurso da câmera, se aberto."""
        if self.cap is not None:
            self.cap.release()
            self.cap = None
        logger.info("Câmera fechada")

    def stop(self, espera_ms: int = 2000) -> None:
        """Pede parada e aguarda no máximo ``espera_ms``.

        Esperar sem limite congelava a janela: quando esta thread está presa em
        ``cap.read()`` de uma câmera que não responde, o ``wait()`` sem prazo
        segura a interface junto (medido: 51s de tela travada ao trocar de
        câmera). Com prazo, a UI segue e a thread termina sozinha.
        """
        self.is_running = False
        if not self.wait(espera_ms):
            logger.warning("Captura da câmera %s não parou no prazo", self.camera_id)


class AudioCaptureWorker(QThread):
    """Thread separada para captura de áudio (Libras multimodal)."""

    audio_ready = pyqtSignal(np.ndarray)
    error_occurred = pyqtSignal(str)

    def __init__(self, sample_rate: int = 16000):
        super().__init__()
        self.sample_rate = sample_rate
        self.is_running = True
        self._audio: Any = None

    def run(self) -> None:
        """Executa o loop de captura de áudio até ser interrompido."""
        try:
            import pyaudio  # dependência opcional importada sob demanda

            self._audio = pyaudio.PyAudio()
            stream = self._open_stream(self._audio, pyaudio)
            self._audio_loop(stream)
            stream.stop_stream()
            stream.close()
            self._audio.terminate()
        except ImportError:
            logger.error("PyAudio não instalado")
            self.error_occurred.emit("PyAudio não instalado")
        except Exception as error:
            logger.error("Erro na captura de áudio: %s", error)
            self.error_occurred.emit(str(error))

    def _open_stream(self, audio: Any, pyaudio: Any) -> Any:
        """Abre o stream de entrada de áudio."""
        stream = audio.open(
            format=pyaudio.paFloat32,
            channels=1,
            rate=self.sample_rate,
            input=True,
            frames_per_buffer=AUDIO_FRAMES_PER_BUFFER,
        )
        logger.info("Áudio iniciado (%s Hz)", self.sample_rate)
        return stream

    def _audio_loop(self, stream: Any) -> None:
        """Lê buffers de áudio e emite o sinal ``audio_ready``."""
        while self.is_running:
            try:
                data = stream.read(AUDIO_FRAMES_PER_BUFFER, exception_on_overflow=False)
                audio_data = np.frombuffer(data, dtype=np.float32)
                self.audio_ready.emit(audio_data)
            except Exception as error:
                logger.warning("Erro ao capturar áudio: %s", error)

    def stop(self) -> None:
        """Para a captura e aguarda a thread terminar."""
        self.is_running = False
        self.wait()
