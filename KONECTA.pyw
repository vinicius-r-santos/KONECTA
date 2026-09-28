"""Abre o KONECTA inteiro com um clique, sem janela preta de terminal.

É o que o ícone KONECTA da área de trabalho executa. Sobe o que ainda não
estiver no ar — o SIGNLAB (recebe o app do celular e publica os modelos) e o
servidor do avatar — e abre o KONECTA com a câmera virtual ligada. O avatar
abre numa janela do Edge ao lado.

O SIGNLAB continua rodando depois de fechar o KONECTA, de propósito: o app do
celular precisa dele para enviar gravações.
"""
import ctypes
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

RAIZ = Path(__file__).resolve().parent
SEM_JANELA = subprocess.CREATE_NO_WINDOW
JANELA = "KONECTA_V3"  # título da janela do KONECTA (main.py)
user32 = ctypes.windll.user32


def aviso(texto: str) -> None:
    # sem terminal, um erro só é visto se virar caixa de mensagem
    user32.MessageBoxW(None, texto, "KONECTA", 0x10)


def janela_do_konecta():
    return user32.FindWindowW(None, JANELA)


def no_ar(url: str) -> bool:
    try:
        urllib.request.urlopen(url, timeout=2)
        return True
    except urllib.error.HTTPError:
        return True  # 401/404: respondeu, está no ar
    except OSError:
        return False


def subir(nome: str, url: str, comando: list, pasta: Path) -> None:
    if no_ar(url):
        return
    subprocess.Popen(comando, cwd=pasta, creationflags=SEM_JANELA,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(40):
        if no_ar(url):
            return
        time.sleep(0.5)
    aviso(f"O {nome} não subiu. O KONECTA abre mesmo assim, sem ele.")


def abrir_tudo() -> None:
    # o python do sistema: é o que os .bat do SIGNLAB e do avatar sempre usaram
    subir("SIGNLAB", "http://127.0.0.1:8100/entrar.html",
          ["python", "-m", "uvicorn", "app.backend.main:app", "--port", "8100"], RAIZ / "SIGNLAB")
    subir("servidor do avatar", "http://127.0.0.1:8300/", ["python", "server.py"],
          RAIZ / "TEXTO_PARA_LIBRAS")

    # as gravações do SIGNLAB não estão no git: copia antes de abrir
    backup = RAIZ / "BACKUP_SIGNLAB.bat"
    if backup.exists():
        subprocess.Popen(["cmd", "/c", str(backup)], creationflags=SEM_JANELA,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    konecta = RAIZ / "KONECTA_V3"
    pythonw = konecta / ".venv" / "Scripts" / "pythonw.exe"
    if not pythonw.exists():
        aviso(f"Ambiente do KONECTA não encontrado: {pythonw}")
        return
    ambiente = dict(os.environ, KONECTA_CAMERA_VIRTUAL="1")
    ambiente.setdefault("KONECTA_LIMIAR", "0.60")  # os mesmos padrões do TESTE_INTERPRETE.bat
    ambiente.setdefault("KONECTA_AUDIO_ATIVO", "true")
    subprocess.Popen([str(pythonw), "app_central/main.py"], cwd=konecta, env=ambiente)


def tela_de_abertura(limite_s: int = 150) -> None:
    """Mostra "Abrindo…" até a janela do KONECTA surgir.

    Sem terminal, os ~40 s de abertura pareciam um clique que não funcionou, e
    um segundo clique tentaria abrir outro KONECTA.
    """
    import tkinter as tk

    raiz = tk.Tk()
    raiz.overrideredirect(True)
    raiz.attributes("-topmost", True)
    raiz.configure(bg="#0f172a")
    tk.Label(raiz, text="KONECTA", fg="#14b8a6", bg="#0f172a",
             font=("Segoe UI", 24, "bold")).pack(padx=48, pady=(26, 2))
    tk.Label(raiz, text="Abrindo… leva uns 40 segundos", fg="#e2e8f0", bg="#0f172a",
             font=("Segoe UI", 11)).pack(padx=48, pady=(0, 26))
    raiz.update_idletasks()
    largura, altura = raiz.winfo_width(), raiz.winfo_height()
    raiz.geometry(f"+{(raiz.winfo_screenwidth() - largura) // 2}"
                  f"+{(raiz.winfo_screenheight() - altura) // 2}")
    inicio = time.monotonic()

    def checar():
        if janela_do_konecta() or time.monotonic() - inicio > limite_s:
            raiz.destroy()
            return
        raiz.after(500, checar)

    checar()
    raiz.mainloop()


if __name__ == "__main__":
    ja_aberto = janela_do_konecta()
    if ja_aberto:
        # segundo clique: só traz a janela para a frente, não abre outro
        user32.ShowWindow(ja_aberto, 9)  # SW_RESTORE
        user32.SetForegroundWindow(ja_aberto)
    else:
        threading.Thread(target=abrir_tudo, daemon=True).start()
        tela_de_abertura()
