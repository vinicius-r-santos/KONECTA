"""Sobe o app de verdade: camera real, modelo real, nada substituido.

Diferente dos outros roteiros, aqui nao ha camera falsa nem motor falso. E' o
mais proximo do que a interprete vai encontrar — so' falta a pessoa sinalizando.

Uso:
    python tests/smoke_app_real.py [segundos]
"""

import sys
import threading
import time
from pathlib import Path

from PyQt6.QtWidgets import QApplication

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app_central.main as principal

SEGUNDOS = int(sys.argv[1]) if len(sys.argv) > 1 else 25

_criada = []
_HubOriginal = principal.KonectaIntelligenceHub


class HubEspiao(_HubOriginal):
    def __init__(self):
        super().__init__()
        _criada.append(self)


principal.KonectaIntelligenceHub = HubEspiao


def encerrar():
    time.sleep(SEGUNDOS)
    app = QApplication.instance()
    if app is not None:
        app.quit()


threading.Thread(target=encerrar, daemon=True).start()

try:
    principal.main()
except SystemExit:
    pass

hub = _criada[0] if _criada else None
if hub is None:
    print("FALHOU: janela nao foi criada", flush=True)
    sys.exit(1)

motor = hub.motores.sinais_para_texto
print(
    f"RESULTADO processados={hub.frames_processados} "
    f"descartados={hub.frames_descartados} "
    f"motor={type(motor).__name__ if motor else None} "
    f"camera={hub.gerenciador.sessao.camera.value} "
    f"historico={hub.estabilizador.historico[-5:]}",
    flush=True,
)

if hub.frames_processados == 0:
    print("FALHOU: nenhum frame da camera real chegou ao reconhecimento", flush=True)
    sys.exit(1)

print("APP_REAL_OK", flush=True)
sys.exit(0)
