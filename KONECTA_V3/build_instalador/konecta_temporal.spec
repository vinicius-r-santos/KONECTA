# -*- mode: python ; coding: utf-8 -*-
"""Spec do worker temporal (sinais_worker.exe): TensorFlow + Keras, isolado do
resto porque ele e' o MediaPipe/PyQt6 do build principal não coexistem no
mesmo processo (ver app_central/providers/signlab_sinais.py). Por isso roda a
partir da .venv-temporal, nunca da .venv principal.

Uso (da raiz do KONECTA_V3) — repare que o distpath é dist\\KONECTA, SEM
"\\temporal" no final: quem cria essa subpasta é o próprio COLLECT (name
abaixo), porque senão o PyInstaller aninha mais um nível
(dist\\KONECTA\\temporal\\sinais_worker\\sinais_worker.exe) e o KONECTA.exe
procura o executável em temporal\\sinais_worker.exe — um nível acima.
    .venv-temporal\\Scripts\\pyinstaller build_instalador\\konecta_temporal.spec --distpath build_instalador\\dist\\KONECTA --noconfirm
"""
from pathlib import Path

RAIZ = Path(SPECPATH).resolve().parent  # KONECTA_V3/

a = Analysis(
    [str(RAIZ / "app_central" / "providers" / "sinais_worker.py")],
    pathex=[str(RAIZ)],
)

pyz = PYZ(a.pure, a.zipped_data)
exe = EXE(
    pyz, a.scripts, [], exclude_binaries=True,
    name="sinais_worker", console=True,  # escondido pelo CREATE_NO_WINDOW de quem chama
)
coll = COLLECT(exe, a.binaries, a.zipfiles, a.datas, name="temporal")
