# Sobrepõe o hook padrão (pyinstaller-hooks-contrib) que falha ao analisar
# webrtcvad-wheels: ele tenta importar um submódulo interno que não existe
# nesse pacote. webrtcvad-wheels é puro Python + uma extensão .pyd já
# compilada, então nenhuma coleta especial é necessária.
hiddenimports = ["webrtcvad"]
