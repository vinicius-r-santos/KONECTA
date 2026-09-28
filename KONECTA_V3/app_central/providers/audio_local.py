"""Áudio → texto rodando na própria máquina (faster-whisper).

Implementa ``AudioParaTextoProvider``. É a implementação disponível hoje; quando
o motor do time virar API, basta escrever outra classe com o mesmo contrato —
nada mais no KONECTA muda.

Duas decisões que vieram de medição, não de preferência:

- **Transcrever fora da thread do loop.** O Whisper segura a CPU por mais de um
  segundo por trecho. No loop, isso pararia o reconhecimento de Libras junto.
- **CPU/int8 por padrão.** Esta máquina não tem GPU NVIDIA; tentar ``cuda``
  primeiro só gastaria tempo para cair no mesmo lugar. Quem tiver GPU muda por
  configuração.
"""

from __future__ import annotations

import asyncio
import json
import logging
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

import numpy as np

from app_central.empacotado import congelado, executavel_irmao
from app_central.providers.base import (
    AudioParaTextoProvider,
    ProviderIndisponivel,
    ResultadoTexto,
)

logger = logging.getLogger(__name__)

TAXA_ESPERADA = 16000  # faster-whisper trabalha em 16kHz
DURACAO_MINIMA_S = 0.3  # abaixo disso é ruído, não fala


