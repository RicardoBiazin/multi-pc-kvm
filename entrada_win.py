"""Camada Win32 do Multi PC - KVM, via ctypes: captura por hook e injecao por
SendInput.

Duas metades independentes:

* `Captura` -- instala WH_MOUSE_LL e WH_KEYBOARD_LL e roda o message loop
  obrigatorio numa thread propria. O callback repassa cada evento para
  `tratar(ev)`; se `tratar` devolver True o evento e' *engolido* e nunca chega
  as aplicacoes locais. O callback tem de ser rapido: se passar de ~300 ms o
  Windows desativa o hook em silencio, sem erro nenhum.

* `Injetor` -- reproduz eventos com SendInput. Mouse em coordenadas absolutas
  normalizadas (0..65535) sobre o desktop virtual; teclado por scancode, que e'
  bem mais compativel que virtual-key com jogos e com layouts diferentes.
"""

from __future__ import annotations

import ctypes
import logging
import threading
import time
from ctypes import wintypes

log = logging.getLogger("entrada")

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

MARCA = 0x3232314B  # dwExtraInfo dos nossos eventos, para nao recapturar

# -- constantes -------------------------------------------------------------

WH_KEYBOARD_LL, WH_MOUSE_LL = 13, 14
LLMHF_INJECTED, LLKHF_EXTENDED = 0x01, 0x01

WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN, WM_LBUTTONUP = 0x0201, 0x0202
WM_RBUTTONDOWN, WM_RBUTTONUP = 0x0204, 0x0205
WM_MBUTTONDOWN, WM_MBUTTONUP = 0x0207, 0x0208
WM_MOUSEWHEEL, WM_MOUSEHWHEEL = 0x020A, 0x020E
WM_XBUTTONDOWN, WM_XBUTTONUP = 0x020B, 0x020C
WM_KEYDOWN, WM_KEYUP = 0x0100, 0x0101
WM_SYSKEYDOWN, WM_SYSKEYUP = 0x0104, 0x0105

INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
MOUSEEVENTF_MOVE, MOUSEEVENTF_ABSOLUTE, MOUSEEVENTF_VIRTUALDESK = 0x0001, 0x8000, 0x4000
MOUSEEVENTF_WHEEL, MOUSEEVENTF_HWHEEL = 0x0800, 0x1000
KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP, KEYEVENTF_SCANCODE = 0x0001, 0x0002, 0x0008

SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79

# Raw Input: e' de onde vem o deslocamento *relativo* do mouse. O hook de baixo
# nivel so' informa posicao absoluta -- ja' presa a' tela -- e por isso nao serve
# para mover um cursor remoto: encostado numa borda, ele para de variar.
WM_INPUT = 0x00FF
RID_INPUT = 0x10000003
RIDEV_INPUTSINK = 0x00000100  # receber mesmo sem foco; exige hwndTarget
RIM_TYPEMOUSE = 0
MOUSE_MOVE_ABSOLUTE = 0x01
PAGINA_GENERICA, USO_MOUSE = 0x01, 0x02
HWND_MESSAGE = -3

CLASSE_RAW = "MultiPCKVMRawInput"
GWLP_WNDPROC = -4
ERRO_CLASSE_JA_EXISTE = 1410

