"""Envio do texto reconhecido para a videochamada (§5 da spec).

Camada de abstração: o núcleo chama ``enviar_legenda`` e não sabe se por trás
está Zoom, Teams ou outra coisa. Adicionar plataforma é criar uma classe aqui.

O que foi apurado antes de implementar, como a §5 manda:

- **Zoom** — tem API REST oficial de closed captions. O host habilita legendas
  de terceiros e copia uma *caption URL*; qualquer serviço faz ``POST`` de texto
  puro UTF-8 nela, com número de sequência. Caminho oficial, sem gambiarra.
- **Teams** — mesmo modelo, via endpoint CART (Communication Access Real-time
  Translation). O organizador gera a URL e o texto enviado aparece para quem
  estiver com legendas ligadas.
- **Google Meet** — **não expõe API pública de injeção de legenda de terceiros.**
  Não há caminho oficial equivalente. Ver ``AdaptadorMeet`` para a alternativa.

Nenhum destes faz engenharia reversa de interface: Zoom e Teams usam o
mecanismo publicado, e o Meet assume a limitação em vez de fingir que funciona.
"""

from __future__ import annotations

import logging
import urllib.parse
from abc import ABC, abstractmethod
from typing import Optional

import aiohttp

from app_central.infra.resiliencia import CircuitBreaker, com_retry

logger = logging.getLogger(__name__)


class AdaptadorVideochamada(ABC):
    """Contrato comum de envio de legenda."""

    nome: str = "desconhecido"
    #: False quando a plataforma não permite injeção direta — a UI avisa o usuário
    injecao_direta: bool = True

    @abstractmethod
    async def enviar_legenda(self, texto: str) -> bool:
        """Entrega o texto à chamada. Devolve se conseguiu."""

    async def disponivel(self) -> bool:
        return True

    async def encerrar(self) -> None:
        pass


class _AdaptadorPorURL(AdaptadorVideochamada):
    """Base para plataformas que recebem legenda por POST numa URL do host.

    Zoom e Teams seguem esse mesmo desenho; muda o formato da requisição.
    """

    def __init__(self, url_legenda: str, timeout_s: float = 5.0, idioma: str = "pt-BR"):
        self.url_legenda = url_legenda
        self.timeout_s = timeout_s
        self.idioma = idioma
        self._sequencia = 0
        self._sessao: Optional[aiohttp.ClientSession] = None
        self._breaker = CircuitBreaker(nome=self.nome, limite_falhas=3, espera_s=20.0)

    async def _obter_sessao(self) -> aiohttp.ClientSession:
        if self._sessao is None or self._sessao.closed:
            self._sessao = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout_s)
            )
        return self._sessao

    @abstractmethod
    def _montar_url(self) -> str:
        """URL final da requisição, já com os parâmetros da plataforma."""

    async def enviar_legenda(self, texto: str) -> bool:
        texto = (texto or "").strip()
        if not texto:
            return False
        if not self.url_legenda:
            logger.warning("%s: URL de legenda não configurada", self.nome)
            return False

        url = self._montar_url()

        async def _post() -> None:
            sessao = await self._obter_sessao()
            async with sessao.post(
                url,
                data=texto.encode("utf-8"),
                headers={"Content-Type": "text/plain; charset=utf-8"},
            ) as resposta:
                if resposta.status >= 400:
                    raise RuntimeError(f"HTTP {resposta.status}")

        try:
            await com_retry(
                _post,
                tentativas=2,  # legenda é efêmera: insistir muito só atrasa a próxima
                breaker=self._breaker,
                descricao=f"legenda {self.nome}",
            )
        except Exception as erro:
            logger.warning("%s: não foi possível enviar a legenda (%s)", self.nome, erro)
            return False

        self._sequencia += 1
        return True

    async def encerrar(self) -> None:
        if self._sessao is not None and not self._sessao.closed:
            await self._sessao.close()
        self._sessao = None


class AdaptadorZoom(_AdaptadorPorURL):
    """Closed Captioning REST API do Zoom.

    O host precisa habilitar legendas de terceiros e fornecer a caption URL.
    A sequência precisa ser crescente: o Zoom usa para ordenar e descartar
    repetição.
    """

    nome = "zoom"

    def _montar_url(self) -> str:
        separador = "&" if "?" in self.url_legenda else "?"
        parametros = urllib.parse.urlencode({"seq": self._sequencia, "lang": self.idioma})
        return f"{self.url_legenda}{separador}{parametros}"


class AdaptadorTeams(_AdaptadorPorURL):
    """Legendas CART do Microsoft Teams.

    O organizador gera a URL de ingestão CART da reunião. O texto enviado
    aparece para quem estiver com legendas ligadas.
    """

    nome = "teams"

    def _montar_url(self) -> str:
        return self.url_legenda


class AdaptadorMeet(AdaptadorVideochamada):
    """Google Meet — sem injeção direta.

    Não existe API pública que permita a um app externo inserir legenda numa
    reunião do Meet. Em vez de fingir suporte, o adaptador assume a limitação e
    entrega o texto na área de transferência: o usuário cola no chat da reunião.

    É pior que legenda automática, e é honesto quanto a isso — ``injecao_direta``
    é False para a UI poder explicar ao usuário por que o comportamento difere.
    """

    nome = "meet"
    injecao_direta = False

    LIMITACAO = (
        "O Google Meet não permite que aplicativos externos insiram legendas. "
        "O texto vai para a área de transferência: cole no chat da reunião."
    )

    def __init__(self):
        self.ultimo_texto = ""

    async def enviar_legenda(self, texto: str) -> bool:
        texto = (texto or "").strip()
        if not texto:
            return False
        self.ultimo_texto = texto
        try:
            from PyQt6.QtWidgets import QApplication

            app = QApplication.instance()
            if app is None:
                return False
            app.clipboard().setText(texto)
            return True
        except Exception as erro:
            logger.warning("meet: não foi possível usar a área de transferência (%s)", erro)
            return False


class AdaptadorNulo(AdaptadorVideochamada):
    """Sem plataforma configurada. Existe para o núcleo não precisar de ``if``."""

    nome = "nenhum"

    async def enviar_legenda(self, texto: str) -> bool:
        return False


ADAPTADORES = {
    "zoom": AdaptadorZoom,
    "teams": AdaptadorTeams,
    "meet": AdaptadorMeet,
    "nenhum": AdaptadorNulo,
}


def criar_adaptador(plataforma: str, url_legenda: str = "") -> AdaptadorVideochamada:
    """Cria o adaptador pelo nome. Plataforma desconhecida vira ``AdaptadorNulo``."""
    classe = ADAPTADORES.get((plataforma or "").lower())
    if classe is None:
        logger.warning("Plataforma '%s' desconhecida; seguindo sem videochamada", plataforma)
        return AdaptadorNulo()
    if classe in (AdaptadorMeet, AdaptadorNulo):
        return classe()
    return classe(url_legenda=url_legenda)
