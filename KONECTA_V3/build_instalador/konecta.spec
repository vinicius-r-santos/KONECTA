# -*- mode: python ; coding: utf-8 -*-
"""Spec do PyInstaller para o build "principal" do KONECTA: a janela (PyQt6 +
MediaPipe + OpenCV), o worker do Whisper e o servidor do avatar — os três
compartilham a mesma venv (.venv), então saem de UM COLLECT só, sem duplicar
os ~900MB de mediapipe/opencv/PyQt6 três vezes.

O worker do Keras (sinais_worker.exe) fica de fora de propósito: ele precisa
do TensorFlow, que não pode coexistir com o MediaPipe neste processo (ver o
comentário em app_central/providers/signlab_sinais.py). Tem o próprio spec,
konecta_temporal.spec, rodado a partir da .venv-temporal.

Uso (da raiz do KONECTA_V3, com a .venv ativa ou explicitando o python dela):
    .venv\\Scripts\\pyinstaller build_instalador\\konecta.spec --distpath build_instalador\\dist --noconfirm
"""
from pathlib import Path

from PyInstaller.utils.hooks import collect_all

RAIZ = Path(SPECPATH).resolve().parent  # KONECTA_V3/
TEXTO_PARA_LIBRAS = RAIZ.parent / "TEXTO_PARA_LIBRAS"
ICONE = str(RAIZ.parent / "konecta.ico")

# O modelo que sai "de fábrica" no instalador: o mesmo que o KONECTA de
# desenvolvimento está usando (o .zip mais novo em models\). Já foi um nome
# fixo, e um retreino (Pai/Mãe corrigidos) teria ficado de fora do instalador.
import sys  # noqa: E402

sys.path.insert(0, str(RAIZ))
from app_central.providers.export_signlab import descobrir_modelo  # noqa: E402

MODELO_DE_FABRICA = descobrir_modelo()
LANDMARKER = RAIZ / "models" / "hand_landmarker.task"
if not LANDMARKER.is_file():
    LANDMARKER = Path("C:/KONECTA/SIGNLAB/vision/models/hand_landmarker.task")

block_cipher = None

# --collect-all mediapipe/cv2 PRECISA entrar já na construção do Analysis: os
# tuples que collect_all() devolve são (origem, destino) — o formato que
# Analysis(datas=...) espera. Anexar isso depois em a.datas (já convertido
# para o TOC interno de 3 campos) mistura os dois formatos e quebra o COLLECT
# com "not enough values to unpack" — foi o que aconteceu na 1a tentativa.
dados_mp, binarios_mp, ocultos_mp = collect_all("mediapipe")
dados_cv2, binarios_cv2, ocultos_cv2 = collect_all("cv2")

a_konecta = Analysis(
    [str(RAIZ / "app_central" / "main.py")],
    pathex=[str(RAIZ)],
    hookspath=[str(RAIZ / "build_instalador" / "hooks")],
    hiddenimports=["webrtcvad", *ocultos_mp, *ocultos_cv2],
    datas=[
        (str(LANDMARKER), "models"),
        (str(MODELO_DE_FABRICA), "models"),
        (str(RAIZ / "vcam" / "CLSID_KONECTA.txt"), "vcam"),
        # Path(__file__).parent de main.py, quando ELE é o script de entrada
        # (não um módulo importado), resolve para a raiz do bundle — não para
        # "app_central/" como no código-fonte. Medido: sem isto o app avisava
        # "config.yaml não encontrado" procurando em _internal\config\, e
        # funcionava mesmo assim com os padrões, mas silenciava a config real.
        (str(RAIZ / "app_central" / "config" / "config.yaml"), "config"),
        *dados_mp, *dados_cv2,
    ],
    binaries=[*binarios_mp, *binarios_cv2],
    noarchive=False,
)

# O modelo "small" do faster-whisper, do cache do Hugging Face desta máquina.
# Vai embutido: sem ele a primeira fala no PC instalado baixaria ~460MB.
_CACHE_WHISPER = Path.home() / ".cache" / "huggingface" / "hub" / "models--Systran--faster-whisper-small" / "snapshots"
WHISPER_SMALL = next(_CACHE_WHISPER.iterdir())

a_whisper = Analysis(
    [str(RAIZ / "app_central" / "providers" / "whisper_worker.py")],
    pathex=[str(RAIZ)],
    hookspath=[str(RAIZ / "build_instalador" / "hooks")],
    hiddenimports=["webrtcvad"],
    datas=[(str(WHISPER_SMALL), "whisper/small")],
)

a_avatar = Analysis(
    [str(TEXTO_PARA_LIBRAS / "server.py")],
    pathex=[str(TEXTO_PARA_LIBRAS)],
    hookspath=[str(RAIZ / "build_instalador" / "hooks")],
    datas=[(str(TEXTO_PARA_LIBRAS / "static"), "static")],
)

MERGE(
    (a_konecta, "main", "KONECTA"),
    (a_whisper, "whisper_worker", "whisper_worker"),
    (a_avatar, "server", "avatar_server"),
)

pyz_konecta = PYZ(a_konecta.pure, a_konecta.zipped_data, cipher=block_cipher)
exe_konecta = EXE(
    pyz_konecta, a_konecta.scripts, [], exclude_binaries=True,
    name="KONECTA", icon=ICONE, console=False,
)

pyz_whisper = PYZ(a_whisper.pure, a_whisper.zipped_data, cipher=block_cipher)
exe_whisper = EXE(
    pyz_whisper, a_whisper.scripts, [], exclude_binaries=True,
    name="whisper_worker", console=True,  # mesmo comportamento de hoje: escondido por CREATE_NO_WINDOW no Popen
)

pyz_avatar = PYZ(a_avatar.pure, a_avatar.zipped_data, cipher=block_cipher)
exe_avatar = EXE(
    pyz_avatar, a_avatar.scripts, [], exclude_binaries=True,
    name="avatar_server", console=True,
)

coll = COLLECT(
    exe_konecta, a_konecta.binaries, a_konecta.zipfiles, a_konecta.datas,
    exe_whisper, a_whisper.binaries, a_whisper.zipfiles, a_whisper.datas,
    exe_avatar, a_avatar.binaries, a_avatar.zipfiles, a_avatar.datas,
    name="KONECTA",
)
