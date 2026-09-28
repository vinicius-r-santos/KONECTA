"""Câmera virtual "KONECTA" pela API nativa do Windows 11 (MFCreateVirtualCamera).

O Zoom e o Meet não recebem imagem do KONECTA: quem entrega câmeras a eles é o
serviço de câmera do Windows (Frame Server), e ele só aceita uma fonte que seja
uma DLL COM registrada em HKLM — a de ``KONECTA_V3/vcam`` (base: VCamSample,
MIT). Este módulo só pede ao Windows que crie a câmera a partir dessa DLL e a
mantenha viva: com ``MFVirtualCameraLifetime_Session``, ela existe enquanto
este processo segura o objeto, e some quando o KONECTA fecha.

Chamadas COM via ctypes, sem dependência nova. As posições na vtable foram
conferidas contra mfvirtualcamera.h e mfobjects.h do SDK 10.0.26100 — errar
uma derruba o processo, não devolve erro.
"""
from __future__ import annotations

import ctypes
import sys
import uuid
from ctypes import POINTER, WINFUNCTYPE, byref, c_long, c_uint32, c_void_p, c_wchar_p
from pathlib import Path
from typing import List

CLSID_KONECTA = "{" + (Path(__file__).resolve().parents[2] / "vcam" / "CLSID_KONECTA.txt"
                       ).read_text(encoding="utf-8").strip().upper() + "}"
NOME_PADRAO = "KONECTA"

_MF_VERSION = 0x00020070
_TIPO_SOFTWARE, _VIDA_SESSAO, _ACESSO_USUARIO = 0, 0, 0

# vtable: IUnknown (0-2) + IMFAttributes (3-32) + IMFVirtualCamera (33-43)
_RELEASE = 2
_GET_ALLOCATED_STRING = 13
_SET_GUID = 24
_START = 36
_REMOVE = 38


class _GUID(ctypes.Structure):
    _fields_ = [("bytes", ctypes.c_ubyte * 16)]

    @classmethod
    def de(cls, texto: str) -> "_GUID":
        g = cls()
        ctypes.memmove(g.bytes, uuid.UUID(texto).bytes_le, 16)
        return g


_SOURCE_TYPE = _GUID.de("c60ac5fe-252a-478f-a0ef-bc8fa5f7cad3")
_SOURCE_TYPE_VIDCAP = _GUID.de("8ac3587a-4ae7-42d8-99e0-0a6013eef90f")
_FRIENDLY_NAME = _GUID.de("60d0e559-52f8-4fa2-bbce-acdb34a8ec01")


# O que fazer diante de cada erro já visto. O texto do Windows ("a solicitação
# é inválida no estado atual") não diz a ninguém que a causa era outra janela.
_O_QUE_FAZER = {
    0xC00D36B2: "provavelmente já existe outra câmera KONECTA ligada; feche a outra "
                "janela (ou o KONECTA) e tente de novo",
    0x80040154: "a DLL da câmera não está registrada; rode vcam\\REGISTRAR_CAMERA.bat "
                "como administrador",
    0x80070005: "os serviços de câmera do Windows não conseguem ler a pasta da DLL; ela "
                "não pode ficar dentro de C:\\Users",
}


class ErroCameraVirtual(RuntimeError):
    def __init__(self, etapa: str, hr: int):
        self.hr = hr & 0xFFFFFFFF
        # FormatError quer o HRESULT com sinal (C long): 0x8xxxxxxx sem sinal
        # estourava com OverflowError e escondia o erro de verdade.
        com_sinal = ctypes.c_long(self.hr).value
        texto = f"{etapa}: HRESULT 0x{self.hr:08X} ({ctypes.FormatError(com_sinal).strip()})"
        if self.hr in _O_QUE_FAZER:
            texto += f" -> {_O_QUE_FAZER[self.hr]}"
        super().__init__(texto)


def _metodo(obj: c_void_p, indice: int, *tipos):
    vtable = ctypes.cast(obj, POINTER(POINTER(c_void_p)))[0]
    return WINFUNCTYPE(c_long, c_void_p, *tipos)(vtable[indice])