WM_APP = 0x8000
MSG_REINSTALAR = WM_APP + 1  # pedido de reinstalacao para a thread dos hooks
INTERVALO_VIGIA = 2.0    # s entre verificacoes do vigia
TOLERANCIA_VIGIA = 3.0   # s de atraso do hook em relacao ao Raw Input
# Renovacao INCONDICIONAL dos dois ganchos, de tempo em tempo.
#
# A deteccao acima so' enxerga a morte do gancho do MOUSE: ela compara o Raw
# Input com `_visto_hook`, e quem marca `_visto_hook` e' so' o callback do mouse.
# Se o Windows derrubar apenas o do TECLADO -- basta um callback passar de ~300
# ms, e o RDP torna isso facil --, o mouse continua marcando, o atraso fica em
# zero, o vigia nunca dispara: o teclado morre em silencio e nao volta ate'
# reiniciar o programa. Era o sintoma "o mouse atravessa mas o teclado nao".
#
# Nao ha' deteccao possivel para o teclado: silencio dele nao prova nada, pode
# ser so' ninguem digitando. Renovar periodicamente e' o que fecha o buraco.
# `_instalar_hooks` instala os novos antes de soltar os antigos, entao a troca
# nao abre janela sem cobertura, e o custo e' irrelevante nesta frequencia.
INTERVALO_RENOVACAO = 60.0  # s
# Enquanto ESTE PC comanda outro, a renovacao fica bem mais curta.
#
# Ganchos de baixo nivel sao chamados em ordem INVERSA de instalacao: o mais
# recente primeiro. Quem instalar depois de nos passa na frente e pode consumir a
# tecla antes -- e' o que o cliente de Area de Trabalho Remota faz em tela cheia,
# para mandar o teclado a' sessao remota. Este programa sobe antes, o `mstsc` sobe
# depois, e o teclado para de atravessar enquanto o mouse continua indo (o mouse
# vem pelo Raw Input, que nao entra nessa fila).
#
# Reinstalar devolve o primeiro lugar. Nao ha' como vencer em definitivo -- os
# dois lados podem reinstalar, e ganha quem fez por ultimo --, mas a alguns
# segundos o buraco deixa de ser perceptivel. So' vale a pena enquanto estamos
# comandando: em casa, o teclado e' para funcionar aqui mesmo.
#
# RECUADO EM 11/08/2026, DE 2s PARA O MESMO INTERVALO DA RENOVACAO NORMAL.
#
# Com 2 segundos, o servidor passou a cair de minuto em minuto: a sessao
# anterior tinha rodado 7 HORAS e sido encerrada limpa; com a cadencia rapida
# foram tres quedas em tres minutos, todas com corrupcao de heap (0xc0000374) e
# nenhuma linha no log. O que o log mostrava era 24 reinstalacoes em 50 segundos
# -- e, pior, `mouse visto ha' 38s` durante elas: a rotatividade nao estava nem
# consertando o gancho, so' trocando estrutura de hook debaixo de callbacks que
# disparam centenas de vezes por segundo.
#
# Nao esta' provado que a reinstalacao CAUSA a corrupcao; a corrupcao ja'
# existia antes desta constante. Esta' medido que a frequencia disparou com ela.
# Ate' a causa aparecer (Page Heap no PC que cai), o certo e' nao agitar o
# subsistema que corrompe memoria.
#
# O custo do recuo: quando outro programa toma o primeiro lugar na fila dos
# ganchos -- o cliente de Area de Trabalho Remota em tela cheia --, o teclado
# leva ate' um minuto para voltar a atravessar, em vez de dois segundos.
INTERVALO_DISPUTA = INTERVALO_RENOVACAO

VK_SHIFT, VK_CONTROL, VK_MENU, VK_ESCAPE = 0x10, 0x11, 0x12, 0x1B
VK_LWIN, VK_RWIN = 0x5B, 0x5C

# Botao -> (flag de pressionar, flag de soltar, mouseData)
BOTOES = {
    "esq": (0x0002, 0x0004, 0),
    "dir": (0x0008, 0x0010, 0),
    "meio": (0x0020, 0x0040, 0),
    "x1": (0x0080, 0x0100, 1),
    "x2": (0x0080, 0x0100, 2),
}

# O Shift DIREITO e' a excecao a regra "repasse o flag de estendida do hook".
# O hook de baixo nivel entrega o Shift direito com LLKHF_EXTENDED ligado, mas
# `E0 36` nao e' o Shift direito: e' o "fake shift" que o Windows fabrica em
# volta das teclas do teclado numerico, e nao corresponde a tecla nenhuma. Se o
# flag for repassado para o SendInput, o outro PC recebe um evento que o Windows
# descarta em silencio -- o Shift direito simplesmente nao funciona la', e o
# esquerdo funciona, porque o scancode dele (0x2A) chega sem o flag.
#
# O Shift direito de verdade e' o scancode 0x36 SEM prefixo. Ctrl e Alt direitos
# nao entram aqui: `E0 1D` e `E0 38` sao mesmo as teclas da direita.
SCAN_SHIFT_DIREITO = 0x36

# Modificadores a soltar ao trocar de maquina, senao ficam presos: (vk, scancode,
# estendida). Os VKs sao os laterais (0xA0..0xA5) porque VK_SHIFT generico nao
# distingue esquerda de direita, e a direita ficaria presa.
MODIFICADORES = (
    (0xA0, 0x2A, False),  # Shift esquerdo
    (0xA1, 0x36, False),  # Shift direito
    (0xA2, 0x1D, False),  # Ctrl esquerdo
    (0xA3, 0x1D, True),   # Ctrl direito
    (0xA4, 0x38, False),  # Alt esquerdo
    (0xA5, 0x38, True),   # Alt direito (AltGr)
    (VK_LWIN, 0x5B, True),
    (VK_RWIN, 0x5C, True),
)


