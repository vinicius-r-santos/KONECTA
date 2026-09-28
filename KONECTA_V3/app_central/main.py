"""KONECTA Intelligence Hub - Aplicação principal.

Janela flutuante de reconhecimento de Libras em tempo real.
"""

import asyncio
import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Dict, Optional

# Garante que a raiz do projeto esteja no path para os imports de pacote
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# pylint: disable=wrong-import-position
# QtWebEngineWidgets EXIGE ser importado antes de existir um QApplication —
# senão o avatar embutido falha com "must be imported before a QCoreApplication
# instance is created". Por isso vem antes de tudo.
try:
    from PyQt6 import QtWebEngineWidgets  # noqa: F401
except ImportError:  # sem PyQt6-WebEngine o app roda sem o avatar embutido
    QtWebEngineWidgets = None  # type: ignore[assignment]

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QFont, QImage, QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QPushButton,
    QSystemTrayIcon,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

import numpy as np
import yaml

from app_central.capture.audio import TAXA as TAXA_AUDIO
from app_central.capture.audio import CapturaAudioWorker
from app_central.capture.cameras import listar_cameras
from app_central.capture.camera_virtual import (
    CameraVirtual, ErroCameraVirtual, SaidaDeQuadros, compor_quadro,
)
from app_central.core.avaliacao import Avaliacao
from app_central.core.config import Config
from app_central.core.estabilizador import Estabilizador
from app_central.core.sessao import Estado, GerenciadorSessao, rotulo
from app_central.empacotado import congelado, executavel_irmao, pasta_dados_do_usuario
from app_central.infra.resiliencia import mensagem_amigavel
from app_central.pipeline.recognizer_pipeline import PipelineResult, RecognizerPipeline
from app_central.providers.audio_local import AudioLocalWhisper
from app_central.providers.base import Motores
from app_central.providers.export_signlab import PASTA_MODELOS, descobrir_modelo
from app_central.providers.http_texto_sinais import TextoParaSinaisHTTP
from app_central.providers.local_sinais import SinaisLocais
from app_central.providers.signlab_sinais import SinaisSignlab, sinais_visiveis
from app_central.utils.metrics import MetricsCollector
from app_central.utils.video_capture import VideoCaptureWorker
from app_central.videocall.adaptadores import criar_adaptador
# pylint: enable=wrong-import-position

# Instalado, "Program Files" não é gravável por um usuário comum — escrever
# ali falhava em silêncio (ou ia parar na pasta virtualizada do Windows, sem
# ninguém saber onde procurar o log). Em desenvolvimento nada muda: continua
# a mesma pasta do projeto de sempre.
_PASTA_LOG = pasta_dados_do_usuario() / "logs"
_PASTA_LOG.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler(str(_PASTA_LOG / "app_central.log"), encoding="utf-8")],
)
logger = logging.getLogger(__name__)

HIGH_CONFIDENCE_COLOR = "#00ff00"  # Verde
MEDIUM_CONFIDENCE_COLOR = "#ffaa00"  # Laranja
LOW_CONFIDENCE_COLOR = "#ff0000"  # Vermelho
HISTORY_LIMIT = 10


