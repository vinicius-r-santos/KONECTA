"""Libras → texto usando os modelos treinados pelo SIGNLAB.

Motivo de existir: o ``MotorKonectaV3`` monta 63 features cruas de uma mão só,
enquanto os modelos do SIGNLAB esperam 128 — duas mãos normalizadas mais dois
flags de presença. Os dois lados nunca conversariam. Este provider fala o
contrato do SIGNLAB, então os modelos já treinados passam a servir ao KONECTA.

Contrato replicado de ``SIGNLAB/vision/features.py`` e ``vision/hands.py``:

    vetor[0:63]    mão esquerda, xyz normalizado
    vetor[63:126]  mão direita, xyz normalizado
    vetor[126]     1.0 se a esquerda foi detectada
    vetor[127]     1.0 se a direita foi detectada

    normalização por mão: punho na origem, escala pela distância
    punho → base do dedo médio (MCP)

O código é replicado, não importado, para o KONECTA não depender do diretório do
SIGNLAB em tempo de execução. Em troca, ``test_signlab_sinais.py`` compara este
layout com o ``feature_config`` gravado dentro do modelo: se o SIGNLAB mudar a
extração, o teste acusa em vez de o reconhecimento degradar em silêncio.
"""

from __future__ import annotations

import asyncio
import collections
import logging
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np

from app_central.providers.base import (
    ProviderIndisponivel,
    ResultadoTexto,
    SinaisParaTextoProvider,
)
from app_central.providers.export_signlab import (
    ExportInvalido,
    ModeloSignlab,
    carregar_export,
)

logger = logging.getLogger(__name__)

TAMANHO_VETOR = 128
PONTOS_POR_MAO = 21
_PUNHO = 0
_MEDIO_MCP = 9


# Classe treinada com gestos que NÃO são sinal: coçar o rosto, digitar, ajeitar
# o cabelo, mãos paradas. Sem ela o modelo sempre escolhe um dos sinais quando
# há mão em quadro, e numa reunião qualquer gesto vira palavra na legenda.
CLASSE_NENHUM = "(nenhum)"


def e_nenhum(nome) -> bool:
    return str(nome).strip().casefold() == CLASSE_NENHUM


def sinais_visiveis(classes: Dict[Any, str]) -> List[str]:
    """Os sinais que a interface mostra e mede: todos menos "(nenhum)"."""
    return sorted(str(nome) for nome in classes.values() if not e_nenhum(nome))


def normalizar_mao(pontos) -> np.ndarray:
    """21 pontos [x,y,z] → vetor de 63 normalizado (punho na origem, escala MCP)."""
    arr = np.asarray(pontos, dtype=np.float32)
    arr = arr - arr[_PUNHO]
    escala = float(np.linalg.norm(arr[_MEDIO_MCP]))
    if escala > 1e-6:
        arr = arr / escala
    return arr.reshape(-1)


def montar_vetor(maos: Dict[str, Optional[List]]) -> Optional[np.ndarray]:
    """Monta as 128 features. ``None`` quando nenhuma mão foi detectada."""
    esquerda = maos.get("left_hand")
    direita = maos.get("right_hand")
    if not esquerda and not direita:
        return None

    vetor = np.zeros(TAMANHO_VETOR, dtype=np.float32)
    if esquerda:
        vetor[0:63] = normalizar_mao(esquerda)
        vetor[126] = 1.0
    if direita:
        vetor[63:126] = normalizar_mao(direita)
        vetor[127] = 1.0
    return vetor


