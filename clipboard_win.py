"""Area de transferencia do 2pc_1Kit: texto (CF_UNICODETEXT) e imagem (CF_DIB).

A deteccao de mudanca usa polling de GetClipboardSequenceNumber(), que e' bem
mais simples que AddClipboardFormatListener (exigiria uma janela oculta com
message loop) e suficiente para uso interativo.

Imagens viajam como PNG (o DIB cru de um print de tela 4K passa de 30 MB).
"""

from __future__ import annotations

import base64
import hashlib
import io
import logging
import struct
import threading
import time

import pywintypes
import win32clipboard as wcb
import win32con
from PIL import Image

log = logging.getLogger("clipboard")

TETO_IMAGEM = 8 * 1024 * 1024  # PNG maior que isso e' descartado
INTERVALO = 0.3  # segundos entre verificacoes


# O clipboard aberto pertence ao *processo*, nao a' thread: se a thread de rede
# abre e fecha enquanto o poller esta' lendo, o poller leva erro 1418. Um lock
# no processo inteiro resolve.
_LOCK = threading.RLock()


class _Aberto:
    """Context manager que tenta abrir o clipboard (outro app pode ter o lock)."""

    def __init__(self, tentativas: int = 10):
        self.tentativas = tentativas

    def __enter__(self):
        _LOCK.acquire()
        for _ in range(self.tentativas):
            try:
                wcb.OpenClipboard()
                return self
            except Exception:  # pywintypes.error quando outro app detem o lock
                time.sleep(0.05)
        _LOCK.release()
        raise OSError("nao foi possivel abrir a area de transferencia")

    def __exit__(self, *exc):
        try:
            wcb.CloseClipboard()
        except Exception:
            pass
        finally:
            _LOCK.release()
        return False


# -- conversao DIB <-> PNG --------------------------------------------------


def _dib_para_png(dib: bytes) -> bytes:
    """Prefixa um BITMAPFILEHEADER no DIB para o Pillow conseguir abrir."""
    tamanho_cabecalho = int.from_bytes(dib[0:4], "little")
    bpp = int.from_bytes(dib[14:16], "little")
    compressao = int.from_bytes(dib[16:20], "little")
    cores_usadas = int.from_bytes(dib[32:36], "little")

    if bpp <= 8:
        paleta = (cores_usadas or (1 << bpp)) * 4
    else:
        paleta = 0
    if compressao == 3:  # BI_BITFIELDS: 3 mascaras de 4 bytes
        paleta += 12

    inicio_pixels = 14 + tamanho_cabecalho + paleta
    bmp = b"BM" + struct.pack("<IHHI", 14 + len(dib), 0, 0, inicio_pixels) + dib

    imagem = Image.open(io.BytesIO(bmp)).convert("RGB")
    saida = io.BytesIO()
    imagem.save(saida, "PNG", compress_level=6)
    return saida.getvalue()


def _png_para_dib(png: bytes) -> bytes:
    imagem = Image.open(io.BytesIO(png)).convert("RGB")
    saida = io.BytesIO()
    imagem.save(saida, "BMP")
    return saida.getvalue()[14:]  # remove o BITMAPFILEHEADER


# -- leitura / escrita ------------------------------------------------------


def ler(tentativas: int = 3) -> dict | None:
    """Le o conteudo atual. Devolve a mensagem de protocolo pronta, ou None.

    Outro processo pode fechar o clipboard no meio da nossa leitura (erro 1418).
    Isso e' transitorio e normal: basta repetir.
    """
    for n in range(tentativas):
        try:
            return _ler_uma_vez()
        except (OSError, pywintypes.error):
            if n == tentativas - 1:
                raise
            time.sleep(0.05)
    return None