def _checar(etapa: str, hr: int) -> None:
    if hr < 0:
        raise ErroCameraVirtual(etapa, hr)


def _iniciar_mf() -> None:
    # MTA, como o winrt::init_apartment() do app de referência. S_FALSE e
    # RPC_E_CHANGED_MODE (já inicializado por outro componente) não são falha.
    hr = ctypes.windll.ole32.CoInitializeEx(None, 0)
    if hr < 0 and (hr & 0xFFFFFFFF) != 0x80010106:
        raise ErroCameraVirtual("CoInitializeEx", hr)
    _checar("MFStartup", ctypes.windll.mfplat.MFStartup(_MF_VERSION, 0))


class CameraVirtual:
    """Liga a câmera ao criar; ``desligar()`` (ou sair do ``with``) a remove."""

    def __init__(self, nome: str = NOME_PADRAO):
        if sys.getwindowsversion().build < 22000:
            raise ErroCameraVirtual("Windows 11 necessário", 0x80004001)
        _iniciar_mf()
        criar = ctypes.windll.mfsensorgroup.MFCreateVirtualCamera
        criar.restype = c_long
        criar.argtypes = [c_uint32, c_uint32, c_uint32, c_wchar_p, c_wchar_p,
                          c_void_p, c_uint32, POINTER(c_void_p)]
        self._obj = c_void_p()
        _checar("MFCreateVirtualCamera",
                criar(_TIPO_SOFTWARE, _VIDA_SESSAO, _ACESSO_USUARIO, nome,
                      CLSID_KONECTA, None, 0, byref(self._obj)))
        try:
            _checar("IMFVirtualCamera::Start", _metodo(self._obj, _START, c_void_p)(self._obj, None))
        except Exception:
            self._soltar()
            raise
        self.nome = nome

    def desligar(self) -> None:
        if not self._obj:
            return
        # Remove, sem Shutdown antes: a referência (VCamSample) documenta que
        # Shutdown + Remove desliga a fonte duas vezes e impede a remoção.
        try:
            _metodo(self._obj, _REMOVE)(self._obj)
        finally:
            self._soltar()

    def _soltar(self) -> None:
        if self._obj:
            _metodo(self._obj, _RELEASE)(self._obj)
            self._obj = c_void_p()

    def __enter__(self) -> "CameraVirtual":
        return self

    def __exit__(self, *_):
        self.desligar()


def listar_cameras() -> List[str]:
    """Nomes das câmeras na ordem do Media Foundation — a mesma do índice CAP_MSMF do OpenCV."""
    _iniciar_mf()
    atributos = c_void_p()
    _checar("MFCreateAttributes", ctypes.windll.mfplat.MFCreateAttributes(byref(atributos), 1))
    try:
        _checar("SetGUID", _metodo(atributos, _SET_GUID, POINTER(_GUID), POINTER(_GUID))(
            atributos, byref(_SOURCE_TYPE), byref(_SOURCE_TYPE_VIDCAP)))
        lista, total = POINTER(c_void_p)(), c_uint32()
        _checar("MFEnumDeviceSources",
                ctypes.windll.mf.MFEnumDeviceSources(atributos, byref(lista), byref(total)))
        nomes = []
        for i in range(total.value):
            ativador = c_void_p(lista[i])
            texto, tamanho = c_wchar_p(), c_uint32()
            hr = _metodo(ativador, _GET_ALLOCATED_STRING, POINTER(_GUID), POINTER(c_wchar_p),
                         POINTER(c_uint32))(ativador, byref(_FRIENDLY_NAME), byref(texto), byref(tamanho))
            nomes.append(texto.value if hr >= 0 else "?")
            if hr >= 0:
                ctypes.windll.ole32.CoTaskMemFree(texto)
            _metodo(ativador, _RELEASE)(ativador)
        ctypes.windll.ole32.CoTaskMemFree(lista)
        return nomes
    finally:
        _metodo(atributos, _RELEASE)(atributos)


