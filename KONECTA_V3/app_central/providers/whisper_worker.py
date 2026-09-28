"""Processo isolado que transcreve áudio com faster-whisper.

Existe porque carregar o modelo dentro do processo da GUI derruba o app com
segmentation fault, de forma reprodutível, quando há captura de áudio e câmera
ativas. A causa exata não foi identificada — a pilha isolada (Qt + QThread +
soundcard + webrtcvad + ctranslate2 em executor) não reproduz o crash. O que se
sabe é que o mesmo modelo, no mesmo Python, roda estável quando fica sozinho num
processo: é assim que o TEXTO_PARA_LIBRAS opera.

Protocolo pela entrada/saída padrão, uma requisição por vez:

    entrada:  8 bytes com o nº de amostras (uint64) + amostras float32
    saída:    uma linha JSON {"texto": "..."} ou {"erro": "..."}

Modelo carregado uma vez, na primeira requisição.
"""

from __future__ import annotations

import json
import struct
import sys
import warnings
from pathlib import Path

import numpy as np

warnings.filterwarnings("ignore")

_modelo = None


def _carregar(nome: str, dispositivo: str, tipo: str):
    global _modelo
    if _modelo is None:
        from faster_whisper import WhisperModel

        try:
            _modelo = WhisperModel(nome, device=dispositivo, compute_type=tipo)
        except Exception:
            # máquina sem CUDA: cai para CPU em vez de deixar o app sem áudio
            _modelo = WhisperModel(nome, device="cpu", compute_type="int8")
    return _modelo


def main() -> int:
    nome = sys.argv[1] if len(sys.argv) > 1 else "small"
    # Instalado, o modelo vem dentro do instalador (_internal/whisper/<nome>):
    # sem isto a primeira fala baixaria ~460MB da internet, calada.
    embutido = Path(getattr(sys, "_MEIPASS", "")) / "whisper" / nome
    if getattr(sys, "frozen", False) and embutido.is_dir():
        nome = str(embutido)
    dispositivo = sys.argv[2] if len(sys.argv) > 2 else "cpu"
    tipo = sys.argv[3] if len(sys.argv) > 3 else "int8"
    idioma = sys.argv[4] if len(sys.argv) > 4 else "pt"

    entrada = sys.stdin.buffer
    saida = sys.stdout

    while True:
        cabecalho = entrada.read(8)
        if len(cabecalho) < 8:
            return 0  # pai fechou a entrada: encerra limpo

        (n_amostras,) = struct.unpack("<Q", cabecalho)
        dados = entrada.read(n_amostras * 4)
        if len(dados) < n_amostras * 4:
            return 0

        try:
            audio = np.frombuffer(dados, dtype=np.float32)
            modelo = _carregar(nome, dispositivo, tipo)
            segmentos, _info = modelo.transcribe(
                audio,
                language=idioma,
                beam_size=1,
                vad_filter=False,
                condition_on_previous_text=False,
            )
            resposta = {"texto": "".join(s.text for s in segmentos).strip()}
        except Exception as erro:
            resposta = {"erro": f"{type(erro).__name__}: {erro}"}

        # JSON só em ASCII ("M\u00e3e"): o stdout de um pipe no Windows
        # sai em cp1252 e o pai lê UTF-8. Com acento cru, "Mãe" e toda frase com
        # "não" se perdiam -- só funcionava quando quem abria o app tinha
        # PYTHONIOENCODING=utf-8, e o duplo clique no .bat não tem.
        saida.write(json.dumps(resposta) + "\n")
        saida.flush()


if __name__ == "__main__":
    sys.exit(main())
