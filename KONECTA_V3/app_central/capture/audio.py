"""Captura do áudio do computador, segmentada por detecção de voz.

Entrega ao resto do app trechos de fala já fechados — não um fluxo contínuo.
Transcrever fluxo contínuo desperdiça CPU em silêncio e produz frases cortadas
no meio; segmentar por VAD antes é o que torna a latência utilizável.

Captura o **loopback** (o que está tocando nas caixas/fone), não o microfone:
numa videochamada, quem o usuário surdo precisa entender é a pessoa do outro
lado, que chega pela saída de áudio. O áudio continua saindo normalmente — a
captura não intercepta nada.

Parâmetros herdados do que já funcionou em produção no TEXTO_PARA_LIBRAS.
"""

from __future__ import annotations

import collections
import logging
import time

import numpy as np
from PyQt6.QtCore import QThread, pyqtSignal

logger = logging.getLogger(__name__)

TAXA = 16000  # webrtcvad e faster-whisper trabalham em 16kHz
MS_FRAME = 30  # webrtcvad aceita 10, 20 ou 30ms
AMOSTRAS_FRAME = int(TAXA * MS_FRAME / 1000)

VAD_AGRESSIVIDADE = 2  # 0 permissivo … 3 rigoroso com ruído
MS_JANELA_INICIO = 300
MS_JANELA_FIM = 200
RAZAO_INICIO = 0.9  # fração de frames com voz para considerar que a fala começou
RAZAO_FIM = 0.7  # fração em silêncio para considerar que terminou

SEGUNDOS_MAX_TRECHO = 8  # fecha à força: frase longa demais atrasa demais
SEGUNDOS_MIN_TRECHO = 0.3  # abaixo disso é ruído, não fala


class CapturaAudioWorker(QThread):
    """Captura loopback e emite trechos de fala prontos para transcrever."""

    fala_detectada = pyqtSignal(np.ndarray)
    erro = pyqtSignal(str)
    dispositivo_mudou = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self._rodando = True

    def run(self) -> None:
        try:
            import soundcard as sc
            import webrtcvad
        except ImportError as erro:
            self.erro.emit(f"captura de áudio indisponível: {erro}")
            return

        vad = webrtcvad.Vad(VAD_AGRESSIVIDADE)

        while self._rodando:
            try:
                alto_falante = sc.default_speaker()
                microfone = sc.get_microphone(
                    id=str(alto_falante.name), include_loopback=True
                )
                self.dispositivo_mudou.emit(alto_falante.name)
                logger.info("Capturando loopback de: %s", alto_falante.name)
                self._laco(microfone, vad)
            except Exception as erro:
                if not self._rodando:
                    break
                # Fone Bluetooth derruba o stream quando o áudio fica ocioso ou o
                # perfil muda; reabrir também cobre o usuário trocar de saída.
                logger.warning("Stream de áudio caiu (%s); reabrindo em 1s", erro)
                time.sleep(1)

    def _laco(self, microfone, vad) -> None:
        anel_inicio = collections.deque(maxlen=MS_JANELA_INICIO // MS_FRAME)
        anel_fim = collections.deque(maxlen=MS_JANELA_FIM // MS_FRAME)
        em_fala = False
        trecho: list = []
        inicio_trecho = 0.0

        with microfone.recorder(samplerate=TAXA, channels=1) as gravador:
            while self._rodando:
                bloco = gravador.record(numframes=AMOSTRAS_FRAME)[:, 0]
                pcm = np.clip(bloco * 32768.0, -32768, 32767).astype(np.int16)
                tem_voz = vad.is_speech(pcm.tobytes(), TAXA)

                if not em_fala:
                    anel_inicio.append((bloco, tem_voz))
                    com_voz = sum(1 for _, v in anel_inicio if v)
                    if com_voz > RAZAO_INICIO * anel_inicio.maxlen:
                        em_fala = True
                        inicio_trecho = time.monotonic()
                        trecho = [b for b, _ in anel_inicio]
                        anel_inicio.clear()
                        anel_fim.clear()
                    continue

                trecho.append(bloco)
                anel_fim.append((bloco, tem_voz))
                em_silencio = sum(1 for _, v in anel_fim if not v)
                janela_cheia = len(anel_fim) == anel_fim.maxlen
                terminou = janela_cheia and em_silencio > RAZAO_FIM * anel_fim.maxlen

                if terminou or (time.monotonic() - inicio_trecho) > SEGUNDOS_MAX_TRECHO:
                    self._emitir(trecho)
                    trecho = []
                    em_fala = False
                    anel_inicio.clear()
                    anel_fim.clear()

    def _emitir(self, trecho: list) -> None:
        if not trecho:
            return
        audio = np.concatenate(trecho).astype(np.float32)
        if len(audio) >= TAXA * SEGUNDOS_MIN_TRECHO:
            self.fala_detectada.emit(audio)

    def stop(self) -> None:
        self._rodando = False
        self.wait(3000)
