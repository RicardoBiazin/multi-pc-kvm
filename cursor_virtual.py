"""Desenha um cursor numa tela que nao tem mouse.

O Windows so' mostra o cursor se houver um dispositivo apontador. Num PC sem
mouse nenhum, `SetCursorPos` move um cursor INVISIVEL: o PC e' comandado de
longe e quem comanda nao ve' onde esta'. O "Mouse sem Fronteiras" resolve
desenhando o cursor por conta propria (a opcao "Desenhar cursor do mouse"); aqui
e' o mesmo: uma janela transparente, sempre no topo, que nao pega clique e nao
rouba o foco, com uma setinha que segue a posicao injetada.

So' liga quando `sem_mouse()` -- com mouse, o cursor de verdade ja' aparece e
dois cursores confundiriam.
"""

from __future__ import annotations

import ctypes
import logging
import threading
from ctypes import wintypes

log = logging.getLogger("cursor_virtual")

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

SM_MOUSEPRESENT = 19
WS_POPUP = 0x80000000
WS_EX_TOPMOST, WS_EX_TRANSPARENT = 0x8, 0x20
WS_EX_TOOLWINDOW, WS_EX_LAYERED, WS_EX_NOACTIVATE = 0x80, 0x80000, 0x08000000
SW_HIDE, SW_SHOWNOACTIVATE = 0, 4
ULW_ALPHA = 2
AC_SRC_OVER, AC_SRC_ALPHA = 0, 1
WM_QUIT = 0x12
WM_APP = 0x8000
MSG_MOVER, MSG_OCULTAR = WM_APP + 1, WM_APP + 2
PM_REMOVE = 1

# A setinha: '#' contorno preto, 'o' miolo branco, '.' transparente. 12 x 19.
SETA = (
    "#...........",
    "##..........",
    "#o#.........",
    "#oo#........",
    "#ooo#.......",
    "#oooo#......",
    "#ooooo#.....",
    "#oooooo#....",
    "#ooooooo#...",
    "#oooooooo#..",
    "#ooooo#####.",
    "#oo#oo#.....",
    "#o#.#oo#....",
    "##..#oo#....",
    "#....#oo#...",
    ".....#oo#...",
    "......#oo#..",
    "......#oo#..",
    ".......##...",
)
LARGURA, ALTURA = len(SETA[0]), len(SETA)

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT,
                             wintypes.WPARAM, wintypes.LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HANDLE),
                ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HANDLE),
                ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                ("SourceConstantAlpha", ctypes.c_ubyte),
                ("AlphaFormat", ctypes.c_ubyte)]


class SIZE(ctypes.Structure):
    _fields_ = [("cx", wintypes.LONG), ("cy", wintypes.LONG)]


user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
                                  wintypes.LPARAM]
user32.DefWindowProcW.restype = LRESULT
user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
user32.RegisterClassW.restype = wintypes.ATOM
user32.CreateWindowExW.argtypes = [
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND,
    wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
user32.CreateWindowExW.restype = wintypes.HWND
user32.DestroyWindow.argtypes = [wintypes.HWND]
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetDC.argtypes = [wintypes.HWND]
user32.GetDC.restype = wintypes.HDC
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                               wintypes.UINT, wintypes.UINT]
user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.DispatchMessageW.restype = LRESULT
user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT,
                                      wintypes.WPARAM, wintypes.LPARAM]
user32.UpdateLayeredWindow.argtypes = [
    wintypes.HWND, wintypes.HDC, ctypes.POINTER(wintypes.POINT),
    ctypes.POINTER(SIZE), wintypes.HDC, ctypes.POINTER(wintypes.POINT),
    wintypes.COLORREF, ctypes.POINTER(BLENDFUNCTION), wintypes.DWORD]
user32.UpdateLayeredWindow.restype = wintypes.BOOL
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateDIBSection.argtypes = [
    wintypes.HDC, ctypes.c_void_p, wintypes.UINT,
    ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD]
gdi32.CreateDIBSection.restype = wintypes.HBITMAP
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.DeleteDC.argtypes = [wintypes.HDC]
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE
kernel32.GetCurrentThreadId.restype = wintypes.DWORD


def sem_mouse() -> bool:
    """O Windows acha que nao ha' mouse ligado a este PC?"""
    return user32.GetSystemMetrics(SM_MOUSEPRESENT) == 0


def pixels_da_seta() -> bytes:
    """BGRA com alfa PRE-MULTIPLICADO (o que UpdateLayeredWindow espera),
    de cima para baixo -- a altura do DIB e' negativa."""
    saida = bytearray()
    for linha in SETA:
        for c in linha:
            if c == "#":
                saida += bytes((0, 0, 0, 255))
            elif c == "o":
                saida += bytes((255, 255, 255, 255))
            else:
                saida += bytes((0, 0, 0, 0))
    return bytes(saida)