# ---------------------------------------------------------------- quadros
# Contrato com a DLL (vcam/VCamSampleSource/FrameGenerator.h): cabeçalho de 64
# bytes + pixels BGRA. Mudou lá, muda aqui.
_QUADRO_NOME = "Global" + chr(92) + "KonectaVCamFrame"  # barra montada: escapes já enganaram
_QUADRO_MAGIC = 0x3143564B  # "KVC1"
_QUADRO_CABECALHO = 64
_QUADRO_MAX_L, _QUADRO_MAX_A = 1920, 1080
_QUADRO_TAMANHO = _QUADRO_CABECALHO + _QUADRO_MAX_L * _QUADRO_MAX_A * 4
_FILE_MAP_ESCRITA = 0x0002


class _Cabecalho(ctypes.Structure):
    _fields_ = [("magic", c_uint32), ("largura", c_uint32), ("altura", c_uint32),
                ("stride", c_uint32), ("sequencia", ctypes.c_int64), ("tempo_ms", ctypes.c_int64)]


class SaidaDeQuadros:
    """Escreve o quadro que a câmera KONECTA vai mostrar.

    Quem cria a memória é a DLL, dentro do serviço de câmera do Windows: um
    processo comum recebe erro 5 ao criar em Global\ (medido nesta máquina).
    Então aqui só se abre — e ela só existe depois que algum app (Meet, Zoom)
    abre a câmera. Antes disso ``escrever`` devolve False e ninguém perde nada:
    não há quem esteja olhando.
    """

    def __init__(self):
        self._handle = None
        self._view = None
        self._ultima_tentativa = 0.0
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.OpenFileMappingW.restype = c_void_p
        k.OpenFileMappingW.argtypes = [c_uint32, ctypes.c_int, c_wchar_p]
        k.MapViewOfFile.restype = c_void_p
        k.MapViewOfFile.argtypes = [c_void_p, c_uint32, c_uint32, c_uint32, ctypes.c_size_t]
        k.UnmapViewOfFile.argtypes = [c_void_p]
        k.CloseHandle.argtypes = [c_void_p]
        k.GetTickCount64.restype = ctypes.c_uint64
        self._k = k

    def _abrir(self) -> bool:
        import time
        if self._view:
            return True
        agora = time.monotonic()
        if agora - self._ultima_tentativa < 1.0:
            return False
        self._ultima_tentativa = agora
        handle = self._k.OpenFileMappingW(_FILE_MAP_ESCRITA, 0, _QUADRO_NOME)
        if not handle:
            return False
        view = self._k.MapViewOfFile(handle, _FILE_MAP_ESCRITA, 0, 0, _QUADRO_TAMANHO)
        if not view:
            self._k.CloseHandle(handle)
            return False
        import numpy as np
        self._handle, self._view = handle, view
        self._cab = _Cabecalho.from_address(view)
        self._pixels = np.ctypeslib.as_array(
            (ctypes.c_ubyte * (_QUADRO_MAX_L * _QUADRO_MAX_A * 4)).from_address(view + _QUADRO_CABECALHO))
        return True

    @property
    def conectada(self) -> bool:
        return self._view is not None

    def escrever(self, quadro_bgr) -> bool:
        """Publica um quadro BGR (OpenCV). False se nenhum app abriu a câmera ainda."""
        import cv2
        if not self._abrir():
            return False
        altura, largura = quadro_bgr.shape[:2]
        if largura > _QUADRO_MAX_L or altura > _QUADRO_MAX_A:
            raise ValueError(f"quadro {largura}x{altura} maior que {_QUADRO_MAX_L}x{_QUADRO_MAX_A}")
        cab = self._cab
        # sequência ímpar = escrevendo: a DLL não copia um quadro pela metade
        seq = cab.sequencia + (1 if cab.sequencia % 2 == 0 else 2)
        cab.sequencia = seq
        cab.magic, cab.largura, cab.altura, cab.stride = _QUADRO_MAGIC, largura, altura, largura * 4
        destino = self._pixels[: altura * largura * 4].reshape(altura, largura, 4)
        cv2.cvtColor(quadro_bgr, cv2.COLOR_BGR2BGRA, dst=destino)
        cab.tempo_ms = self._k.GetTickCount64()
        cab.sequencia = seq + 1
        return True

    def fechar(self) -> None:
        if self._view:
            self._k.UnmapViewOfFile(self._view)
            self._view = None
        if self._handle:
            self._k.CloseHandle(self._handle)
            self._handle = None


