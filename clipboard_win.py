"""Area de transferencia do Multi PC - KVM: texto (CF_UNICODETEXT) e imagem (CF_DIB).

A deteccao de mudanca usa polling de GetClipboardSequenceNumber(), que e' bem
mais simples que AddClipboardFormatListener (exigiria uma janela oculta com
message loop) e suficiente para uso interativo.

Imagens viajam como PNG (o DIB cru de um print de tela 4K passa de 30 MB).
"""

from __future__ import annotations

import base64
import contextlib
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

import arquivos

log = logging.getLogger("clipboard")

TETO_IMAGEM = 8 * 1024 * 1024  # PNG maior que isso e' descartado
INTERVALO = 0.3  # segundos entre verificacoes
# Publicar no clipboard nao e' atomico para quem observa: da' para pegar o
# instante em que so' os formatos proprios do programa entraram e o texto
# ainda nao. Visto no log de 31/08/2026 -- "formato que nao sei mandar" e,
# no MESMO segundo, o texto chegando. Naquela vez recuperou porque o segundo
# SetClipboardData mexeu na sequencia de novo; quando nao mexe, a copia se
# perde inteira e para quem copiou o programa simplesmente parou.
TENTATIVAS_DE_LEITURA = 5
ESPERA_ENTRE_LEITURAS = 0.15


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


# Nomes dos formatos padrao, para o log dizer O QUE foi copiado quando nao
# soubermos levar. Sem isto a linha "nao sei mandar" nao leva a lugar nenhum.
_FORMATOS_PADRAO = {
    win32con.CF_TEXT: "CF_TEXT",
    win32con.CF_BITMAP: "CF_BITMAP",
    win32con.CF_METAFILEPICT: "CF_METAFILEPICT",
    win32con.CF_OEMTEXT: "CF_OEMTEXT",
    win32con.CF_DIB: "CF_DIB",
    win32con.CF_PALETTE: "CF_PALETTE",
    win32con.CF_UNICODETEXT: "CF_UNICODETEXT",
    win32con.CF_ENHMETAFILE: "CF_ENHMETAFILE",
    win32con.CF_HDROP: "CF_HDROP",
    win32con.CF_LOCALE: "CF_LOCALE",
    win32con.CF_DIBV5: "CF_DIBV5",
}


def formatos_no_clipboard() -> list[str]:
    """O que esta' no clipboard agora, por nome. So' para o log."""
    nomes: list[str] = []
    try:
        with _Aberto(tentativas=3):
            formato = 0
            while True:
                formato = wcb.EnumClipboardFormats(formato)
                if not formato:
                    break
                nome = _FORMATOS_PADRAO.get(formato)
                if nome is None:
                    try:
                        nome = wcb.GetClipboardFormatName(formato)
                    except Exception:
                        nome = f"#{formato}"
                nomes.append(nome)
    except Exception:
        log.debug("nao consegui listar os formatos", exc_info=True)
    return nomes


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
        # CF_HDROP vem antes do texto: copiar arquivo no Explorer costuma deixar
        # tambem uma versao em texto (o caminho), e mandar o caminho em vez do
        # arquivo seria inutil do outro lado.
        if wcb.IsClipboardFormatAvailable(win32con.CF_HDROP):
            caminhos = wcb.GetClipboardData(win32con.CF_HDROP)
            if caminhos:
                return {"t": "clip", "fmt": "arquivos",
                        "caminhos": [str(c) for c in caminhos]}
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


