"""Onde as coisas vivem quando o KONECTA foi instalado (frozen com PyInstaller).

Rodando do código-fonte, o worker do Keras é a ``.venv-temporal`` e o avatar é
``python TEXTO_PARA_LIBRAS/server.py`` — dois processos "soltos" que o próprio
usuário sabe montar. No instalador, não existe python nem venv na máquina: os
mesmos papéis são executáveis próprios (``temporal/sinais_worker.exe``,
``avatar_server.exe``), irmãos do ``KONECTA.exe``, gerados pelo build em
``KONECTA_V3/build_instalador``.

Este módulo é o único lugar que sabe diferenciar os dois mundos. O resto do
código central (providers, main.py) só chama estas funções.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def congelado() -> bool:
    """True quando este processo É um KONECTA.exe gerado pelo PyInstaller."""
    return bool(getattr(sys, "frozen", False))


def pasta_instalacao() -> Path:
    """A pasta onde o executável (ou, em dev, o pacote ``app_central``) vive."""
    if congelado():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def pasta_dados_do_usuario() -> Path:
    """Onde gravar log e estado: instalado, ``Program Files`` não é gravável
    por usuário comum, e escrever lá silenciosamente falha ou é redirecionado
    pela virtualização do Windows. Em dev mantém o comportamento de sempre
    (pasta do projeto), para não mudar nada de quem já usa o app assim.
    """
    if not congelado():
        return Path(__file__).resolve().parents[1]  # KONECTA_V3/
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "KONECTA"


def executavel_irmao(nome: str) -> Path | None:
    """Acha um executável irmão pelo nome (ex.: ``"avatar_server.exe"``).

    Procura direto na pasta de instalação e, por causa do worker do Keras,
    também em ``temporal/`` — ele vem de um build separado (ambiente próprio
    com TensorFlow) e fica numa subpasta de propósito.
    """
    base = pasta_instalacao()
    for candidato in (base / nome, base / "temporal" / nome):
        if candidato.is_file():
            return candidato
    return None