class CursorVirtual:
    """A janela da setinha. Todo o trabalho de janela fica numa thread propria,
    com laco de mensagens; de fora so' se posta o pedido."""

    def __init__(self):
        self._thread: threading.Thread | None = None
        self._pronto = threading.Event()
        self._tid = 0
        self.funciona = False  # a janela foi criada e desenha
        self._hwnd = self._hdc_mem = None

    def _garantir(self) -> bool:
        if self._thread is None or not self._thread.is_alive():
            self._pronto.clear()
            self._thread = threading.Thread(target=self._laco, name="cursor-virtual",
                                            daemon=True)
            self._thread.start()
            self._pronto.wait(3)
        return self.funciona

    def mover(self, x: int, y: int) -> None:
        if self._garantir():
            user32.PostThreadMessageW(self._tid, MSG_MOVER, x & 0xFFFFFFFF,
                                      y & 0xFFFFFFFF)

    def ocultar(self) -> None:
        if self._thread is not None and self.funciona:
            user32.PostThreadMessageW(self._tid, MSG_OCULTAR, 0, 0)

    def parar(self) -> None:
        if self._thread is not None and self.funciona:
            user32.PostThreadMessageW(self._tid, WM_QUIT, 0, 0)
            self._thread.join(timeout=2)
        self._thread = None
        self.funciona = False

    # -- a thread da janela --------------------------------------------------

    def _laco(self) -> None:
        self._tid = kernel32.GetCurrentThreadId()
        hwnd = hdc_mem = bitmap = hdc_tela = None
        try:
            proc = WNDPROC(lambda h, m, w, l: user32.DefWindowProcW(h, m, w, l))
            nome = "MultiPCKVMCursor"
            classe = WNDCLASSW()
            classe.lpfnWndProc = proc
            classe.hInstance = kernel32.GetModuleHandleW(None)
            classe.lpszClassName = nome
            user32.RegisterClassW(ctypes.byref(classe))  # repetir da' erro inofensivo
            hwnd = user32.CreateWindowExW(
                WS_EX_TOPMOST | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW
                | WS_EX_LAYERED | WS_EX_NOACTIVATE, nome, "", WS_POPUP, 0, 0,
                LARGURA, ALTURA, None, None, classe.hInstance, None)
            if not hwnd:
                raise OSError("CreateWindowExW falhou", ctypes.get_last_error())
            hdc_tela = user32.GetDC(None)
            hdc_mem = gdi32.CreateCompatibleDC(hdc_tela)
            bmi = BITMAPINFOHEADER()
            bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
            bmi.biWidth, bmi.biHeight = LARGURA, -ALTURA  # de cima para baixo
            bmi.biPlanes, bmi.biBitCount = 1, 32
            bits = ctypes.c_void_p()
            bitmap = gdi32.CreateDIBSection(hdc_mem, ctypes.byref(bmi), 0,
                                            ctypes.byref(bits), None, 0)
            if not bitmap or not bits:
                raise OSError("CreateDIBSection falhou", ctypes.get_last_error())
            dados = pixels_da_seta()
            ctypes.memmove(bits, dados, len(dados))
            gdi32.SelectObject(hdc_mem, bitmap)
            self._hwnd, self._hdc_mem = hwnd, hdc_mem
            self.funciona = True
            self._pronto.set()

            visivel = False
            msg = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == MSG_MOVER:
                    x = ctypes.c_int32(msg.wParam & 0xFFFFFFFF).value
                    y = ctypes.c_int32(msg.lParam & 0xFFFFFFFF).value
                    self._desenhar(hwnd, hdc_mem, x, y)
                    if not visivel:
                        user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
                        visivel = True
                elif msg.message == MSG_OCULTAR and visivel:
                    user32.ShowWindow(hwnd, SW_HIDE)
                    visivel = False
                else:
                    user32.TranslateMessage(ctypes.byref(msg))
                    user32.DispatchMessageW(ctypes.byref(msg))
        except Exception:
            log.warning("nao consegui criar o cursor virtual", exc_info=True)
        finally:
            self.funciona = False
            self._pronto.set()
            if bitmap:
                gdi32.DeleteObject(bitmap)
            if hdc_mem:
                gdi32.DeleteDC(hdc_mem)
            if hdc_tela:
                user32.ReleaseDC(None, hdc_tela)
            if hwnd:
                user32.DestroyWindow(hwnd)

    @staticmethod
    def _desenhar(hwnd, hdc_mem, x: int, y: int) -> bool:
        destino = wintypes.POINT(x, y)
        tamanho = SIZE(LARGURA, ALTURA)
        origem = wintypes.POINT(0, 0)
        mistura = BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
        return bool(user32.UpdateLayeredWindow(
            hwnd, None, ctypes.byref(destino), ctypes.byref(tamanho), hdc_mem,
            ctypes.byref(origem), 0, ctypes.byref(mistura), ULW_ALPHA))