def _ler_uma_vez() -> dict | None:
    with _Aberto():
        if wcb.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT):
            texto = wcb.GetClipboardData(win32con.CF_UNICODETEXT)
            if texto:
                return {"t": "clip", "fmt": "texto", "dados": texto}
        if wcb.IsClipboardFormatAvailable(win32con.CF_DIB):
            dib = wcb.GetClipboardData(win32con.CF_DIB)
        else:
            return None

    # Converter fora do lock: o Pillow pode demorar em imagens grandes.
    try:
        png = _dib_para_png(dib)
    except Exception:
        log.warning("imagem no formato DIB nao pudemos converter", exc_info=True)
        return None
    if len(png) > TETO_IMAGEM:
        log.info("imagem de %.1f MB ignorada (teto de %.0f MB)",
                 len(png) / 1e6, TETO_IMAGEM / 1e6)
        return None
    return {"t": "clip", "fmt": "imagem", "dados": base64.b64encode(png).decode()}


def escrever(msg: dict) -> None:
    """Aplica no clipboard local uma mensagem `clip` vinda da rede."""
    if msg["fmt"] == "texto":
        formato, dados = win32con.CF_UNICODETEXT, msg["dados"]
    else:
        formato = win32con.CF_DIB
        dados = _png_para_dib(base64.b64decode(msg["dados"]))

    with _Aberto():
        wcb.EmptyClipboard()
        wcb.SetClipboardData(formato, dados)


def impressao(msg: dict) -> str:
    """Hash do conteudo, usado para nao devolver ao outro PC o que veio dele."""
    dados = msg["dados"]
    bruto = dados.encode("utf-8") if isinstance(dados, str) else dados
    return hashlib.sha256(msg["fmt"].encode() + b"\0" + bruto).hexdigest()


# -- thread de sincronizacao ------------------------------------------------


class Sincronizador(threading.Thread):
    """Observa o clipboard local e publica mudancas via `enviar`.

    `aplicar()` deve ser chamado pela thread de rede quando chega um `clip`:
    ele grava no clipboard local e memoriza a impressao para o proximo ciclo
    do poller nao reenviar o mesmo conteudo de volta (eco infinito).
    """

    def __init__(self, enviar, parar: threading.Event):
        super().__init__(name="clipboard", daemon=True)
        self.enviar = enviar
        self.parar = parar
        self._ultima = None
        self._sequencia = None
        self._lock = threading.Lock()

    def aplicar(self, msg: dict) -> None:
        # O lock fica preso durante toda a escrita: se o poller rodasse no meio,
        # veria o conteudo novo e o devolveria ao outro PC (eco).
        with self._lock:
            self._ultima = impressao(msg)
            try:
                escrever(msg)
            except Exception:
                log.warning("falha ao gravar no clipboard", exc_info=True)
                return
            # A nossa propria escrita incrementa a sequencia; absorve-la aqui.
            self._sequencia = wcb.GetClipboardSequenceNumber()
            if msg["fmt"] == "imagem":
                # Reler: o PNG que sai do clipboard pode nao ser byte a byte o
                # que entrou, e a impressao precisa bater no proximo ciclo.
                try:
                    relido = ler()
                    if relido is not None:
                        self._ultima = impressao(relido)
                except Exception:
                    log.debug("nao foi possivel reler a imagem aplicada",
                              exc_info=True)
        log.info("clipboard recebido (%s)", msg["fmt"])

    def run(self) -> None:
        self._sequencia = wcb.GetClipboardSequenceNumber()
        while not self.parar.wait(INTERVALO):
            try:
                sequencia = wcb.GetClipboardSequenceNumber()
                with self._lock:
                    if sequencia == self._sequencia:
                        continue
                # A sequencia so' e' confirmada depois de uma leitura que deu
                # certo -- senao uma falha transitoria perderia a mudanca.
                msg = ler()
                marca = impressao(msg) if msg is not None else None
                with self._lock:
                    self._sequencia = sequencia
                    if msg is None or marca == self._ultima:
                        continue
                    self._ultima = marca
                self.enviar(msg)
                log.info("clipboard enviado (%s)", msg["fmt"])
            except Exception:
                log.debug("erro no ciclo do clipboard", exc_info=True)