class KonectaIntelligenceHub(QMainWindow):
    """Aplicação principal - Janela flutuante."""

    recognition_updated = pyqtSignal(PipelineResult)
    metrics_updated = pyqtSignal(dict)
    latencia_medida = pyqtSignal(dict)
    sessao_mudou = pyqtSignal(object)
    fala_transcrita = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.config = self._load_config()
        self.pipeline: RecognizerPipeline = None  # type: ignore[assignment]
        self.metrics = MetricsCollector()
        self.camera_worker = None
        self.audio_worker = None
        self.audio_label = None
        self.preview_label = None
        self.camera_combo = None
        self.candidato_label = None
        self.avatar_view = None
        self.vocabulario_label = None
        self.avaliacao = None
        self.avaliacao_label = None
        self.botao_avaliar = None
        self._avaliacao_inicio = 0.0
        # câmera "KONECTA" que o Meet e o Zoom enxergam (vcam/): webcam + legenda
        self.botao_camera_virtual = None
        self._camera_virtual = None
        self._saida_virtual = None
        self._momento_confirmado = 0.0
        # a palavra na tela so' muda quando um sinal e' CONFIRMADO
        self._sinal_exibido = ""
        self._confianca_exibida = 0.0
        self._ultimo_no_historico = None
        self.is_running = False

        # Contrapressão: só o frame mais recente interessa (ver _process_frame).
        # O lock protege estes três campos, tocados pela thread da câmera e pela
        # thread do loop asyncio.
        self._frame_lock = threading.Lock()
        self._frame_pendente: Optional[np.ndarray] = None
        self._processando = False
        self.frames_descartados = 0
        self.frames_processados = 0

        # Arquitetura nova: configuração central, estado de sessão observável e
        # motores atrás de contrato (ciclos 2 a 7).
        self.configuracao = Config.carregar(
            Path(__file__).parent / "config" / "config.yaml"
        )
        self.gerenciador = GerenciadorSessao()
        # Ajustáveis em sessão de teste sem editar código: modelo com poucas
        # classes costuma dar confiança baixa (medido 0,40–0,46 com 3 sinais),
        # e o limiar padrão rejeitaria tudo.
        self.estabilizador = Estabilizador(
            limiar_confianca=float(os.environ.get("KONECTA_LIMIAR", "0.75")),
            tempo_hold_s=float(os.environ.get("KONECTA_HOLD_S", "1.2")),
        )
        self.motores = Motores()
        self.videochamada = criar_adaptador("nenhum")

        # Qt não roda um event loop asyncio; mantemos um loop dedicado
        # numa thread separada para agendar process_frame sem bloquear a UI.
        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._loop_thread.start()

        # Widgets criados em _init_ui (declarados aqui para clareza/tipagem)
        self.signal_label: QLabel = None  # type: ignore[assignment]
        self.confidence_label: QLabel = None  # type: ignore[assignment]
        self.latency_label: QLabel = None  # type: ignore[assignment]
        self.history_display: QTextEdit = None  # type: ignore[assignment]
        self.start_btn: QPushButton = None  # type: ignore[assignment]
        self.stop_btn: QPushButton = None  # type: ignore[assignment]
        self.motors_display: QLabel = None  # type: ignore[assignment]
        self.tray: QSystemTrayIcon = None  # type: ignore[assignment]

        # sondar dispositivos antes da UI: o seletor precisa da lista pronta
        self._cameras = listar_cameras()

        logger.info("Iniciando KONECTA Intelligence Hub...")

        self._init_ui()
        self._init_pipeline()
        self._init_motores()
        self._init_camera()
        self._init_captura_audio()
        self._setup_tray()
        self.gerenciador.observar(self._on_sessao_mudou)

        # A publicação automática do SIGNLAB larga um .zip novo em models/
        # quando o app de gravação recebe sinais; aqui ele entra sem reiniciar.
        self._timer_modelo = QTimer(self)
        self._timer_modelo.timeout.connect(self._verificar_modelo_novo)
        self._timer_modelo.start(10_000)

        # O atalho que abre tudo (C:\KONECTA\KONECTA.bat) já liga a câmera
        # virtual: um clique a menos antes da reunião.
        if os.environ.get("KONECTA_CAMERA_VIRTUAL") == "1":
            QTimer.singleShot(0, self._alternar_camera_virtual)

    def _load_config(self) -> Dict:
        """Carrega a configuração do arquivo ``config/config.yaml``."""
        config_path = Path(__file__).parent / "config" / "config.yaml"
        try:
            with open(config_path, "r", encoding="utf-8") as config_file:
                return yaml.safe_load(config_file)
        except Exception as error:
            logger.warning("Erro ao carregar config.yaml: %s", error)
            return self._get_default_config()

    @staticmethod
    def _get_default_config() -> Dict:
        """Retorna a configuração padrão quando o arquivo está ausente."""
        return {
            "app": {
                "name": "KONECTA Intelligence Hub",
                "window": {"width": 500, "height": 400},
            },
            "motors": {
                "konecta_v3": {"enabled": True},
                "claude_logic": {"enabled": True},
            },
            "pipeline": {"target_latency_ms": 1000},
        }

    def _init_pipeline(self) -> None:
        """Inicializa o pipeline de reconhecimento com a configuração carregada."""
        try:
            config = {
                "konecta_model_path": "models/v1",
                "claude_api_key": os.getenv("ANTHROPIC_API_KEY"),
                "claude_model": "claude-3-5-sonnet-20241022",
            }
            self.pipeline = RecognizerPipeline(config)
            logger.info("Pipeline iniciado")
        except Exception as error:
            logger.error("Erro ao inicializar pipeline: %s", error)

    def _preaquecer_modelo(self, motor) -> None:
        """Sobe o worker do Keras em segundo plano, antes do primeiro sinal de verdade.

        Medido: o worker leva ~8,5s para subir na primeira predição (import do
        TensorFlow + montar o modelo); depois disso cada predição custa ~75ms.
        Sem isto, os 8,5s caíam inteiros em cima do primeiro sinal que a pessoa
        fizesse — ~2s para a janela de 30 quadros encher, MAIS ~8,5s do worker
        subindo, quase 11s parado antes da primeira palavra. Rodar isto agora,
        em paralelo com a câmera abrindo, esconde esse tempo: quando a primeira
        janela de verdade fechar, o worker já está de pé.
        """
        def _aquecer():
            try:
                # _carregar() só lê metadados (rápido); é depois dela que dá
                # para saber se o modelo é temporal (tem worker) ou estático.
                motor._carregar()
                if motor._export is None or not motor._export.temporal:
                    return  # estático (.joblib): não tem worker para aquecer
                tamanho = motor._export.tamanho_sequencia or 30
                motor._prever_no_processo(np.zeros((tamanho, 128), dtype=np.float32))
                logger.info("Worker de sinais dinâmicos pré-aquecido")
            except Exception as erro:
                # o app segue funcionando: a primeira predição de verdade
                # simplesmente sobe o worker na hora, como antes
                logger.warning("Pré-aquecimento do worker falhou: %s", erro)

        threading.Thread(target=_aquecer, name="preaquecer-worker", daemon=True).start()

    def _garantir_avatar_no_ar(self) -> None:
        """Sobe o servidor do avatar sozinho, se instalado e ainda não estiver de pé.

        Em desenvolvimento não faz nada: quem roda do código já sabe subir o
        servidor à parte (ou o KONECTA.pyw sobe por você). No instalador não
        existe essa etapa manual — o clique no atalho precisa bastar sozinho,
        então o próprio KONECTA.exe verifica e, se faltar, liga o
        ``avatar_server.exe`` irmão.
        """
        if not congelado():
            return
        import socket
        import subprocess

        url = self.configuracao.texto_para_sinais.url
        host_porta = url.split("//", 1)[-1].split("/", 1)[0]
        host, _, porta = host_porta.partition(":")
        try:
            with socket.create_connection((host or "127.0.0.1", int(porta or 80)), timeout=0.3):
                return  # já está no ar
        except OSError:
            pass

        exe = executavel_irmao("avatar_server.exe")
        if exe is None:
            logger.warning("avatar_server.exe não encontrado ao lado do KONECTA.exe")
            return
        subprocess.Popen(
            [str(exe)], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        logger.info("Servidor do avatar iniciado (%s)", exe)

    def _init_motores(self) -> None:
        """Monta os motores a partir da configuração (§7).

        Escolha do motor de Libras: se houver modelo do SIGNLAB configurado,
        usa-o — é o único caminho que hoje reconhece de verdade nesta máquina
        (ver ANALISE_E_PLANO_GAUNTLET.md, ciclo 9). Sem ele, cai no motor
        embutido, que exige ``models/v1``.
        """
        self._garantir_avatar_no_ar()
        # Ordem: variável de ambiente (útil para testar um modelo específico),
        # depois o que estiver em models/. O caminho normal é o segundo: exportar
        # no SIGNLAB e largar o arquivo lá.
        caminho_signlab = os.environ.get("KONECTA_MODELO_SIGNLAB", "")
        # Modelo escolhido à mão pela variável: a troca a quente não mexe nele.
        self._modelo_forcado = bool(caminho_signlab)
        self._modelo_signlab = None
        if not caminho_signlab:
            descoberto = descobrir_modelo()
            if descoberto is not None:
                caminho_signlab = str(descoberto)
                logger.info("Modelo do SIGNLAB encontrado em models/: %s", descoberto.name)

        try:
            if caminho_signlab:
                self.motores.sinais_para_texto = SinaisSignlab(caminho_modelo=caminho_signlab)
                self._modelo_signlab = Path(caminho_signlab)
                self._preaquecer_modelo(self.motores.sinais_para_texto)
            else:
                logger.warning(
                    "Nenhum modelo em %s — exporte um experimento no SIGNLAB e "
                    "largue o .zip nessa pasta",
                    PASTA_MODELOS,
                )
                self.motores.sinais_para_texto = SinaisLocais(
                    caminho_modelo=self.configuracao.caminho_modelo
                )
        except Exception as erro:
            logger.error("Motor de Libras indisponível: %s", erro)

        try:
            if self.configuracao.audio_para_texto.ativo:
                self.motores.audio_para_texto = AudioLocalWhisper(
                    idioma=self.configuracao.captura.idioma.split("-")[0]
                )
        except Exception as erro:
            logger.error("Motor de áudio indisponível: %s", erro)

        try:
            if self.configuracao.texto_para_sinais.ativo:
                self.motores.texto_para_sinais = TextoParaSinaisHTTP(
                    url_base=self.configuracao.texto_para_sinais.url,
                    timeout_s=self.configuracao.texto_para_sinais.timeout_s,
                    tentativas=self.configuracao.texto_para_sinais.tentativas,
                )
        except Exception as erro:
            logger.error("Motor texto→Libras indisponível: %s", erro)

        self.videochamada = criar_adaptador(
            os.environ.get("KONECTA_PLATAFORMA", "nenhum"),
            os.environ.get("KONECTA_URL_LEGENDA", ""),
        )
        if not self.videochamada.injecao_direta:
            logger.info(getattr(self.videochamada, "LIMITACAO", ""))

        self._ajustar_hold_para_o_modelo()
        self._mostrar_vocabulario()

        self.gerenciador.atualizar(
            motor_sinais=Estado.ATIVO if self.motores.sinais_para_texto else Estado.DESLIGADO,
            motor_audio=Estado.ATIVO if self.motores.audio_para_texto else Estado.DESLIGADO,
            motor_texto_sinais=(
                Estado.ATIVO if self.motores.texto_para_sinais else Estado.DESLIGADO
            ),
        )
        asyncio.run_coroutine_threadsafe(self._checar_motores(), self._loop)

    def _ajustar_hold_para_o_modelo(self) -> None:
        """Encurta o hold quando o modelo é temporal.

        O hold existe para modelos estáticos, onde cada frame é uma decisão
        independente e é preciso ver o mesmo sinal repetido antes de acreditar.
        Um modelo temporal já decide sobre uma janela de 30 frames (~2s): exigir
        mais 0,8s de repetição em cima disso rejeitava predições de **100% de
        confiança**, porque o sinal simplesmente não dura tanto.

        Medido na sessão: 'mae' e 'filha' com 100%, ambos descartados.
        """
        if os.environ.get("KONECTA_HOLD_S"):
            return  # respeitamos o ajuste manual de quem está testando

        motor = self.motores.sinais_para_texto
        export = getattr(motor, "_export", None)
        if export is None:
            try:
                motor._carregar()  # type: ignore[union-attr]
                export = getattr(motor, "_export", None)
            except Exception:
                return

        if export is not None and export.temporal:
            self.estabilizador.tempo_hold_s = 0.25
            logger.info(
                "Modelo temporal: hold reduzido para %.2fs (a janela de %s frames "
                "já dá a estabilidade)",
                self.estabilizador.tempo_hold_s,
                export.tamanho_sequencia,
            )

    async def _checar_motores(self) -> None:
        """Confere de verdade se cada motor responde, em vez de supor (§11)."""
        for campo, provider in (
            ("motor_sinais", self.motores.sinais_para_texto),
            ("motor_audio", self.motores.audio_para_texto),
            ("motor_texto_sinais", self.motores.texto_para_sinais),
        ):
            if provider is None:
                continue
            try:
                ok = await provider.disponivel()
            except Exception:
                ok = False
            self.gerenciador.atualizar(**{campo: Estado.ATIVO if ok else Estado.ERRO})

    def _init_camera(self, indice: int = None) -> None:
        """Inicializa o worker de captura no dispositivo escolhido."""
        if indice is None:
            indice = self._cameras[0].indice if self._cameras else 0
        try:
            self.camera_worker = VideoCaptureWorker(camera_id=indice)
            self.camera_worker.frame_ready.connect(self._process_frame)
            self.camera_worker.frame_ready.connect(self._mostrar_preview)
            self.camera_worker.frame_ready.connect(self._enviar_camera_virtual)
            self.camera_worker.start()
            self.is_running = True
            logger.info("Câmera iniciada")
            self.gerenciador.atualizar(camera=Estado.ATIVO)
        except Exception as error:
            logger.error("Erro ao inicializar câmera: %s", error)
            self.gerenciador.atualizar(camera=Estado.ERRO, ultimo_erro=str(error))

    def _init_ui(self) -> None:
        """Cria a interface gráfica."""
        self.setWindowTitle("KONECTA_V3")
        self.setGeometry(80, 80, 1040, 640)
        # Barra de título nativa: traz minimizar, maximizar e fechar prontos, e
        # é por onde se arrasta. Sem moldura, a janela não tinha esses botões e
        # precisava de código próprio para ser movida.
        self.setWindowFlags(
            Qt.WindowType.Window  # type: ignore[attr-defined]
            | Qt.WindowType.WindowStaysOnTopHint  # continua flutuando sobre a videochamada
            | Qt.WindowType.WindowMinimizeButtonHint
            | Qt.WindowType.WindowMaximizeButtonHint
            | Qt.WindowType.WindowCloseButtonHint
        )

        central_widget = QWidget()
        self.setCentralWidget(central_widget)

        layout = QVBoxLayout(central_widget)
        layout.setContentsMargins(15, 15, 15, 15)
        layout.setSpacing(10)

        layout.addLayout(self._create_header_layout())

        # Os dois sentidos da conversa lado a lado, na mesma janela:
        # esquerda = o surdo sinaliza e vira texto; direita = a fala do ouvinte
        # vira Libras no avatar.
        colunas = QHBoxLayout()
        colunas.addLayout(self._criar_coluna_sinais(), stretch=1)
        colunas.addWidget(self._criar_coluna_avatar(), stretch=1)
        layout.addLayout(colunas, stretch=1)
        layout.addLayout(self._create_stats_layout())
        layout.addWidget(QLabel("Histórico:"))
        layout.addWidget(self._create_history_display())
        layout.addLayout(self._create_controls_layout())
        layout.addLayout(self._create_motors_section())
        layout.addStretch()

        self.recognition_updated.connect(self._on_recognition_updated)
        self.metrics_updated.connect(self._on_metrics_updated)
        self.latencia_medida.connect(self._on_latencia_medida)
        self.sessao_mudou.connect(self._aplicar_sessao_na_ui)
        self.fala_transcrita.connect(self._on_fala_transcrita)

    def _create_header_layout(self) -> QHBoxLayout:
        """Cria o cabeçalho com título e status."""
        header_layout = QHBoxLayout()

        title_label = QLabel("Reconhecimento de Sinais")
        title_font = QFont()
        title_font.setPointSize(12)
        title_font.setBold(True)
        title_label.setFont(title_font)

        status_label = QLabel("🟢 Ativo")
        status_label.setStyleSheet("color: #00ff00; font-weight: bold;")

        header_layout.addWidget(title_label)
        header_layout.addStretch()
        header_layout.addWidget(status_label)
        return header_layout

    def _criar_coluna_sinais(self) -> QVBoxLayout:
        """Coluna da esquerda: câmera, sinal reconhecido, histórico."""
        coluna = QVBoxLayout()
        titulo = QLabel("SINAIS → TEXTO")
        titulo.setStyleSheet("font-size: 10px; font-weight: bold; color: #555;")
        coluna.addWidget(titulo)
        coluna.addLayout(self._criar_seletor_camera())
        coluna.addWidget(self._criar_painel_avaliacao())
        coluna.addWidget(self._criar_preview())
        coluna.addWidget(self._create_signal_label())
        coluna.addWidget(self._criar_label_candidato())
        coluna.addWidget(self._criar_label_vocabulario())
        return coluna


    # ─────────────────────────────────────────────────────────────
    # Medição de acurácia: alvo sorteado antes, gabarito garantido
    # ─────────────────────────────────────────────────────────────

    def _criar_painel_avaliacao(self) -> QLabel:
        """Mostra qual sinal fazer durante uma medição."""
        self.avaliacao_label = QLabel("")
        self.avaliacao_label.setAlignment(Qt.AlignmentFlag.AlignCenter)  # type: ignore[attr-defined]
        self.avaliacao_label.setWordWrap(True)
        self.avaliacao_label.setStyleSheet(
            "background: #2d3a8c; color: white; border-radius: 6px;"
            "padding: 8px; font-size: 15px; font-weight: bold;"
        )
        self.avaliacao_label.hide()
        return self.avaliacao_label

    def _alternar_avaliacao(self) -> None:
        if self.avaliacao is not None:
            self._encerrar_avaliacao()
            return

        motor = self.motores.sinais_para_texto
        export = getattr(motor, "_export", None)
        if export is None or not export.classes:
            self.avaliacao_label.setText("Sem modelo carregado — não há o que medir.")
            self.avaliacao_label.show()
            return

        # sem "(nenhum)": ela nunca é confirmada, e a rodada que a sorteasse
        # nunca fecharia
        vocabulario = sinais_visiveis(export.classes)
        rodadas = int(os.environ.get("KONECTA_RODADAS", "20"))
        self.avaliacao = Avaliacao(vocabulario=vocabulario, rodadas_alvo=rodadas)
        self.botao_avaliar.setText("Encerrar medição")
        self.avaliacao_label.show()
        self._proxima_rodada()
        logger.info("Medição iniciada: %s rodadas, %s sinais", rodadas, len(vocabulario))

    def _proxima_rodada(self) -> None:
        alvo = self.avaliacao.proximo_alvo()
        if alvo is None:
            self._encerrar_avaliacao()
            return
        # o estabilizador precisa esquecer o sinal anterior, senão a repetição
        # do mesmo alvo seria bloqueada por evitar_repeticao
        self.estabilizador.sem_maos()
        self._avaliacao_inicio = time.monotonic()
        self.avaliacao_label.setText(
            f"Faça o sinal:  {alvo.upper()}" + chr(10) +
            f"rodada {self.avaliacao.numero_da_rodada} de {self.avaliacao.rodadas_alvo}"
            f"   ·   acertos até aqui: {self.avaliacao.acertos}"
        )

    def _registrar_rodada(self, reconhecido: str, confianca: float) -> None:
        """Chamado quando um sinal é confirmado durante a medição."""
        rodada = self.avaliacao.registrar(
            reconhecido, confianca, time.monotonic() - self._avaliacao_inicio
        )
        logger.info(
            "medição: alvo=%s reconhecido=%s %s",
            rodada.alvo,
            rodada.reconhecido,
            "ACERTOU" if rodada.acertou else "errou",
        )
        self._proxima_rodada()

    def _encerrar_avaliacao(self) -> None:
        avaliacao, self.avaliacao = self.avaliacao, None
        self.botao_avaliar.setText("Medir acurácia")
        if avaliacao is None or not avaliacao.rodadas:
            self.avaliacao_label.hide()
            return

        try:
            caminho = avaliacao.salvar_csv(Path(__file__).parent.parent / "avaliacoes")
            onde = chr(10) + f"salvo em {caminho.name}"
        except Exception as erro:
            logger.warning("Não foi possível salvar o CSV: %s", erro)
            onde = ""

        self.avaliacao_label.setText(avaliacao.resumo().replace(chr(10), "   ") + onde)
        self.history_display.setText(avaliacao.resumo())
        logger.info("Medição encerrada — %s", avaliacao.resumo().replace(chr(10), " | "))

    def _criar_label_vocabulario(self) -> QLabel:
        """Lista os sinais que o modelo conhece.

        Sem isto a pessoa não tem como saber o que tentar — e um sinal fora do
        vocabulário é indistinguível de uma falha do sistema.
        """
        self.vocabulario_label = QLabel("carregando vocabulário…")
        self.vocabulario_label.setWordWrap(True)
        self.vocabulario_label.setStyleSheet(
            "color: #666; font-size: 10px; background: #f4f4f4;"
            "border-radius: 4px; padding: 4px;"
        )
        return self.vocabulario_label

    def _verificar_modelo_novo(self) -> None:
        """Troca o modelo sem reiniciar quando um .zip mais novo aparece em models/.

        É o fim da linha da publicação automática: a gravação chega pelo app
        do celular, o SIGNLAB treina e larga o .zip aqui.
        """
        if self._modelo_forcado:
            return
        novo = descobrir_modelo()
        if novo is None or novo == self._modelo_signlab:
            return
        try:
            motor = SinaisSignlab(caminho_modelo=str(novo))
            # O construtor é preguiçoso e aceitaria um .zip corrompido; carregar
            # antes de trocar mantém o modelo que funciona se o novo for ruim.
            motor._carregar()
        except Exception as erro:
            logger.error("Modelo novo em models/ recusado (%s): %s", novo.name, erro)
            self._modelo_signlab = novo  # não tentar o mesmo arquivo a cada 10s
            return

        antigo, self.motores.sinais_para_texto = self.motores.sinais_para_texto, motor
        self._modelo_signlab = novo
        self.estabilizador.sem_maos()  # candidato do modelo antigo não vale no novo
        self._ajustar_hold_para_o_modelo()
        self._mostrar_vocabulario()
        logger.info("Modelo trocado a quente: %s", novo.name)
        if self.tray is not None:
            sinais = len(getattr(getattr(motor, "_export", None), "classes", None) or {})
            self.tray.showMessage("KONECTA", f"Modelo atualizado: {sinais} sinais")
        # Fora da thread da interface: liberar() espera a predição em curso.
        liberar = getattr(antigo, "liberar", None)
        if liberar is not None:
            threading.Thread(target=liberar, daemon=True).start()

    def _mostrar_vocabulario(self) -> None:
        """Preenche a lista de sinais conhecidos, depois do modelo carregar."""
        if self.vocabulario_label is None:
            return
        motor = self.motores.sinais_para_texto
        export = getattr(motor, "_export", None)
        if export is None or not export.classes:
            self.vocabulario_label.setText("Nenhum modelo carregado")
            return
        sinais = sinais_visiveis(export.classes)
        modalidade = "dinâmicos" if export.temporal else "estáticos"
        self.vocabulario_label.setText(
            f"Reconhece {len(sinais)} sinais {modalidade}:  " + " · ".join(sinais)
        )

    def _criar_coluna_avatar(self) -> QWidget:
        """Coluna da direita: o avatar que recebe a fala transcrita.

        É a mesma página do TEXTO_PARA_LIBRAS, embutida: os dois sentidos da
        conversa ficam numa janela só. Exige o QtWebEngine do Qt6 (Chromium
        140). No PyQt5 era um Chromium 83, e o VLibras 7 (set/2026) não roda
        nele: "SyntaxError: Unexpected token '='" no vlibras-initial-*.js.
        """
        caixa = QWidget()
        coluna = QVBoxLayout(caixa)
        coluna.setContentsMargins(0, 0, 0, 0)

        titulo = QLabel("ÁUDIO → LIBRAS")
        titulo.setStyleSheet("font-size: 10px; font-weight: bold; color: #555;")
        coluna.addWidget(titulo)

        url = self.configuracao.texto_para_sinais.url
        try:
            # Cada QWebEngineView sobe um Chromium. A suíte cria dezenas de
            # janelas, e isso a tornava pesada e instável — por isso o desligamento.
            if os.environ.get("KONECTA_SEM_AVATAR") == "1":
                raise RuntimeError("avatar desligado por configuração")

            from PyQt6.QtCore import QUrl
            from PyQt6.QtWebEngineCore import QWebEnginePage
            from PyQt6.QtWebEngineWidgets import QWebEngineView

            class PaginaAvatar(QWebEnginePage):
                # O avatar quebrou em silêncio quando o gov.br trocou o VLibras:
                # agora erro de JavaScript da página vai para o log.
                def javaScriptConsoleMessage(self, nivel, mensagem, linha, origem):
                    erro = QWebEnginePage.JavaScriptConsoleMessageLevel.ErrorMessageLevel
                    if nivel == erro and "ws://" not in mensagem:
                        logger.warning("Avatar (js): %s [%s:%s]", mensagem,
                                       origem.rsplit("/", 1)[-1], linha)

            self.avatar_view = QWebEngineView()
            self.avatar_view.setPage(PaginaAvatar(self.avatar_view))
            self.avatar_view.setMinimumWidth(320)
            self.avatar_view.load(QUrl(url))
            coluna.addWidget(self.avatar_view, stretch=1)
            QTimer.singleShot(5000, self._conferir_avatar)
        except Exception as erro:
            # sem PyQt6-WebEngine o resto do app continua funcionando
            logger.warning("Avatar embutido indisponível: %s", erro)
            aviso = QLabel(f"Avatar indisponível: abra {url} no navegador.")
            aviso.setAlignment(Qt.AlignmentFlag.AlignCenter)
            aviso.setWordWrap(True)
            aviso.setStyleSheet("color: #888; font-size: 11px;")
            coluna.addWidget(aviso, stretch=1)

        self.audio_label = QLabel("Áudio do PC: iniciando…")
        self.audio_label.setStyleSheet("color: gray; font-size: 10px;")
        self.audio_label.setWordWrap(True)
        coluna.addWidget(self.audio_label)
        return caixa

    def _conferir_avatar(self, tentativas: int = 36) -> None:
        """Registra no log quando o avatar fica pronto, ou que não ficou em 3 min.

        A primeira carga do VLibras 7 leva ~40 s. Sem este registro, "não
        carregou" só aparecia quando alguém olhava a tela.
        """
        if self.avatar_view is None:
            return

        def recebido(status):
            if status == "pronto":
                logger.info("Avatar pronto")
            elif tentativas > 0:
                QTimer.singleShot(5000, lambda: self._conferir_avatar(tentativas - 1))
            else:
                logger.warning("Avatar não ficou pronto em 3 min (status: %s)", status)

        self.avatar_view.page().runJavaScript(
            "document.getElementById('status')?.textContent", recebido)

    def _criar_label_candidato(self) -> QLabel:
        """Linha discreta com o sinal em análise, abaixo da palavra confirmada."""
        self.candidato_label = QLabel("aguardando sinal…")
        self.candidato_label.setAlignment(Qt.AlignmentFlag.AlignCenter)  # type: ignore[attr-defined]
        self.candidato_label.setStyleSheet("color: #888; font-size: 11px;")
        return self.candidato_label

    def _criar_seletor_camera(self) -> QHBoxLayout:
        """Deixa escolher entre webcam interna e externa.

        Sem isto o app pega o primeiro dispositivo que responder — que costuma
        ser o errado quando há duas câmeras ligadas.
        """
        linha = QHBoxLayout()
        rotulo = QLabel("Câmera:")
        rotulo.setStyleSheet("font-size: 10px;")

        self.camera_combo = QComboBox()
        self.camera_combo.setStyleSheet("font-size: 10px;")
        for camera in self._cameras:
            self.camera_combo.addItem(camera.rotulo, camera.indice)
        if not self._cameras:
            self.camera_combo.addItem("nenhuma câmera encontrada", -1)
            self.camera_combo.setEnabled(False)
        self.camera_combo.currentIndexChanged.connect(self._trocar_camera)

        # Sondar câmera exige abri-la: se outro programa (ou outra janela deste
        # app) estiver segurando uma, ela some da lista. O botão permite reler
        # depois de liberar, sem reiniciar tudo.
        self.botao_reler = QPushButton("↻")
        self.botao_reler.setToolTip("Procurar câmeras novamente")
        self.botao_reler.setFixedWidth(28)
        self.botao_reler.clicked.connect(self._reler_cameras)

        linha.addWidget(rotulo)
        linha.addWidget(self.camera_combo, stretch=1)
        linha.addWidget(self.botao_reler)
        return linha

    def _reler_cameras(self) -> None:
        """Refaz a busca por câmeras e reconstrói o seletor."""
        atual = self.camera_combo.currentData()
        self._cameras = listar_cameras()

        self.camera_combo.blockSignals(True)
        self.camera_combo.clear()
        for camera in self._cameras:
            self.camera_combo.addItem(camera.rotulo, camera.indice)
        if not self._cameras:
            self.camera_combo.addItem("nenhuma câmera encontrada", -1)
        self.camera_combo.setEnabled(bool(self._cameras))

        # mantém a seleção anterior quando o dispositivo ainda existe
        posicao = self.camera_combo.findData(atual)
        if posicao >= 0:
            self.camera_combo.setCurrentIndex(posicao)
        self.camera_combo.blockSignals(False)

        logger.info("Câmeras relidas: %s", [c.indice for c in self._cameras])

    def _criar_preview(self) -> QLabel:
        """Imagem ao vivo da câmera: é como a pessoa se enquadra."""
        self.preview_label = QLabel("aguardando câmera…")
        self.preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)  # type: ignore[attr-defined]
        self.preview_label.setMinimumHeight(240)
        self.preview_label.setStyleSheet(
            "background-color: #111; color: #888; border-radius: 4px;"
        )
        return self.preview_label

    #: ligações entre os 21 pontos da mão, no padrão do MediaPipe
    OSSOS_MAO = (
        (0, 1), (1, 2), (2, 3), (3, 4),           # polegar
        (0, 5), (5, 6), (6, 7), (7, 8),           # indicador
        (5, 9), (9, 10), (10, 11), (11, 12),      # médio
        (9, 13), (13, 14), (14, 15), (15, 16),    # anelar
        (13, 17), (17, 18), (18, 19), (19, 20),   # mínimo
        (0, 17),                                   # base da palma
    )

    def _desenhar_maos(self, imagem: np.ndarray) -> None:
        """Desenha o esqueleto das mãos sobre o frame (já espelhado).

        Serve para a pessoa ver se o MediaPipe está enxergando a mão dela — a
        causa mais comum de "não reconheceu" é a mão fora de quadro ou mal
        iluminada, e sem o esqueleto isso é invisível.
        """
        motor = self.motores.sinais_para_texto
        maos = getattr(motor, "ultimas_maos", None)
        if not maos:
            return

        import cv2

        altura, largura = imagem.shape[:2]
        for lado, pontos in maos.items():
            if not pontos:
                continue
            cor = (0, 200, 255) if lado == "left_hand" else (0, 255, 120)
            # as coordenadas vêm normalizadas 0..1; x é espelhado junto com a imagem
            coords = [
                (int((1.0 - p[0]) * largura), int(p[1] * altura)) for p in pontos
            ]
            for a, b in self.OSSOS_MAO:
                if a < len(coords) and b < len(coords):
                    cv2.line(imagem, coords[a], coords[b], cor, 2)
            for ponto in coords:
                cv2.circle(imagem, ponto, 3, cor, -1)

    def _mostrar_preview(self, frame: np.ndarray) -> None:
        """Desenha o frame na tela. Roda na thread da GUI (sinal com fila)."""
        if self.preview_label is None:
            return
        try:
            import cv2

            # espelha: a pessoa se vê como num espelho, senão o enquadramento confunde
            imagem = cv2.cvtColor(cv2.flip(frame, 1), cv2.COLOR_BGR2RGB)
            self._desenhar_maos(imagem)
            altura, largura, _ = imagem.shape
            qimagem = QImage(
                imagem.data, largura, altura, 3 * largura, QImage.Format.Format_RGB888
            )
            self.preview_label.setPixmap(
                QPixmap.fromImage(qimagem).scaled(
                    self.preview_label.width(),
                    self.preview_label.height(),
                    Qt.AspectRatioMode.KeepAspectRatio,  # type: ignore[attr-defined]
                    Qt.TransformationMode.SmoothTransformation,  # type: ignore[attr-defined]
                )
            )
        except Exception as erro:
            logger.debug("Falha ao desenhar preview: %s", erro)

    def _trocar_camera(self, posicao: int) -> None:
        """Reinicia a captura no dispositivo escolhido.

        Desconectar antes de parar é essencial: sem isso o worker antigo
        continua entregando frames enquanto encerra, e a tela alterna entre as
        duas câmeras em vez de trocar.
        """
        indice = self.camera_combo.itemData(posicao)
        if indice is None or indice < 0:
            return
        if self.camera_worker is not None and getattr(
            self.camera_worker, "camera_id", None
        ) == indice:
            return  # já é esta câmera

        logger.info("Trocando para câmera %s", indice)
        self.gerenciador.atualizar(camera=Estado.LIGANDO)
        if self.preview_label is not None:
            self.preview_label.setText("trocando de câmera…")

        anterior = self.camera_worker
        self.camera_worker = None
        if anterior is not None:
            try:
                anterior.frame_ready.disconnect()
                anterior.error_occurred.disconnect()
            except (TypeError, RuntimeError):
                pass  # já estava desconectado
            try:
                anterior.stop()
            except Exception as erro:
                logger.warning("Erro ao parar câmera anterior: %s", erro)

        self._init_camera(indice)

    def _create_signal_label(self) -> QLabel:
        """Cria o rótulo do sinal reconhecido."""
        self.signal_label = QLabel("Aguardando...")
        self.signal_label.setStyleSheet(
            "font-size: 36px; font-weight: bold; color: #0084ff; text-align: center;"
        )
        return self.signal_label

    def _create_stats_layout(self) -> QHBoxLayout:
        """Cria a barra de confiança e latência."""
        stats_layout = QHBoxLayout()

        self.confidence_label = QLabel("Confiança: --")
        self.confidence_label.setStyleSheet("color: #666; font-size: 10px;")

        self.latency_label = QLabel("Latência: --ms")
        self.latency_label.setStyleSheet("color: #666; font-size: 10px;")

        stats_layout.addWidget(self.confidence_label)
        stats_layout.addStretch()
        stats_layout.addWidget(self.latency_label)
        return stats_layout

    def _create_history_display(self) -> QTextEdit:
        """Cria o painel de histórico de sinais."""
        self.history_display = QTextEdit()
        self.history_display.setReadOnly(True)
        self.history_display.setMaximumHeight(120)
        self.history_display.setStyleSheet(
            "background-color: #f0f0f0; border: 1px solid #ccc; border-radius: 4px;"
        )
        return self.history_display

    def _create_controls_layout(self) -> QHBoxLayout:
        """Cria os botões de controle (iniciar/parar/limpar)."""
        controls_layout = QHBoxLayout()

        self.start_btn = QPushButton("Iniciar")
        self.start_btn.clicked.connect(self._start_recognition)

        self.stop_btn = QPushButton("Parar")
        self.stop_btn.clicked.connect(self._stop_recognition)
        self.stop_btn.setEnabled(False)

        self.botao_avaliar = QPushButton("Medir acurácia")
        self.botao_avaliar.setToolTip(
            "Sorteia sinais, mostra qual fazer e mede quantos o sistema acerta"
        )
        self.botao_avaliar.clicked.connect(self._alternar_avaliacao)

        self.botao_camera_virtual = QPushButton("Câmera virtual: desligada")
        self.botao_camera_virtual.setToolTip(
            "Cria a câmera 'KONECTA' para o Meet e o Zoom: a sua imagem com a "
            "legenda do sinal embaixo, para quem não sabe Libras"
        )
        self.botao_camera_virtual.clicked.connect(self._alternar_camera_virtual)

        clear_btn = QPushButton("Limpar")
        clear_btn.clicked.connect(self._clear_history)

        controls_layout.addWidget(self.start_btn)
        controls_layout.addWidget(self.stop_btn)
        controls_layout.addWidget(self.botao_avaliar)
        controls_layout.addWidget(self.botao_camera_virtual)
        controls_layout.addWidget(clear_btn)
        return controls_layout

    def _create_motors_section(self) -> QHBoxLayout:
        """Estado de câmera, motores e videochamada, no formato da §11.

        O usuário precisa ver, sem ambiguidade, o que está capturando e o que
        está funcionando — é requisito de privacidade, não enfeite.
        """
        motors_layout = QHBoxLayout()

        self.motors_display = QLabel("iniciando…")
        self.motors_display.setStyleSheet("color: #666; font-size: 9px;")

        motors_layout.addWidget(self.motors_display)
        return motors_layout

    def _on_sessao_mudou(self, sessao) -> None:
        """Recebe a mudança de sessão de QUALQUER thread e repassa ao Qt.

        O GerenciadorSessao é notificado também pela thread do asyncio (quando um
        sinal é reconhecido). Tocar widget fora da thread da GUI é comportamento
        indefinido no Qt — aqui só emitimos o sinal, e o Qt entrega o slot na
        thread certa.
        """
        self.sessao_mudou.emit(sessao)

    def _aplicar_sessao_na_ui(self, sessao) -> None:
        """Atualiza os indicadores. Roda sempre na thread da GUI."""
        if self.motors_display is None:
            return
        partes = [
            rotulo("CÂMERA", sessao.camera),
            rotulo("LIBRAS→TEXTO", sessao.motor_sinais),
            rotulo("TEXTO→LIBRAS", sessao.motor_texto_sinais),
        ]
        if not self.videochamada.injecao_direta:
            partes.append(f"VIDEOCHAMADA: ⚠ {self.videochamada.nome} sem legenda automática")
        self.motors_display.setText("   ".join(partes))

    def _setup_tray(self) -> None:
        """Configura o ícone na bandeja do sistema."""
        self.tray = QSystemTrayIcon(self)

        menu = QMenu()
        show_action = QAction("Mostrar", self)
        show_action.triggered.connect(self.show_normal)
        menu.addAction(show_action)

        hide_action = QAction("Ocultar", self)
        hide_action.triggered.connect(self.hide)
        menu.addAction(hide_action)

        menu.addSeparator()

        exit_action = QAction("Sair", self)
        exit_action.triggered.connect(self._quit)
        menu.addAction(exit_action)

        self.tray.setContextMenu(menu)
        self.tray.setVisible(True)
        logger.info("Tray icon configurado")

    def _process_frame(self, frame: np.ndarray) -> None:
        """Guarda o frame mais recente para o pipeline, descartando o anterior.

        A câmera produz mais rápido do que o pipeline consome. Agendar uma
        corrotina por frame fazia a fila crescer sem limite: medido com pipeline
        bloqueante a 30fps, 69 de 150 frames nunca chegaram a rodar e o atraso
        crescia ~2 frames/s (ver tests/probe_backpressure.py).

        Numa legenda ao vivo o frame velho não tem valor. Guardamos só o último
        e deixamos no máximo um em processamento; o que chegar durante esse
        tempo substitui o anterior na espera.
        """
        if not (self.pipeline and self.is_running):
            return

        # O instante da captura viaja junto com o frame: sem ele, só saberíamos
        # o tempo do pipeline e não o atraso que o usuário realmente sente.
        with self._frame_lock:
            descartou = self._frame_pendente is not None
            self._frame_pendente = (frame, time.monotonic())
            if descartou:
                self.frames_descartados += 1
                return  # já existe um consumidor agendado; ele pegará este frame
            ocupado = self._processando

        if not ocupado:
            asyncio.run_coroutine_threadsafe(self._consumir_frames(), self._loop)

    async def _consumir_frames(self) -> None:
        """Processa o frame pendente até não haver mais nenhum."""
        with self._frame_lock:
            if self._processando:
                return
            self._processando = True
        try:
            while True:
                with self._frame_lock:
                    pendente = self._frame_pendente
                    self._frame_pendente = None
                    if pendente is None:
                        self._processando = False
                        return
                frame, capturado_em = pendente
                await self._run_pipeline(frame, capturado_em)
        except BaseException:
            with self._frame_lock:
                self._processando = False
            raise

    async def _run_pipeline(self, frame: np.ndarray, capturado_em: float = None) -> None:
        """Executa o reconhecimento de Libras para um frame."""
        if capturado_em is None:
            capturado_em = time.monotonic()
        try:
            fila_ms = (time.monotonic() - capturado_em) * 1000
            result = await self._reconhecer_frame(frame)
            total_ms = (time.monotonic() - capturado_em) * 1000
            self.frames_processados += 1
            self.latencia_medida.emit(
                {
                    "fila_ms": fila_ms,
                    "ia_ms": result.latency_ms,
                    "total_ms": total_ms,
                }
            )
            self.recognition_updated.emit(result)
            await self._distribuir(result)
        except Exception as error:
            logger.error("Erro ao processar: %s", error)

    async def _reconhecer_frame(self, frame: np.ndarray) -> PipelineResult:
        """Reconhece um frame pelo provider e estabiliza como o V1 fazia.

        Prefere o provider (que fala o contrato e consome export do SIGNLAB);
        cai no pipeline antigo quando não há provider configurado, para não
        quebrar quem ainda depende dele.

        O estabilizador é o que separa "o que é este frame" de "o que a pessoa
        quis dizer": só sinal que se sustenta vira texto.
        """
        motor = self.motores.sinais_para_texto
        if motor is None:
            return await self.pipeline.process_frame(frame, user_id="default")

        resultado = await motor.reconhecer(frame)

        # Texto vazio tem dois significados diferentes, e confundi-los quebrava
        # o reconhecimento: "sem_maos" é a pessoa saiu de quadro (aí sim zera a
        # contagem), mas "acumulando" é o modelo temporal enchendo a janela —
        # ele devolve vazio nos frames entre predições. Tratar isso como mão
        # ausente reiniciava o candidato a cada 2 frames, e nenhuma predição
        # jamais completava o hold: 119 predições de 100% sem uma confirmação.
        # "sem_sinal" é a classe "(nenhum)": mão em quadro fazendo outra coisa,
        # que para o candidato conta como se ela tivesse parado de sinalizar.
        if resultado.detalhes.get("status") in ("sem_maos", "sem_sinal"):
            self.estabilizador.sem_maos()
        confirmado = (
            self.estabilizador.avaliar(resultado.texto, resultado.confianca)
            if resultado.texto
            else None
        )

        # A tela mostra apenas sinal CONFIRMADO, e o mantém até vir outro. Antes
        # exibíamos o candidato de cada predição, e a palavra piscava entre
        # sinais várias vezes por segundo — ilegível para quem está sinalizando.
        if confirmado is not None:
            self._sinal_exibido = confirmado.texto
            self._momento_confirmado = time.monotonic()
            self._confianca_exibida = confirmado.confianca
            # durante uma medição, cada confirmação fecha uma rodada
            if self.avaliacao is not None:
                self._registrar_rodada(confirmado.texto, confirmado.confianca)

        # Registrar cada predição é o que permite ajustar o limiar durante uma
        # sessão: sem isto, "não reconheceu nada" é indistinguível de "quase
        # reconheceu". Só quando há candidato, para não encher o log.
        if resultado.texto:
            logger.info(
                "predição: %s %.0f%% (limiar %.0f%%) %s",
                resultado.texto,
                resultado.confianca * 100,
                self.estabilizador.limiar_confianca * 100,
                "CONFIRMADO" if confirmado else "abaixo do limiar/hold",
            )

        return PipelineResult(
            signal=self._sinal_exibido,
            confidence=self._confianca_exibida,
            latency_ms=resultado.latencia_ms,
            confidence_level="high" if confirmado else "low",
            validated_by=resultado.fonte,
            recommendation="accept" if confirmado else "wait",
            # o candidato em análise vai à parte, com a confiança: é o que
            # mostra à pessoa por que um sinal não está sendo aceito
            user_history=(
                [f"{resultado.texto}|{resultado.confianca:.2f}"]
                if resultado.texto
                else []
            ),
        )

    # ─────────────────────────────────────────────────────────────
    # Fluxo do usuário SURDO: câmera → sinal → texto → videochamada
    # ─────────────────────────────────────────────────────────────

    def _init_captura_audio(self) -> None:
        """Sobe a escuta do áudio, se o motor de transcrição estiver ativo."""
        if self.motores.audio_para_texto is None:
            return
        try:
            self.audio_worker = CapturaAudioWorker()
            self.audio_worker.fala_detectada.connect(self._processar_fala)
            self.audio_worker.erro.connect(
                lambda m: self.gerenciador.atualizar(microfone=Estado.ERRO, ultimo_erro=m)
            )
            self.audio_worker.start()
            self.gerenciador.atualizar(microfone=Estado.ATIVO)
            logger.info("Captura de áudio iniciada")
        except Exception as erro:
            logger.error("Erro ao iniciar captura de áudio: %s", erro)
            self.gerenciador.atualizar(microfone=Estado.ERRO)

    def _processar_fala(self, audio: np.ndarray) -> None:
        """Recebe um trecho de fala da thread de captura e agenda no loop."""
        asyncio.run_coroutine_threadsafe(self._audio_para_libras(audio), self._loop)

    async def _audio_para_libras(self, audio: np.ndarray) -> None:
        """Fluxo do usuário OUVINTE: fala → texto → avatar em Libras.

        Falha em qualquer etapa degrada em silêncio: perder uma frase é ruim,
        derrubar a sessão inteira é pior.
        """
        motor_audio = self.motores.audio_para_texto
        motor_avatar = self.motores.texto_para_sinais
        if motor_audio is None:
            return

        try:
            resultado = await motor_audio.transcrever(audio, TAXA_AUDIO)
        except Exception as erro:
            logger.warning("Transcrição falhou: %s", mensagem_amigavel(erro))
            return

        texto = (resultado.texto or "").strip()
        if not texto:
            return

        logger.info("Ouvi: %s (%.0fms)", texto, resultado.latencia_ms)
        self.fala_transcrita.emit(texto)

        if motor_avatar is None:
            return
        try:
            await motor_avatar.sinalizar(texto)
        except Exception as erro:
            logger.warning("Avatar não recebeu o texto: %s", mensagem_amigavel(erro))

    def _on_fala_transcrita(self, texto: str) -> None:
        """Mostra na UI o que foi ouvido (roda na thread da GUI)."""
        if self.audio_label is not None:
            self.audio_label.setText(f"Ouvi: {texto}")

    async def _distribuir(self, result: PipelineResult) -> None:
        """Entrega o sinal reconhecido a quem precisa dele.

        Falha aqui não pode derrubar o reconhecimento: se a videochamada estiver
        fora, a conversa continua na tela.
        """
        texto = (result.signal or "").strip()
        if not texto or texto in ("ERROR", "NO_HANDS"):
            return
        if result.recommendation == "wait":
            return  # sinal ainda nao se sustentou; nao vale mandar
        self.gerenciador.registrar_sinal(texto)
        try:
            await self.videochamada.enviar_legenda(texto)
        except Exception as erro:
            logger.warning("Legenda não enviada à videochamada: %s", erro)

    def _on_recognition_updated(self, result: PipelineResult) -> None:
        """Atualiza a UI com o resultado do reconhecimento."""
        if not result.signal:
            # nada confirmado ainda: mantém a tela como está em vez de piscar
            self._mostrar_candidato(result)
            return

        # a palavra confirmada, com a acurácia junto
        self.signal_label.setText(f"{result.signal}   {result.confidence * 100:.0f}%")
        self.signal_label.setStyleSheet(
            "font-size: 40px; font-weight: bold; "
            f"color: {self._confidence_color(result.confidence)}; text-align: center;"
        )
        self.confidence_label.setText("confirmado")
        self._mostrar_candidato(result)
        # a latência é escrita por _on_latencia_medida, que conhece o tempo real
        # sentido pelo usuário (fila + IA), não só o interno do pipeline
        if result.signal != self._ultimo_no_historico:
            self._ultimo_no_historico = result.signal
            self._add_to_history(result.signal, result.confidence, result.latency_ms)
        self.metrics.record_result(result)

    def _mostrar_candidato(self, result: PipelineResult) -> None:
        """Mostra, discreto, o sinal em análise.

        Fica separado da palavra grande de propósito: a pessoa precisa ver que o
        app está reagindo sem que a palavra principal fique trocando.
        """
        if self.candidato_label is None:
            return
        bruto = result.user_history[0] if result.user_history else ""
        if not bruto:
            self.candidato_label.setText("aguardando sinal…")
            self.candidato_label.setStyleSheet("color: #888; font-size: 11px;")
            return

        nome, _, conf = bruto.partition("|")
        confianca = float(conf) if conf else 0.0
        limiar = self.estabilizador.limiar_confianca
        falta = confianca < limiar
        self.candidato_label.setText(
            f"analisando: {nome} {confianca * 100:.0f}%"
            + (f"  (precisa de {limiar * 100:.0f}%)" if falta else "  — segure firme")
        )
        self.candidato_label.setStyleSheet(
            f"color: {'#c88' if falta else '#8c8'}; font-size: 11px;"
        )

    @staticmethod
    def _confidence_color(confidence: float) -> str:
        """Retorna a cor associada ao nível de confiança."""
        if confidence > 0.85:
            return HIGH_CONFIDENCE_COLOR
        if confidence > 0.7:
            return MEDIUM_CONFIDENCE_COLOR
        return LOW_CONFIDENCE_COLOR

    def _on_latencia_medida(self, medida: Dict) -> None:
        """Mostra a latência que o usuário sente, decomposta por etapa (§13).

        ``total`` é o que importa para o usuário; ``fila`` e ``IA`` separadas
        dizem onde atacar quando o total sobe.
        """
        total = medida["total_ms"]
        self.latency_label.setText(
            f"Latência: {total:.0f}ms (fila {medida['fila_ms']:.0f} · IA {medida['ia_ms']:.0f})"
        )
        cor = "#00aa00" if total < 1000 else "#ffaa00" if total < 1500 else "#ff0000"
        self.latency_label.setStyleSheet(f"color: {cor}; font-size: 10px;")

    def _on_metrics_updated(self, stats: Dict) -> None:
        """Registra estatísticas atualizadas (mantém sinal para extensões)."""
        logger.info("Stats: %s", stats)

    def _add_to_history(self, signal: str, confidence: float, latency: float) -> None:
        """Adiciona um sinal ao histórico, mantendo no máximo ``HISTORY_LIMIT`` linhas."""
        new_entry = f"• {signal} ({confidence:.0%}) [{latency:.0f}ms]\n"
        lines = self.history_display.toPlainText().split("\n")
        if len(lines) > HISTORY_LIMIT:
            lines = lines[-HISTORY_LIMIT:]
        self.history_display.setText(new_entry + "\n".join(lines))

    def _start_recognition(self) -> None:
        """Inicia o reconhecimento contínuo."""
        self.is_running = True
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        logger.info("Reconhecimento iniciado")

    def _stop_recognition(self) -> None:
        """Pausa o reconhecimento."""
        self.is_running = False
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.signal_label.setText("Pausado")
        logger.info("Reconhecimento pausado")

    def _clear_history(self) -> None:
        """Limpa o histórico de sinais."""
        self.history_display.clear()

    def show_normal(self) -> None:
        """Mostra e eleva a janela."""
        self.show()
        self.raise_()
        self.activateWindow()

    def _quit(self) -> None:
        """Encerra a aplicação a partir do menu do tray."""
        self.close()

    # A legenda fica na imagem por este tempo depois de confirmada. Mais que
    # isso, quem está na chamada leria uma palavra antiga como se fosse nova.
    _LEGENDA_S = 5.0

    def _alternar_camera_virtual(self) -> None:
        """Liga ou desliga a câmera "KONECTA" que o Meet e o Zoom enxergam."""
        if self._camera_virtual is not None:
            self._desligar_camera_virtual()
            return
        try:
            self._camera_virtual = CameraVirtual()
        except ErroCameraVirtual as erro:
            from PyQt6.QtWidgets import QMessageBox

            logger.error("Câmera virtual: %s", erro)
            QMessageBox.warning(self, "Câmera virtual",
                                f"Não foi possível ligar a câmera virtual: {erro}")
            return
        self._saida_virtual = SaidaDeQuadros()
        self.botao_camera_virtual.setText("Câmera virtual: ligada")
        logger.info("Câmera virtual ligada")
        if self.tray is not None:
            self.tray.showMessage(
                "KONECTA", "Câmera virtual ligada. No Meet ou no Zoom, escolha a câmera KONECTA.")

    def _desligar_camera_virtual(self) -> None:
        if self._saida_virtual is not None:
            self._saida_virtual.fechar()
            self._saida_virtual = None
        if self._camera_virtual is not None:
            try:
                self._camera_virtual.desligar()
            except Exception as erro:
                logger.warning("Câmera virtual: erro ao desligar: %s", erro)
            self._camera_virtual = None
            logger.info("Câmera virtual desligada")
        if self.botao_camera_virtual is not None:
            self.botao_camera_virtual.setText("Câmera virtual: desligada")

    def _enviar_camera_virtual(self, frame: np.ndarray) -> None:
        """Manda o quadro da webcam, com a legenda do último sinal, para a câmera virtual.

        O quadro vai sem espelho: é assim que os outros participantes o recebem.
        O Meet espelha só a própria imagem de quem transmite, então ela verá a
        legenda invertida na tela dela e os outros a verão certa.
        """
        if self._saida_virtual is None:
            return
        legenda = None
        if self._sinal_exibido and time.monotonic() - self._momento_confirmado < self._LEGENDA_S:
            legenda = self._sinal_exibido.upper()
        try:
            self._saida_virtual.escrever(compor_quadro(frame, legenda))
        except Exception as erro:  # a chamada não pode derrubar o reconhecimento
            logger.warning("Câmera virtual: quadro não enviado: %s", erro)

    def closeEvent(self, event) -> None:
        """Encerramento seguro: corta captura, fecha motores e para o loop (§10)."""
        self._desligar_camera_virtual()
        if self.camera_worker:
            self.camera_worker.stop()
        self.gerenciador.desligar_tudo()

        # os motores precisam fechar sessões HTTP e liberar o MediaPipe; damos um
        # prazo curto para não travar o fechamento da janela
        try:
            futuro = asyncio.run_coroutine_threadsafe(self.motores.encerrar(), self._loop)
            futuro.result(timeout=2.0)
        except Exception as erro:
            logger.debug("Motores não encerraram no prazo: %s", erro)

        # Cancelar antes de parar: uma checagem de motor presa num socket
        # impediria o processo de encerrar.
        def _cancelar_e_parar():
            for tarefa in asyncio.all_tasks(self._loop):
                tarefa.cancel()
            self._loop.stop()

        self._loop.call_soon_threadsafe(_cancelar_e_parar)
        self._loop_thread.join(timeout=3)
        event.accept()
        logger.info("Aplicação fechada")