def escrever(msg: dict, tentativas: int = 3) -> None:
    """Aplica no clipboard local uma mensagem `clip` vinda da rede.

    Como na leitura, outro processo pode mexer no clipboard no meio da escrita
    (`SetClipboardData` devolve 'identificador invalido'). E' transitorio.

    TEXTO VAI POR `SetClipboardText`, E NAO POR `SetClipboardData`.

    O programa fechava sozinho, sem nada no log, com corrupcao de heap. O Page
    Heap parou o processo no ato e o dump mostrou onde:

        user32!SetClipboardData
        user32!ConvertMemHandle
        KERNELBASE!GlobalFlags
        ntdll!RtlGetUserInfoHeap
        verifier!AVrfpDphFindBusyMemoryNoCheck   <- bloco ja' liberado

    O `SetClipboardData` estava recebendo um handle de memoria global cujo bloco
    ja' nao valia -- e quem alocou e passou esse handle foi o proprio pywin32,
    dentro da chamada. `SetClipboardText` percorre outro caminho no pywin32 para
    o mesmo fim.

    Nao esta' provado que isto elimina a queda; esta' provado que era ALI que ela
    acontecia. Manter o Page Heap ligado ate' confirmar.
    """
    escrever_texto = msg["fmt"] == "texto"
    if escrever_texto:
        formato, dados = win32con.CF_UNICODETEXT, msg["dados"]
    elif msg["fmt"] == "arquivos":
        formato = win32con.CF_HDROP
        dados = arquivos.montar_hdrop(msg["caminhos"])
    else:
        formato = win32con.CF_DIB
        dados = _png_para_dib(base64.b64decode(msg["dados"]))

    for n in range(tentativas):
        try:
            with _Aberto():
                wcb.EmptyClipboard()
                if escrever_texto:
                    wcb.SetClipboardText(dados, formato)
                else:
                    wcb.SetClipboardData(formato, dados)
            return
        except (OSError, pywintypes.error):
            if n == tentativas - 1:
                raise
            time.sleep(0.05)