# -- structs ----------------------------------------------------------------

ULONG_PTR = wintypes.WPARAM  # inteiro do tamanho de um ponteiro


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG), ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD),
                ("wParamH", wintypes.WORD)]


class INPUT(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]

    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _U)]


class RAWINPUTDEVICE(ctypes.Structure):
    _fields_ = [("usUsagePage", wintypes.USHORT), ("usUsage", wintypes.USHORT),
                ("dwFlags", wintypes.DWORD), ("hwndTarget", wintypes.HWND)]


class RAWINPUTHEADER(ctypes.Structure):
    _fields_ = [("dwType", wintypes.DWORD), ("dwSize", wintypes.DWORD),
                ("hDevice", wintypes.HANDLE), ("wParam", wintypes.WPARAM)]


class RAWMOUSE(ctypes.Structure):
    # A uniao {ULONG ulButtons; struct {USHORT usButtonFlags; USHORT usButtonData;}}
    # ocupa 4 bytes, entao os dois USHORT servem no lugar dela.
    _fields_ = [
        ("usFlags", wintypes.USHORT), ("_reservado", wintypes.USHORT),
        ("usButtonFlags", wintypes.USHORT), ("usButtonData", wintypes.USHORT),
        ("ulRawButtons", wintypes.ULONG),
        ("lLastX", wintypes.LONG), ("lLastY", wintypes.LONG),
        ("ulExtraInformation", wintypes.ULONG),
    ]


class RAWINPUT(ctypes.Structure):
    _fields_ = [("header", RAWINPUTHEADER), ("mouse", RAWMOUSE)]


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("pt", wintypes.POINT), ("mouseData", wintypes.DWORD),
        ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


LRESULT = ctypes.c_ssize_t
HOOKPROC = ctypes.CFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
WNDPROC = ctypes.CFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT,
                           wintypes.WPARAM, wintypes.LPARAM)


class WNDCLASS(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT), ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR),
    ]


user32.RegisterRawInputDevices.argtypes = [ctypes.POINTER(RAWINPUTDEVICE),
                                           wintypes.UINT, wintypes.UINT]
user32.RegisterRawInputDevices.restype = wintypes.BOOL
user32.GetRawInputData.argtypes = [wintypes.HANDLE, wintypes.UINT, ctypes.c_void_p,
                                   ctypes.POINTER(wintypes.UINT), wintypes.UINT]
user32.GetRawInputData.restype = wintypes.UINT
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
                                  wintypes.LPARAM]
user32.DefWindowProcW.restype = LRESULT
# Sem estes tipos declarados o ctypes trunca os handles de 64 bits e o
# CreateWindowExW estoura em OverflowError.
kernel32.GetModuleHandleW.restype = wintypes.HMODULE
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASS)]
user32.RegisterClassW.restype = wintypes.WORD
user32.CreateWindowExW.argtypes = [
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
user32.CreateWindowExW.restype = wintypes.HWND
user32.DestroyWindow.argtypes = [wintypes.HWND]
user32.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
user32.UnregisterClassW.restype = wintypes.BOOL
# SetWindowLongPtrW so' existe no Windows de 64 bits; no de 32 quem faz o mesmo
# papel e' SetWindowLongW, com o mesmo tamanho de ponteiro.
_definir_wndproc = getattr(user32, "SetWindowLongPtrW", None) or user32.SetWindowLongW
_definir_wndproc.argtypes = [wintypes.HWND, ctypes.c_int, WNDPROC]
_definir_wndproc.restype = LRESULT
_ler_wndproc = getattr(user32, "GetWindowLongPtrW", None) or user32.GetWindowLongW
_ler_wndproc.argtypes = [wintypes.HWND, ctypes.c_int]
_ler_wndproc.restype = LRESULT

user32.SetWindowsHookExW.restype = wintypes.HHOOK
user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wintypes.HINSTANCE,
                                     wintypes.DWORD]
user32.CallNextHookEx.restype = LRESULT
user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM,
                                  wintypes.LPARAM]
user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.SendInput.restype = wintypes.UINT