class SinaisSignlab(SinaisParaTextoProvider):
    """Reconhecimento local com modelo do SIGNLAB (``.joblib``)."""

    nome = "signlab_local"

    def __init__(
        self,
        caminho_modelo: str,
        confianca_minima: float = 0.0,
        caminho_landmarker: str = "",
        passo_predicao: int = 3,
    ):
        self.caminho_modelo = Path(caminho_modelo)
        self.confianca_minima = confianca_minima
        self.caminho_landmarker = caminho_landmarker
        # Prever a cada frame com a janela cheia gastaria CPU à toa: a janela
        # muda pouco entre frames vizinhos. Prever a cada N mantém a resposta
        # viva sem saturar.
        self.passo_predicao = max(1, passo_predicao)
        self._janela: collections.deque = collections.deque(maxlen=30)
        self._desde_ultima = 0
        self._classificador: Any = None
        self._nomes: Dict[Any, str] = {}
        self._config_features: Dict[str, Any] = {}
        self._export: Optional[ModeloSignlab] = None
        self._detector: Any = None
        self._mp: Any = None
        self._relogio_video = time.monotonic()  # base do timestamp exigido pelo modo VIDEO
        self._lock = threading.Lock()  # o detector do MediaPipe não é thread-safe
        self._lock_processo = threading.Lock()
        #: ultimo par de maos detectado, para a UI desenhar o esqueleto
        self.ultimas_maos = {"left_hand": None, "right_hand": None}
        self._processo = None

    # ------------------------------------------------------------- modelo

    def _carregar(self) -> None:
        """Carrega o export do SIGNLAB (.zip, pasta, .joblib ou .keras).

        Modelo temporal NAO e' instanciado aqui: o Keras dele vive no
        sinais_worker, porque TensorFlow nao coexiste com PyQt5 neste processo.
        Aqui so' lemos o metadata para saber a janela e as classes.
        """
        if self._classificador is not None or self._export is not None:
            return

        try:
            # carregar_rede=False: se for temporal, a rede fica no worker
            self._export = carregar_export(self.caminho_modelo, carregar_rede=False)
        except ExportInvalido as erro:
            raise ProviderIndisponivel(str(erro)) from erro

        self._classificador = self._export.modelo
        self._nomes = self._export.classes
        self._config_features = self._export.feature_config
        logger.info(
            "Modelo SIGNLAB carregado: %s (%d sinais, %s)",
            Path(self._export.origem).name,
            len(self._nomes),
            "temporal" if self._export.temporal else "estático",
        )

    async def disponivel(self) -> bool:
        """Checagem barata: o artefato existe e tem cara de export do SIGNLAB?

        **Não carrega o modelo.** Carregar um temporal traz TensorFlow junto —
        segundos e centenas de MB — e esta checagem roda no arranque. O modelo
        é carregado na primeira predição de verdade.
        """
        try:
            return await asyncio.get_running_loop().run_in_executor(
                None, self._parece_valido
            )
        except Exception as erro:
            logger.warning("Provider SIGNLAB indisponível: %s", erro)
            return False

    def _parece_valido(self) -> bool:
        """Inspeciona o artefato sem instanciar rede nem classificador."""
        caminho = self.caminho_modelo
        if not caminho.exists():
            logger.warning("Modelo não encontrado: %s", caminho)
            return False

        if caminho.is_dir():
            return any(caminho.glob("*.joblib")) or any(caminho.glob("*.keras"))

        if caminho.suffix == ".zip":
            import zipfile

            try:
                with zipfile.ZipFile(caminho) as pacote:
                    nomes = pacote.namelist()
            except zipfile.BadZipFile:
                logger.warning("Zip inválido: %s", caminho.name)
                return False
            return any(n.endswith((".joblib", ".keras")) for n in nomes)

        return caminho.suffix in (".joblib", ".keras")

    # ------------------------------------------------------------- visão

    def _obter_detector(self):
        """Cria o HandLandmarker (API tasks do MediaPipe).

        A API antiga ``mp.solutions.hands`` não existe nas distribuições atuais
        do pacote — nesta máquina o mediapipe expõe apenas ``Image`` e ``tasks``.
        É a mesma API que o SIGNLAB usa, o que também mantém a extração idêntica
        à que gerou os modelos.
        """
        if self._detector is None:
            import mediapipe as mp
            from mediapipe.tasks.python import vision as mp_vision
            from mediapipe.tasks.python.core.base_options import BaseOptions

            caminho = self._caminho_landmarker()
            if caminho is None:
                raise ProviderIndisponivel(
                    "hand_landmarker.task não encontrado; sem ele não há extração de mãos"
                )
            self._mp = mp
            # VIDEO em vez de IMAGE: entre quadros vizinhos a mão quase não se
            # move, e neste modo o MediaPipe reaproveita o rastreamento em vez
            # de rodar o detector de palma inteiro a cada quadro — é a mesma
            # ideia do "passo_predicao", mas dentro do próprio MediaPipe.
            # Herda o ganho do rastreamento sem mudar o vetor de 128 features
            # nem os modelos já treinados: aqui só muda COMO a mão é achada.
            self._detector = mp_vision.HandLandmarker.create_from_options(
                mp_vision.HandLandmarkerOptions(
                    base_options=BaseOptions(model_asset_path=str(caminho)),
                    running_mode=mp_vision.RunningMode.VIDEO,
                    num_hands=2,
                    min_hand_detection_confidence=0.5,
                    min_tracking_confidence=0.5,
                )
            )
        return self._detector

    def _caminho_landmarker(self) -> Optional[Path]:
        """Procura o asset do MediaPipe nos lugares conhecidos do projeto."""
        if self.caminho_landmarker and Path(self.caminho_landmarker).is_file():
            return Path(self.caminho_landmarker)
        candidatos = [
            Path(__file__).resolve().parents[2] / "models" / "hand_landmarker.task",
            Path("C:/KONECTA/SIGNLAB/vision/models/hand_landmarker.task"),
            Path("C:/KONECTA/OCR/modelos/hand_landmarker.task"),
        ]
        for candidato in candidatos:
            if candidato.is_file():
                return candidato
        return None

    def _carregar_e_extrair(self, frame: np.ndarray) -> Dict[str, Optional[List]]:
        """Parte bloqueante, executada fora da thread do loop."""
        self._carregar()
        return self._extrair_maos(frame)

    def _extrair_maos(self, frame: np.ndarray) -> Dict[str, Optional[List]]:
        import cv2

        detector = self._obter_detector()
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        imagem = self._mp.Image(image_format=self._mp.ImageFormat.SRGB, data=rgb)
        with self._lock:  # o detector não é thread-safe
            # timestamp em ms desde a criação do detector: precisa ser
            # estritamente crescente (exigência do modo VIDEO), e um relógio
            # monotônico garante isso mesmo com quadros descartados pela
            # backpressure — nunca anda para trás.
            marca_ms = int((time.monotonic() - self._relogio_video) * 1000)
            resultado = detector.detect_for_video(imagem, marca_ms)

        maos: Dict[str, Optional[List]] = {"left_hand": None, "right_hand": None}
        for marcas, lado in zip(resultado.hand_landmarks, resultado.handedness):
            pontos = [[p.x, p.y, p.z] for p in marcas]
            chave = "left_hand" if lado[0].category_name == "Left" else "right_hand"
            # mesma regra do SIGNLAB: se o lado já está ocupado, usa o que sobrou
            if maos[chave] is None:
                maos[chave] = pontos
            elif maos["left_hand"] is None:
                maos["left_hand"] = pontos
            elif maos["right_hand"] is None:
                maos["right_hand"] = pontos

        # guarda para a UI desenhar o esqueleto: mostra à pessoa se o MediaPipe
        # está enxergando a mão dela, que é a causa nº 1 de "não reconheceu"
        self.ultimas_maos = maos
        return maos

    # ------------------------------------------------------------- uso

    async def reconhecer(self, frame: np.ndarray) -> ResultadoTexto:
        """Reconhece um frame.

        Com modelo **temporal**, um frame sozinho não diz nada: a rede foi
        treinada sobre uma janela de N frames. Em vez de exigir que quem chama
        saiba disso, acumulamos a janela aqui e devolvemos texto vazio enquanto
        ela não fecha. Assim o mesmo código de captura serve para sinal estático
        e dinâmico.
        """
        inicio = time.monotonic()
        try:
            await asyncio.get_running_loop().run_in_executor(None, self._carregar)
        except ProviderIndisponivel:
            raise
        except Exception as erro:
            raise ProviderIndisponivel(f"falha ao carregar modelo: {erro}") from erro

        if self._export is not None and self._export.temporal:
            return self._filtrar_nenhum(await self._reconhecer_temporal(frame, inicio))

        try:
            # joblib.load e MediaPipe seguram a thread por centenas de ms (o
            # primeiro detect() chega a segundos). Rodando no loop, o app fica
            # cego durante o arranque: medido, 9 de 10 frames descartados. Vai
            # para um executor.
            maos = await asyncio.get_running_loop().run_in_executor(
                None, self._carregar_e_extrair, frame
            )
            vetor = montar_vetor(maos)
        except ProviderIndisponivel:
            raise
        except Exception as erro:
            raise ProviderIndisponivel(f"falha ao extrair sinais: {erro}") from erro

        if vetor is None:
            # nenhuma mão no frame: resposta legítima, não erro
            return ResultadoTexto(
                texto="",
                confianca=0.0,
                latencia_ms=(time.monotonic() - inicio) * 1000,
                fonte=self.nome,
                detalhes={"status": "sem_maos"},
            )

        try:
            texto, confianca = self._prever(vetor)
        except Exception as erro:
            raise ProviderIndisponivel(f"falha na predição: {erro}") from erro

        if confianca < self.confianca_minima:
            texto = ""

        return self._filtrar_nenhum(ResultadoTexto(
            texto=texto,
            confianca=confianca,
            latencia_ms=(time.monotonic() - inicio) * 1000,
            fonte=self.nome,
            detalhes={
                "maos": sum(1 for v in maos.values() if v),
                "modelo": self.caminho_modelo.name,
            },
        ))

    @staticmethod
    def _filtrar_nenhum(resultado: ResultadoTexto) -> ResultadoTexto:
        """Predição de "(nenhum)" não é texto: é a pessoa não sinalizando."""
        if not e_nenhum(resultado.texto):
            return resultado
        return replace(resultado, texto="",
                       detalhes={**resultado.detalhes, "status": "sem_sinal"})

    def _prever(self, vetor: np.ndarray):
        entrada = vetor.reshape(1, -1)
        classe = self._classificador.predict(entrada)[0]
        confianca = 1.0
        if hasattr(self._classificador, "predict_proba"):
            confianca = float(np.max(self._classificador.predict_proba(entrada)[0]))
        nome = self._export.nome_da_classe(classe) if self._export else str(classe)
        return nome, confianca

    async def _reconhecer_temporal(self, frame: np.ndarray, inicio: float) -> ResultadoTexto:
        """Acumula a janela e prevê quando ela fecha.

        Enquanto a janela enche, devolve texto vazio — não é erro, é o sinal
        ainda acontecendo. Mão fora de quadro esvazia o buffer: o gesto seguinte
        não deve herdar o começo do anterior.
        """
        try:
            maos = await asyncio.get_running_loop().run_in_executor(
                None, self._extrair_maos, frame
            )
        except Exception as erro:
            raise ProviderIndisponivel(f"falha ao extrair sinais: {erro}") from erro

        vetor = montar_vetor(maos)
        if vetor is None:
            self._janela.clear()
            return ResultadoTexto(
                texto="",
                confianca=0.0,
                latencia_ms=(time.monotonic() - inicio) * 1000,
                fonte=self.nome,
                detalhes={"status": "sem_maos", "janela": 0},
            )

        tamanho = self._export.tamanho_sequencia or 30
        if self._janela.maxlen != tamanho:
            self._janela = collections.deque(self._janela, maxlen=tamanho)
        self._janela.append(vetor)

        # janela incompleta, ou ainda não é hora de prever
        self._desde_ultima += 1
        if len(self._janela) < tamanho or self._desde_ultima < self.passo_predicao:
            return ResultadoTexto(
                texto="",
                confianca=0.0,
                latencia_ms=(time.monotonic() - inicio) * 1000,
                fonte=self.nome,
                detalhes={"status": "acumulando", "janela": len(self._janela)},
            )

        self._desde_ultima = 0
        try:
            texto, confianca = await asyncio.get_running_loop().run_in_executor(
                None, self._prever_no_processo, np.stack(self._janela)
            )
        except Exception as erro:
            raise ProviderIndisponivel(f"falha na predição temporal: {erro}") from erro

        if confianca < self.confianca_minima:
            texto = ""

        return ResultadoTexto(
            texto=texto,
            confianca=confianca,
            latencia_ms=(time.monotonic() - inicio) * 1000,
            fonte=self.nome,
            detalhes={"temporal": True, "janela": tamanho},
        )

    # --------------------------------------------------- sinais dinâmicos

    async def reconhecer_sequencia(self, frames: List[np.ndarray]) -> ResultadoTexto:
        """Reconhece um sinal dinâmico a partir de uma sequência de frames.

        Sinal dinâmico não cabe num frame: o modelo temporal do SIGNLAB é
        treinado sobre uma janela de N frames, como o KONECTA V1 já fazia.
        Com modelo estático, cai no comportamento padrão (último frame).
        """
        if not frames:
            raise ProviderIndisponivel("sequência vazia")

        self._carregar()
        if self._export is None or not self._export.temporal:
            return await self.reconhecer(frames[-1])

        inicio = time.monotonic()
        try:
            sequencia = await asyncio.get_running_loop().run_in_executor(
                None, self._vetores_da_sequencia, frames
            )
        except Exception as erro:
            raise ProviderIndisponivel(f"falha ao extrair sequência: {erro}") from erro

        if sequencia is None:
            return ResultadoTexto(
                texto="",
                confianca=0.0,
                latencia_ms=(time.monotonic() - inicio) * 1000,
                fonte=self.nome,
                detalhes={"status": "sem_maos"},
            )

        try:
            texto, confianca = await asyncio.get_running_loop().run_in_executor(
                None, self._prever_sequencia, sequencia
            )
        except Exception as erro:
            raise ProviderIndisponivel(f"falha na predição temporal: {erro}") from erro

        if confianca < self.confianca_minima:
            texto = ""

        return ResultadoTexto(
            texto=texto,
            confianca=confianca,
            latencia_ms=(time.monotonic() - inicio) * 1000,
            fonte=self.nome,
            detalhes={"frames": len(frames), "temporal": True},
        )

    def _vetores_da_sequencia(self, frames: List[np.ndarray]) -> Optional[np.ndarray]:
        """Extrai um vetor por frame. ``None`` se nenhum frame tinha mãos."""
        self._carregar()
        vetores = []
        for frame in frames:
            vetor = montar_vetor(self._extrair_maos(frame))
            if vetor is not None:
                vetores.append(vetor)
        if not vetores:
            return None
        return np.stack(vetores)

    @staticmethod
    def _python_do_worker() -> str:
        """Interpretador que roda o worker temporal.

        Precisa ser um ambiente **com** TensorFlow — e o da GUI não pode ter,
        porque a simples presença do pacote faz o MediaPipe importá-lo e a DLL
        falhar sob PyQt5 (medido: app processa 0 frames com TF instalado, 655
        sem). Por isso a venv separada.
        """
        dedicada = Path(__file__).resolve().parents[2] / ".venv-temporal" / "Scripts" / "python.exe"
        if dedicada.is_file():
            return str(dedicada)
        logger.warning(
            "%s não existe; usando o Python da GUI, que provavelmente não tem "
            "TensorFlow. Crie a venv com: python -m venv .venv-temporal && "
            ".venv-temporal\\Scripts\\pip install keras tensorflow-cpu numpy",
            dedicada,
        )
        return sys.executable

    def _prever_no_processo(self, sequencia: np.ndarray):
        """Manda a janela para o worker e recebe a predição.

        O Keras vive lá porque TensorFlow e PyQt5 não coexistem neste processo
        (ver sinais_worker.py). Aqui só trafegam os vetores já extraídos.
        """
        import json
        import struct
        import subprocess

        with self._lock_processo:
            if self._processo is None or self._processo.poll() is not None:
                roteiro = Path(__file__).parent / "sinais_worker.py"
                self._processo = subprocess.Popen(
                    [self._python_do_worker(), "-u", str(roteiro), str(self.caminho_modelo)],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                logger.info("Worker de sinais dinâmicos iniciado (pid %s)", self._processo.pid)

            try:
                self._processo.stdin.write(struct.pack("<Q", len(sequencia)))
                self._processo.stdin.write(sequencia.astype(np.float32).tobytes())
                self._processo.stdin.flush()
                linha = self._processo.stdout.readline()
            except (BrokenPipeError, OSError) as erro:
                self._processo = None
                raise RuntimeError(f"worker de sinais caiu: {erro}") from erro

        if not linha:
            self._processo = None
            raise RuntimeError("worker de sinais encerrou sem responder")

        resposta = json.loads(linha.decode("utf-8"))
        if "erro" in resposta:
            raise RuntimeError(resposta["erro"])
        return resposta.get("texto", ""), float(resposta.get("confianca", 0.0))

    def _prever_sequencia(self, sequencia: np.ndarray):
        """Ajusta a janela ao tamanho que a rede espera e prevê.

        Repete o último frame quando falta e corta o excedente pelo fim — mesma
        estratégia do V1, que preserva o começo do gesto.
        """
        esperado = self._export.tamanho_sequencia or len(sequencia)
        if len(sequencia) < esperado:
            faltam = esperado - len(sequencia)
            sequencia = np.concatenate([sequencia, np.repeat(sequencia[-1:], faltam, axis=0)])
        elif len(sequencia) > esperado:
            sequencia = sequencia[-esperado:]

        entrada = sequencia.reshape(1, esperado, TAMANHO_VETOR)
        probabilidades = self._classificador.predict(entrada, verbose=0)[0]
        indice = int(np.argmax(probabilidades))
        return self._export.nome_da_classe(indice), float(probabilidades[indice])

    def liberar(self) -> None:
        """Fecha o detector e encerra o worker do Keras.

        Pega os dois locks de propósito: com uma predição em curso, espera ela
        terminar em vez de fechar o detector por baixo dela. Sem encerrar o
        worker, cada troca de modelo a quente deixava para trás um processo
        Python com o TensorFlow carregado.
        """
        with self._lock:
            detector, self._detector = self._detector, None
            if detector is not None:
                try:
                    detector.close()
                except Exception:
                    pass
        with self._lock_processo:
            processo, self._processo = self._processo, None
        if processo is not None and processo.poll() is None:
            try:
                processo.stdin.close()  # o worker sai limpo no EOF
                processo.wait(timeout=3)
            except Exception:
                processo.kill()
        self._classificador = None

    async def encerrar(self) -> None:
        self.liberar()