def impressao(msg: dict) -> str:
    """Hash do conteudo, usado para nao devolver ao outro PC o que veio dele."""
    if msg["fmt"] == "arquivos":
        # A impressao e' dos CAMINHOS, nao do conteudo: ler MB de arquivo a cada
        # 0,3 s de polling so' para calcular hash seria absurdo. Caminho basta --
        # o clipboard guarda caminho, e e' ele que muda quando a selecao muda.
        bruto = "\0".join(msg["caminhos"]).encode("utf-8")
    else:
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
        self._recepcao = arquivos.Recepcao()
        self._falhas = 0  # ciclos que morreram na excecao, para o log ralear

    def aplicar_arquivo(self, msg: dict) -> None:
        """Consome uma mensagem 'arq'. Ao completar, poe os arquivos no clipboard.

        Separado de `aplicar` porque uma transferencia sao muitas mensagens, e o
        clipboard so' e' tocado na ultima -- antes disso nao ha' arquivo inteiro
        para colar.
        """
        prontos = self._recepcao.aplicar(msg)
        if prontos is None:
            return
        self.aplicar({"t": "clip", "fmt": "arquivos",
                      "caminhos": [str(c) for c in prontos]})

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

    def _enviar_arquivos(self, caminhos: list[str]) -> None:
        """Manda a selecao em blocos. Nada e' enviado se `preparar` recusar."""
        pronto = arquivos.preparar(caminhos)
        if pronto is None:
            return
        lista, total = pronto
        # O identificador nomeia a pasta no outro lado; o relogio basta, porque
        # ha' uma transferencia por vez e ela e' curta.
        identificador = f"{int(time.time() * 1000):x}"
        log.info("enviando %d arquivo(s), %.1f MB", len(lista), total / 1e6)
        # `closing` porque `mensagens` e' um generator que mantem o arquivo de
        # origem ABERTO entre dois blocos. Sair do laco no meio -- e' o que o
        # `parar` faz -- deixaria o handle pendurado ate' o coletor passar, e
        # enquanto isso ninguem consegue mover nem apagar o arquivo do usuario.
        with contextlib.closing(
                arquivos.mensagens(lista, total, identificador)) as fluxo:
            for mensagem in fluxo:
                if self.parar.is_set():
                    self.enviar({"t": "arq", "e": "aborta", "id": identificador,
                                 "motivo": "o programa esta' encerrando"})
                    return
                self.enviar(mensagem)
        log.info("clipboard enviado (arquivos)")

    def run(self) -> None:
        self._sequencia = wcb.GetClipboardSequenceNumber()
        # Nascimento e morte no log porque "o clipboard parou" quase sempre e'
        # esta thread nao estando no ar -- no cliente ela e' de UMA conexao, e
        # cada queda leva a dela junto.
        log.info("sincronizador do clipboard no ar")
        try:
            self._laco()
        finally:
            log.info("sincronizador do clipboard encerrado")

    def _ler_com_paciencia(self) -> dict | None:
        """Le o clipboard dando tempo a quem esta' publicando.

        Uma olhada so' pega o meio da publicacao e devolve None; dai' a
        sequencia e' confirmada e a copia se perde para sempre. Reler algumas
        vezes custa alguns centesimos e resolve o caso comum, que e' o programa
        publicar o formato dele primeiro e o texto logo depois.
        """
        for n in range(TENTATIVAS_DE_LEITURA):
            msg = ler()
            if msg is not None:
                if n:
                    log.debug("clipboard so' ficou legivel na %da olhada", n + 1)
                return msg
            if n < TENTATIVAS_DE_LEITURA - 1:
                time.sleep(ESPERA_ENTRE_LEITURAS)
        return None

    def _laco(self) -> None:
        while not self.parar.wait(INTERVALO):
            try:
                sequencia = wcb.GetClipboardSequenceNumber()
                with self._lock:
                    if sequencia == self._sequencia:
                        continue
                # A sequencia so' e' confirmada depois de uma leitura que deu
                # certo -- senao uma falha transitoria perderia a mudanca.
                msg = self._ler_com_paciencia()
                marca = impressao(msg) if msg is not None else None
                with self._lock:
                    self._sequencia = sequencia
                    if msg is None or marca == self._ultima:
                        if msg is None:
                            # Copiaram algo que nao sabemos levar (formato
                            # proprio de um programa, por exemplo). Fica no
                            # log: do lado de la' a queixa e' "copiei e nao
                            # colou", e sem esta linha nao ha' por onde comecar.
                            log.info("copiaram um formato que nao sei mandar; "
                                     "nada foi enviado (no clipboard: %s)",
                                     ", ".join(formatos_no_clipboard())
                                     or "nada")
                        continue
                    anterior = self._ultima
                    self._ultima = marca
                try:
                    if msg["fmt"] == "arquivos":
                        self._enviar_arquivos(msg["caminhos"])
                    else:
                        self.enviar(msg)
                        log.info("clipboard enviado (%s)", msg["fmt"])
                except Exception:
                    # A impressao VOLTA a ser a de antes. Sem isto, um envio
                    # que falha (a conexao caiu no exato momento) deixa gravada
                    # a impressao do que nao foi -- e copiar A MESMA COISA de
                    # novo, que e' o que a pessoa faz quando percebe que nao
                    # colou, bate com essa impressao e e' ignorado. Para o
                    # usuario o clipboard simplesmente "parou de funcionar", e
                    # so' volta quando ele copia outra coisa qualquer.
                    with self._lock:
                        if self._ultima == marca:
                            self._ultima = anterior
                    raise
                self._falhas = 0
            except Exception:
                # Em DEBUG isto era invisivel, e invisivel e' o pior lugar para
                # esta falha: o clipboard simplesmente nao atravessa e nao ha'
                # uma linha sequer a que se agarrar. Ralear o log evita encher
                # o arquivo quando a causa e' permanente.
                self._falhas += 1
                if self._falhas == 1 or self._falhas % 20 == 0:
                    log.warning("ciclo do clipboard falhou (%da vez); a copia "
                                "nao atravessou", self._falhas, exc_info=True)
