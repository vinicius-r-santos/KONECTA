"""Liga a câmera virtual KONECTA com a imagem de teste, até apertar Enter.

Etapa 1 da câmera nativa: serve para conferir, numa chamada de verdade, que o
outro lado recebe a imagem. Ainda não mostra a webcam nem a legenda.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app_central.capture.camera_virtual import CameraVirtual, ErroCameraVirtual  # noqa: E402

try:
    camera = CameraVirtual()
except ErroCameraVirtual as erro:
    print(f"[ERRO] Não liguei a câmera: {erro}")
    input("\nEnter para sair")
    sys.exit(1)

print("[OK] Câmera ligada.")
print("     No Meet ou no Zoom, escolha a câmera 'KONECTA (Câmera Virtual do Windows)'.")
print("     Por enquanto ela mostra uma imagem colorida de teste.")
try:
    input("\nEnter para desligar a câmera...")
finally:
    camera.desligar()