class AudioLocalWhisper(AudioParaTextoProvider):
    """Transcrição local com faster-whisper."""

    nome = "whisper_local"

    def __init__(
        self,
        modelo: str = "small",
        dispositivo: str = "cpu",
        tipo_computacao: str = "int8",
        idioma: str = "pt",
        usar_processo: bool = True,
    ):
        self.modelo = modelo
        self.dispositivo = dispositivo
        self.tipo_computacao = tipo_computacao
        self.idioma = idioma
        self.usar_processo = usar_processo
        self._whisper: Any = None
        self._processo: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()

    def _carregar(self) -> Any:
        """Carrega o modelo. Bloqueia: só chamar de dentro de um executor."""
        if self._whisper is None:
            from faster_whisper import WhisperModel

            logger.info(
                "Carregando faster-whisper '%s' (%s/%s)…",
                self.modelo,
                self.dispositivo,
                self.tipo_computacao,
            )
            try:
                self._whisper = WhisperModel(
                    self.modelo,
                    device=self.dispositivo,
                    compute_type=self.tipo_computacao,
                )
            except Exception as erro:
                # máquina sem CUDA cai para CPU em vez de deixar o app sem áudio
                if self.dispositivo != "cpu":
                    logger.warning("'%s' indisponível (%s); usando cpu/int8", self.dispositivo, erro)
                    self._whisper = WhisperModel(self.modelo, device="cpu", compute_type="int8")
                else:
                    raise
        return self._whisper

    async def disponivel(self) -> bool:
        """Checagem barata: confere que dá para transcrever, sem carregar nada.

        Não carrega o modelo de propósito. Baixar/instanciar o Whisper leva
        centenas de MB e vários segundos; fazer isso numa checagem de saúde
        travava o encerramento do app, porque o Python espera as threads do
        executor ao sair. O modelo é carregado na primeira transcrição de fato.
        """
        try:
            import faster_whisper  # noqa: F401

            return True
        except Exception as erro:
            logger.warning("Transcrição indisponível: %s", erro)
            return False

    async def transcrever(self, audio: np.ndarray, taxa_amostragem: int) -> ResultadoTexto:
        inicio = time.monotonic()

        if taxa_amostragem != TAXA_ESPERADA:
            raise ProviderIndisponivel(
                f"áudio deve estar em {TAXA_ESPERADA}Hz; veio {taxa_amostragem}Hz"
            )

        amostras = np.asarray(audio, dtype=np.float32).reshape(-1)
        if amostras.size < TAXA_ESPERADA * DURACAO_MINIMA_S:
            # trecho curto demais: silêncio ou ruído, não vale rodar o modelo
            return ResultadoTexto(
                texto="",
                confianca=0.0,
                latencia_ms=(time.monotonic() - inicio) * 1000,
                fonte=self.nome,
                detalhes={"status": "curto_demais"},
            )

        try:
            texto = await asyncio.get_running_loop().run_in_executor(
                None, self._transcrever_bloqueante, amostras
            )
        except Exception as erro:
            raise ProviderIndisponivel(f"falha ao transcrever: {erro}") from erro

        return ResultadoTexto(
            texto=texto,
            confianca=1.0 if texto else 0.0,
            latencia_ms=(time.monotonic() - inicio) * 1000,
            fonte=self.nome,
            detalhes={"duracao_audio_s": round(amostras.size / TAXA_ESPERADA, 2)},
        )

    def _transcrever_bloqueante(self, amostras: np.ndarray) -> str:
        # Sem processo isolado, o modelo pode ser usado direto — é o caminho que
        # os testes exercitam, injetando ``_whisper``.
        if self._whisper is not None or not self.usar_processo:
            modelo = self._carregar()
            segmentos, _info = modelo.transcribe(
                amostras,
                language=self.idioma,
                beam_size=1,  # beam pequeno = mais rápido, o que importa em tempo real
                vad_filter=False,  # a segmentação é feita antes, por quem captura
                condition_on_previous_text=False,  # não arrasta erro entre trechos
            )
            return "".join(s.text for s in segmentos).strip()
        return self._transcrever_no_processo(amostras)

    # ------------------------------------------------ processo isolado

    def _garantir_processo(self):
        """Sobe o worker se ele não estiver de pé (ou tiver morrido)."""
        if self._processo is not None and self._processo.poll() is None:
            return self._processo

        args = [self.modelo, self.dispositivo, self.tipo_computacao, self.idioma]
        if congelado():
            # instalado: mesmo binário do KONECTA.exe, isolado por processo à
            # toa — mas o build também gera whisper_worker.exe como irmão, e
            # reaproveitar o comando de sempre custaria mais que usar o pronto.
            exe = executavel_irmao("whisper_worker.exe")
            if exe is None:
                raise RuntimeError(
                    "whisper_worker.exe não encontrado ao lado do KONECTA.exe "
                    "— instalação incompleta"
                )
            comando = [str(exe), *args]
        else:
            roteiro = Path(__file__).parent / "whisper_worker.py"
            comando = [sys.executable, "-u", str(roteiro), *args]

        self._processo = subprocess.Popen(
            comando,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        logger.info("Worker de transcrição iniciado (pid %s)", self._processo.pid)
        return self._processo

    def _transcrever_no_processo(self, amostras: np.ndarray) -> str:
        with self._lock:  # uma requisição por vez: o protocolo é sequencial
            processo = self._garantir_processo()
            try:
                processo.stdin.write(struct.pack("<Q", len(amostras)))
                processo.stdin.write(amostras.astype(np.float32).tobytes())
                processo.stdin.flush()
                linha = processo.stdout.readline()
            except (BrokenPipeError, OSError) as erro:
                self._processo = None  # próxima chamada sobe outro
                raise RuntimeError(f"worker de transcrição caiu: {erro}") from erro

        if not linha:
            self._processo = None
            raise RuntimeError("worker de transcrição encerrou sem responder")

        resposta = json.loads(linha.decode("utf-8"))
        if "erro" in resposta:
            raise RuntimeError(resposta["erro"])
        return resposta.get("texto", "")

    async def encerrar(self) -> None:
        self._whisper = None
        processo, self._processo = self._processo, None
        if processo is not None and processo.poll() is None:
            try:
                processo.stdin.close()  # fecha a entrada: o worker sai sozinho
                processo.wait(timeout=3)
            except Exception:
                processo.kill()