# ---------------------------------------------------------------- legenda
_SAIDA_L, _SAIDA_A = 1280, 720  # a DLL entrega 16:9 (MediaStream.cpp)
# O Meet no celular em pé mostrou só a metade central da largura (print do
# teste): a legenda cabe nessa faixa para não ser cortada.
_LEGENDA_LARGURA_MAX = 600
_LEGENDA_MARGEM_BASE = 40
_FONTE = "C:/Windows/Fonts/segoeuib.ttf"
_cache_legenda: dict = {}
_tela: dict = {}


def _imagem_legenda(texto: str):
    """Faixa da legenda: cor (uint8) e pesos de mistura, desenhada uma vez por texto.

    Pillow e não cv2.putText: as fontes do OpenCV não têm acento, e "NÃO"
    sairia "N?O".
    """
    import numpy as np
    from PIL import Image, ImageDraw, ImageFont

    if texto in _cache_legenda:
        return _cache_legenda[texto]
    tamanho = 80
    while True:
        fonte = ImageFont.truetype(_FONTE, tamanho)
        x0, y0, x1, y1 = fonte.getbbox(texto)
        if x1 - x0 <= _LEGENDA_LARGURA_MAX or tamanho <= 28:
            break
        tamanho -= 4
    mx, my = 28, 16
    largura, altura = (x1 - x0) + 2 * mx, (y1 - y0) + 2 * my
    imagem = Image.new("RGBA", (largura, altura), (0, 0, 0, 0))
    desenho = ImageDraw.Draw(imagem)
    desenho.rounded_rectangle((0, 0, largura - 1, altura - 1), radius=altura // 4, fill=(0, 0, 0, 185))
    desenho.text((mx - x0, my - y0), texto, font=fonte, fill=(255, 255, 255, 255))
    rgba = np.asarray(imagem)
    peso = rgba[..., 3].astype(np.float32) / 255
    pronto = (np.ascontiguousarray(rgba[..., [2, 1, 0]]), peso, 1 - peso)
    if len(_cache_legenda) > 64:
        _cache_legenda.clear()
    _cache_legenda[texto] = pronto
    return pronto


def compor_quadro(quadro_bgr, legenda: str | None = None):
    """Quadro 1280x720 para a câmera virtual: a webcam inteira e a legenda embaixo.

    A webcam é 4:3 e vai inteira, com faixas nas laterais: cortar em cima e
    embaixo para encher o 16:9 tiraria mãos do quadro.

    O quadro devolvido é reaproveitado na chamada seguinte (as faixas laterais
    não mudam e pintá-las a cada quadro custava 3 ms): use antes de chamar de novo.
    """
    import cv2
    import numpy as np

    altura, largura = quadro_bgr.shape[:2]
    escala = min(_SAIDA_L / largura, _SAIDA_A / altura)
    nl, na = int(largura * escala), int(altura * escala)
    x, y = (_SAIDA_L - nl) // 2, (_SAIDA_A - na) // 2
    chave = (largura, altura)
    if chave not in _tela:
        _tela.clear()
        tela = np.empty((_SAIDA_A, _SAIDA_L, 3), np.uint8)
        tela[:] = (20, 16, 15)
        _tela[chave] = tela
    saida = _tela[chave]
    saida[y:y + na, x:x + nl] = cv2.resize(quadro_bgr, (nl, na), interpolation=cv2.INTER_LINEAR)
    if legenda:
        cor, peso, peso_fundo = _imagem_legenda(legenda)
        la, ll = cor.shape[:2]
        lx, ly = (_SAIDA_L - ll) // 2, _SAIDA_A - la - _LEGENDA_MARGEM_BASE
        regiao = np.ascontiguousarray(saida[ly:ly + la, lx:lx + ll])
        saida[ly:ly + la, lx:lx + ll] = cv2.blendLinear(cor, regiao, peso, peso_fundo)
    return saida