def _ja_esta_aberto() -> bool:
    """Detecta outra janela do KONECTA já rodando.

    Duas instâncias disputam a câmera: a segunda não consegue abrir o
    dispositivo que a primeira segura, e ele simplesmente some da lista de
    câmeras — foi o que fez a webcam externa "sumir" numa sessão.
    """
    import socket

    # Os testes sobem janelas em subprocessos enquanto o app pode estar aberto:
    # a trava faria cada um deles encerrar antes de testar qualquer coisa.
    if os.environ.get("KONECTA_SEM_TRAVA") == "1":
        return False

    trava = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        trava.bind(("127.0.0.1", 47615))  # porta arbitrária, só para travar
        trava.listen(1)
    except OSError:
        return True
    # mantém o socket vivo pelo tempo do processo
    globals()["_TRAVA_INSTANCIA"] = trava
    return False


def main() -> None:
    """Função principal da aplicação."""
    if congelado():
        # Instalado, o atalho abre o KONECTA.exe direto: não passa pelo
        # KONECTA.pyw, que é quem define estes padrões em desenvolvimento.
        # setdefault: quem quiser outro valor ainda pode sobrescrever.
        os.environ.setdefault("KONECTA_CAMERA_VIRTUAL", "1")
        os.environ.setdefault("KONECTA_LIMIAR", "0.60")
        os.environ.setdefault("KONECTA_AUDIO_ATIVO", "true")

    app = QApplication(sys.argv)
    app.setStyle("Fusion")

    if _ja_esta_aberto():
        from PyQt6.QtWidgets import QMessageBox

        logger.warning("Já existe uma janela do KONECTA aberta; encerrando esta")
        QMessageBox.warning(
            None,
            "KONECTA_V3",
            "Já existe uma janela do KONECTA_V3 aberta.\n\n"
            "Duas janelas disputam a câmera e uma delas fica sem imagem.\n"
            "Use a janela que já está aberta.",
        )
        sys.exit(0)

    window = KonectaIntelligenceHub()
    window.show()

    logger.info("KONECTA Intelligence Hub iniciado")
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