# -- utilidades -------------------------------------------------------------


def ativar_dpi() -> None:
    """PER_MONITOR_AWARE_V2. Sem isto as coordenadas vem escaladas e a borda
    nunca casa entre monitores com escalas diferentes."""
    try:
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except AttributeError:  # Windows < 10 1703
        try:
            ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)
        except Exception:
            user32.SetProcessDPIAware()


def geometria_virtual() -> tuple[int, int, int, int]:
    """(x, y, largura, altura) do desktop virtual, somando todos os monitores."""
    return (
        user32.GetSystemMetrics(SM_XVIRTUALSCREEN),
        user32.GetSystemMetrics(SM_YVIRTUALSCREEN),
        user32.GetSystemMetrics(SM_CXVIRTUALSCREEN),
        user32.GetSystemMetrics(SM_CYVIRTUALSCREEN),
    )


MONITORINFOF_PRIMARY = 0x01
MONITORENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMONITOR,
                                     wintypes.HDC,
                                     ctypes.POINTER(wintypes.RECT),
                                     wintypes.LPARAM)


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


user32.EnumDisplayMonitors.argtypes = [wintypes.HDC, ctypes.c_void_p,
                                       MONITORENUMPROC, wintypes.LPARAM]
user32.EnumDisplayMonitors.restype = wintypes.BOOL
user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(MONITORINFO)]
user32.GetMonitorInfoW.restype = wintypes.BOOL


def monitores() -> list[tuple[int, int, int, int, bool]]:
    """[(x, y, largura, altura, e_o_principal)] de cada monitor.

    O retangulo do desktop virtual nao basta quando ha' mais de um monitor: com
    telas de alturas diferentes ou desalinhadas, partes desse retangulo nao
    existem em tela nenhuma. O Windows prende o cursor fora delas, e sem saber
    onde elas ficam a posicao que calculamos descola da real.
    """
    achados: list[tuple[int, int, int, int, bool]] = []

    def visitar(monitor, _hdc, _rect, _dados):
        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(MONITORINFO)
        if user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            r = info.rcMonitor
            achados.append((r.left, r.top, r.right - r.left, r.bottom - r.top,
                            bool(info.dwFlags & MONITORINFOF_PRIMARY)))
        return True

    if not user32.EnumDisplayMonitors(None, None, MONITORENUMPROC(visitar), 0):
        log.warning("EnumDisplayMonitors falhou; usando o desktop virtual inteiro")
    if not achados:
        x0, y0, largura, altura = geometria_virtual()
        achados.append((x0, y0, largura, altura, True))
    # Principal primeiro, depois da esquerda para a direita.
    achados.sort(key=lambda m: (not m[4], m[0], m[1]))
    return achados


def posicao_cursor() -> tuple[int, int]:
    ponto = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(ponto))
    return ponto.x, ponto.y


def mover_cursor(x: int, y: int) -> None:
    user32.SetCursorPos(int(x), int(y))


def modificador_pressionado(vk: int) -> bool:
    return bool(user32.GetAsyncKeyState(vk) & 0x8000)


def injecao_funciona() -> tuple[bool, str]:
    """Confere se SendInput realmente move o cursor desta maquina.

    O SendInput devolve sucesso mesmo quando o Windows descarta o evento -- e'
    o que acontece quando outro programa de teclado/mouse compartilhado (Mouse
    Without Borders, Synergy, ...) esta' no ar prendendo o mesmo caminho de
    input. Sem esta verificacao o cliente aceita o controle, injeta, e nada
    aparece na tela: falha silenciosa.

    Move o cursor 4 px e o devolve; nao interfere no uso.
    """
    origem = posicao_cursor()
    x0, y0, largura, altura = geometria_virtual()
    passo = 4 if origem[0] - x0 < largura - 8 else -4
    alvo = (origem[0] + passo, origem[1])
    injetor = Injetor()
    injetor.mover_para(*alvo)
    time.sleep(0.06)
    obtido = posicao_cursor()
    injetor.mover_para(*origem)
    if abs(obtido[0] - alvo[0]) <= 1 and abs(obtido[1] - alvo[1]) <= 1:
        return True, "injecao de mouse funcionando"
    return False, (f"o Windows descartou a injecao de mouse: pedi "
                   f"{alvo}, o cursor ficou em {obtido}")


# -- injecao ----------------------------------------------------------------


class Injetor:
    """Reproduz eventos recebidos da rede como se fossem input local."""

    def __init__(self):
        self.x0, self.y0, self.largura, self.altura = geometria_virtual()

    def _enviar(self, *entradas: INPUT) -> None:
        vetor = (INPUT * len(entradas))(*entradas)
        enviados = user32.SendInput(len(entradas), vetor, ctypes.sizeof(INPUT))
        if enviados != len(entradas):
            log.warning("SendInput recusou o evento (erro %d) -- falta elevacao?",
                        ctypes.get_last_error())

    def _mouse(self, flags: int, dx: int = 0, dy: int = 0, dados: int = 0) -> INPUT:
        entrada = INPUT(type=INPUT_MOUSE)
        entrada.mi = MOUSEINPUT(dx, dy, dados & 0xFFFFFFFF, flags, 0, MARCA)
        return entrada

    def mover_para(self, x: float, y: float) -> None:
        """Move para coordenada absoluta do desktop virtual."""
        nx = round((x - self.x0) * 65535 / max(1, self.largura - 1))
        ny = round((y - self.y0) * 65535 / max(1, self.altura - 1))
        flags = MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK
        self._enviar(self._mouse(flags, nx, ny))

    def botao(self, nome: str, pressionar: bool) -> None:
        if nome not in BOTOES:
            return
        baixo, alto, dados = BOTOES[nome]
        self._enviar(self._mouse(baixo if pressionar else alto, dados=dados))

    def roda(self, delta: int, horizontal: bool = False) -> None:
        flags = MOUSEEVENTF_HWHEEL if horizontal else MOUSEEVENTF_WHEEL
        self._enviar(self._mouse(flags, dados=delta))

    def tecla(self, vk: int, scan: int, estendida: bool, pressionar: bool) -> None:
        entrada = INPUT(type=INPUT_KEYBOARD)
        if scan:
            flags = KEYEVENTF_SCANCODE
            # `and scan != SCAN_SHIFT_DIREITO`: ver a nota do scancode 0x36.
            # A correcao fica aqui, e nao na captura, porque protege tambem os
            # eventos que vierem de um PC ainda na versao antiga.
            if estendida and scan != SCAN_SHIFT_DIREITO:
                flags |= KEYEVENTF_EXTENDEDKEY
        else:  # sem scancode utilizavel, cai para virtual-key
            flags = 0
        if not pressionar:
            flags |= KEYEVENTF_KEYUP
        entrada.ki = KEYBDINPUT(vk if not scan else 0, scan, flags, 0, MARCA)
        self._enviar(entrada)

    def soltar_modificadores(self) -> None:
        """Evita Ctrl/Alt/Shift/Win presos depois de uma troca de maquina.

        Solta apenas os que estao de fato pressionados: assim uma travessia
        normal nao gera oito keyups a cada vez.
        """
        for vk, scan, estendida in MODIFICADORES:
            if modificador_pressionado(vk):
                self.tecla(vk, scan, estendida, False)


# -- captura ----------------------------------------------------------------


class Captura(threading.Thread):
    """Hooks globais de mouse e teclado com message loop dedicado.

    `tratar(ev) -> bool`: recebe o evento normalizado e devolve True para
    bloquear (nao repassar as aplicacoes locais). Deve ser barato.
    """

    def __init__(self, tratar, ao_delta=None):
        super().__init__(name="captura", daemon=True)
        self.tratar = tratar
        # `ao_delta(dx, dy)` recebe o deslocamento relativo do Raw Input. E' o
        # unico caminho confiavel para mover o cursor do outro PC.
        self.ao_delta = ao_delta
        self._hooks: list = []
        # Guardar as referencias: se o CFUNCTYPE for coletado, o hook crasha.
        self._proc_mouse = HOOKPROC(self._mouse)
        self._proc_teclado = HOOKPROC(self._teclado)
        self._proc_janela = WNDPROC(self._janela)
        self.pronta = threading.Event()
        self.raw_ativo = False
        self._id_thread = 0
        self._hwnd = None
        self._buffer = ctypes.create_string_buffer(ctypes.sizeof(RAWINPUT) + 64)
        # Vigia: o Raw Input chega por mensagem de janela e nao depende dos
        # hooks. Se ele esta' recebendo movimento e o hook nao, o Windows
        # desinstalou o hook em silencio -- e' o que ele faz quando o callback
        # passa de ~300 ms, sem devolver erro nenhum.
        self._visto_raw = 0.0
        self._visto_hook = 0.0
        # Marcado pelo gancho do TECLADO. Nao serve para detectar morte (ver
        # INTERVALO_RENOVACAO), mas diz no log ha' quanto tempo ele nao e'
        # chamado -- que e' a informacao que faltava para diagnosticar isso.
        self._visto_teclado = 0.0
        self.reinstalacoes = 0
        self._motivo_reinstalacao = ""
        self._avisar_reinstalacao = False
        # O dono liga aqui: True enquanto ESTE PC comanda outro. Ver
        # INTERVALO_DISPUTA. Sem ninguem ligar, o comportamento nao muda.
        self.em_disputa = lambda: False

    # -- callbacks (executam na thread do hook, tem de ser rapidos) ---------

    def _mouse(self, ncode, wparam, lparam):
        if ncode != 0:
            return user32.CallNextHookEx(None, ncode, wparam, lparam)
        self._visto_hook = time.monotonic()
        dados = ctypes.cast(lparam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
        # Eventos injetados incluem os gerados pelo nosso proprio SetCursorPos.
        if dados.flags & LLMHF_INJECTED or dados.dwExtraInfo == MARCA:
            return user32.CallNextHookEx(None, ncode, wparam, lparam)

        ev = None
        if wparam == WM_MOUSEMOVE:
            ev = {"t": "mv", "pos": (dados.pt.x, dados.pt.y)}
        elif wparam in (WM_LBUTTONDOWN, WM_LBUTTONUP):
            ev = {"t": "btn", "b": "esq", "down": wparam == WM_LBUTTONDOWN}
        elif wparam in (WM_RBUTTONDOWN, WM_RBUTTONUP):
            ev = {"t": "btn", "b": "dir", "down": wparam == WM_RBUTTONDOWN}
        elif wparam in (WM_MBUTTONDOWN, WM_MBUTTONUP):
            ev = {"t": "btn", "b": "meio", "down": wparam == WM_MBUTTONDOWN}
        elif wparam in (WM_XBUTTONDOWN, WM_XBUTTONUP):
            qual = "x2" if (dados.mouseData >> 16) == 2 else "x1"
            ev = {"t": "btn", "b": qual, "down": wparam == WM_XBUTTONDOWN}
        elif wparam in (WM_MOUSEWHEEL, WM_MOUSEHWHEEL):
            delta = ctypes.c_short(dados.mouseData >> 16).value
            ev = {"t": "whl", "d": delta, "h": wparam == WM_MOUSEHWHEEL}

        if ev is not None:
            try:
                if self.tratar(ev):
                    return 1
            except Exception:
                log.error("erro no tratamento de evento de mouse", exc_info=True)
        return user32.CallNextHookEx(None, ncode, wparam, lparam)

    def _teclado(self, ncode, wparam, lparam):
        if ncode != 0:
            return user32.CallNextHookEx(None, ncode, wparam, lparam)
        # Marca ANTES do filtro da MARCA: o que interessa aqui e' que o gancho
        # foi chamado, mesmo que o evento seja nosso e va' ser ignorado adiante.
        self._visto_teclado = time.monotonic()
        dados = ctypes.cast(lparam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
        if dados.dwExtraInfo == MARCA:
            return user32.CallNextHookEx(None, ncode, wparam, lparam)

        ev = {
            "t": "key",
            "vk": dados.vkCode,
            "sc": dados.scanCode,
            "ext": bool(dados.flags & LLKHF_EXTENDED),
            "down": wparam in (WM_KEYDOWN, WM_SYSKEYDOWN),
        }
        try:
            if self.tratar(ev):
                return 1
        except Exception:
            log.error("erro no tratamento de evento de teclado", exc_info=True)
        return user32.CallNextHookEx(None, ncode, wparam, lparam)

    # -- thread ------------------------------------------------------------

    # -- Raw Input (deslocamento relativo) ---------------------------------

    def _janela(self, hwnd, mensagem, wparam, lparam):
        if mensagem == WM_INPUT and self.ao_delta is not None:
            tamanho = wintypes.UINT(len(self._buffer))
            lidos = user32.GetRawInputData(
                wintypes.HANDLE(lparam), RID_INPUT, self._buffer,
                ctypes.byref(tamanho), ctypes.sizeof(RAWINPUTHEADER))
            if lidos and lidos != 0xFFFFFFFF:
                dados = ctypes.cast(self._buffer,
                                    ctypes.POINTER(RAWINPUT)).contents
                if dados.header.dwType == RIM_TYPEMOUSE:
                    rato = dados.mouse
                    # Mesas digitalizadoras e alguns KVM reportam absoluto;
                    # desses nao da' para tirar deslocamento.
                    if not (rato.usFlags & MOUSE_MOVE_ABSOLUTE):
                        if rato.lLastX or rato.lLastY:
                            self._visto_raw = time.monotonic()
                            try:
                                self.ao_delta(rato.lLastX, rato.lLastY)
                            except Exception:
                                log.error("erro ao tratar delta bruto",
                                          exc_info=True)
        return user32.DefWindowProcW(hwnd, mensagem, wparam, lparam)

    def _registrar_raw(self) -> None:
        """Cria uma janela so' de mensagens e pede o mouse bruto para ela."""
        classe = WNDCLASS()
        classe.lpfnWndProc = self._proc_janela
        classe.hInstance = kernel32.GetModuleHandleW(None)
        classe.lpszClassName = CLASSE_RAW
        if not user32.RegisterClassW(ctypes.byref(classe)):
            erro = ctypes.get_last_error()
            if erro != ERRO_CLASSE_JA_EXISTE:
                log.warning("RegisterClass falhou (erro %d)", erro)
                return
        self._hwnd = user32.CreateWindowExW(
            0, CLASSE_RAW, "Multi PC - KVM", 0, 0, 0, 0, 0,
            wintypes.HWND(HWND_MESSAGE), None, classe.hInstance, None)
        if not self._hwnd:
            log.warning("nao consegui criar a janela de mensagens (erro %d)",
                        ctypes.get_last_error())
            return

        # A classe de janela vale para o PROCESSO inteiro e guarda o WNDPROC de
        # quem a registrou. Parar e reiniciar o servidor cria uma Captura nova,
        # mas a classe da anterior sobrevive: o RegisterClass acima falha com
        # ERROR_CLASS_ALREADY_EXISTS -- que toleramos -- e a janela nasce
        # apontando para o WNDPROC da instancia MORTA. O WM_INPUT ia entao para
        # um objeto cujo `ao_delta` nao comanda mais nada, e o movimento do mouse
        # sumia em silencio. O teclado continuava funcionando, porque os hooks
        # sao reinstalados a cada instancia -- era exatamente esse o sintoma:
        # "religou o servidor, o mouse trava e so' o teclado funciona".
        #
        # Amarrar o WNDPROC A' JANELA, e nao a' classe, torna a instancia certa
        # a dona das mensagens, independente de qual delas registrou a classe.
        _definir_wndproc(self._hwnd, GWLP_WNDPROC, self._proc_janela)

        dispositivo = RAWINPUTDEVICE(PAGINA_GENERICA, USO_MOUSE, RIDEV_INPUTSINK,
                                     self._hwnd)
        if not user32.RegisterRawInputDevices(ctypes.byref(dispositivo), 1,
                                              ctypes.sizeof(RAWINPUTDEVICE)):
            log.warning("RegisterRawInputDevices falhou (erro %d)",
                        ctypes.get_last_error())
            return
        self.raw_ativo = True
        log.info("Raw Input do mouse registrado (deslocamento relativo)")

    def parar(self) -> None:
        """Encerra o message loop, o que desinstala os hooks.

        Sem isto, parar e reiniciar o servidor deixaria a instalacao antiga no
        lugar e o input passaria por dois conjuntos de hooks.
        """
        if self._id_thread:
            user32.PostThreadMessageW(self._id_thread, 0x0012, 0, 0)  # WM_QUIT
            self.join(timeout=3)

    def _decidir(self, agora: float, renovado_em: float) -> tuple[str, bool] | None:
        """(motivo, avisar) se e' hora de reinstalar; None se esta' tudo bem.

        Separado do laco para poder ser testado sem thread nem espera real.
        """
        atraso = self._visto_raw - self._visto_hook
        if self._visto_raw and atraso > TOLERANCIA_VIGIA:
            # Este e' o caso ANORMAL: o Raw Input recebe movimento e o gancho do
            # mouse nao. Merece aviso no log.
            return f"o gancho do mouse esta' {atraso:.1f}s atras do Raw Input", True
        try:
            disputando = bool(self.em_disputa())
        except Exception:
            disputando = False
        intervalo = INTERVALO_DISPUTA if disputando else INTERVALO_RENOVACAO
        if agora - renovado_em >= intervalo:
            # Rotina: nao ha' nada de errado detectado, e e' justamente por isso
            # que ela existe -- nem a morte do gancho do teclado nem a perda do
            # primeiro lugar na fila sao detectaveis daqui.
            return ("renovacao rapida (comandando)" if disputando
                    else "renovacao periodica"), False
        return None

    def _vigiar(self) -> None:
        """Mantem os dois ganchos vivos: por deteccao e por renovacao periodica."""
        pausa = threading.Event()
        renovado_em = time.monotonic()
        while not pausa.wait(INTERVALO_VIGIA):
            if not self._hooks:
                return
            agora = time.monotonic()
            decisao = self._decidir(agora, renovado_em)
            if decisao is None:
                continue
            motivo, avisar = decisao
            if avisar:
                # Zera a divergencia para nao repetir o aviso a cada 2 s enquanto
                # a reinstalacao nao surtir efeito.
                self._visto_hook = self._visto_raw
            self.reinstalacoes += 1
            self._motivo_reinstalacao = motivo
            self._avisar_reinstalacao = avisar
            renovado_em = agora
            # Reinstalar tem de ser na thread dos hooks: e' dela que eles sao.
            user32.PostThreadMessageW(self._id_thread, MSG_REINSTALAR, 0, 0)

    @staticmethod
    def _idade(marca: float) -> str:
        return "nunca" if not marca else f"{time.monotonic() - marca:.0f}s"

    def _instalar_hooks(self) -> bool:
        # hMod tem de ser NULL nos hooks de baixo nivel: o callback vive num
        # thunk do ctypes, nao dentro de um modulo. Passar o handle do .exe da'
        # ERROR_MOD_NOT_FOUND (126).
        novos = []
        for tipo, proc in ((WH_MOUSE_LL, self._proc_mouse),
                           (WH_KEYBOARD_LL, self._proc_teclado)):
            handle = user32.SetWindowsHookExW(tipo, proc, None, 0)
            if not handle:
                for feito in novos:
                    user32.UnhookWindowsHookEx(feito)
                log.error("SetWindowsHookEx(%d) falhou: erro %d", tipo,
                          ctypes.get_last_error())
                return False
            novos.append(handle)
        for antigo in self._hooks:
            user32.UnhookWindowsHookEx(antigo)
        self._hooks = novos
        return True

    def run(self) -> None:
        self._id_thread = kernel32.GetCurrentThreadId()
        if self.ao_delta is not None:
            self._registrar_raw()
        if not self._instalar_hooks():
            raise OSError("nao consegui instalar os hooks de mouse e teclado")
        self.pronta.set()
        log.info("hooks de mouse e teclado instalados")
        threading.Thread(target=self._vigiar, name="vigia-hooks",
                         daemon=True).start()

        # Hooks de baixo nivel exigem um message loop na thread que os instalou.
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == MSG_REINSTALAR:
                # Reinstalar tem de acontecer nesta thread: e' dela que os hooks
                # sao, e e' ela que roda o message loop.
                if self._instalar_hooks():
                    # A idade dos dois ganchos vai no log de proposito: e' o que
                    # permite ver, depois do fato, que o teclado estava morto
                    # enquanto o mouse seguia atendendo.
                    registrar = (log.warning if self._avisar_reinstalacao
                                 else log.debug)
                    registrar("ganchos reinstalados (%s; %da vez) -- mouse visto "
                              "ha' %s, teclado ha' %s", self._motivo_reinstalacao,
                              self.reinstalacoes, self._idade(self._visto_hook),
                              self._idade(self._visto_teclado))
                continue
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

        for handle in self._hooks:
            user32.UnhookWindowsHookEx(handle)
        self._hooks.clear()
        if self._hwnd:
            user32.DestroyWindow(self._hwnd)
            self._hwnd = None
            # Cancelar o registro tambem, e nao so' destruir a janela: a classe
            # sobrevive ao fim da thread e continua guardando o ponteiro para o
            # WNDPROC desta instancia. Se o objeto for coletado, o ponteiro fica
            # pendurado -- e o proximo CreateWindowEx nasceria apontando para
            # memoria liberada.
            user32.UnregisterClassW(CLASSE_RAW, kernel32.GetModuleHandleW(None))
        self.raw_ativo = False
