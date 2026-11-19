"""Audio entre os PCs: ouvir, num PC, o que outro esta' tocando.

Captura: WASAPI loopback (o que o PC manda para os alto-falantes), via
PyAudioWPatch. Transporte: UDP proprio na porta 24812, e NAO o canal TCP de
controle -- um pacote de som atrasado ali travaria mouse e teclado atras dele
(head-of-line blocking). No UDP um pacote perdido vira um estalo e o controle
segue intacto.

Cada pacote vai cifrado com AES-GCM, chave derivada da chave compartilhada do
config.json. O nonce e' o cabecalho em claro (id da fonte + sequencia), e
tambem entra como dado autenticado: pacote adulterado, de outra chave ou
repetido e' descartado.

Cabecalho (12 bytes): id da fonte (4, sorteado a cada captura) | sequencia (8).
Texto cifrado: tamanho do nome (1) | nome do PC | taxa (4) | canais (1) | PCM
int16. A fonte manda no formato que o Windows captura; quem ouve abre a saida
com o mesmo formato.

O loopback NAO entrega quadro nenhum enquanto o PC esta' mudo. Isto e' normal:
quem ouve trata falta de pacote como silencio.
"""

from __future__ import annotations

import array
import hashlib
import logging
import os
import queue
import socket
import struct
import threading
import time
from collections import deque

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

log = logging.getLogger("audio")

PORTA = 24812
CABECALHO = struct.Struct(">4sQ")      # id da fonte, sequencia
FORMATO = struct.Struct(">IB")         # taxa, canais
PCM_POR_PACOTE = 1200                  # bytes: cabe no MTU de 1500 com folga
PRE_BUFFER_MS = 60                     # reserva contra jitter antes de tocar
BUFFER_MAXIMO_MS = 250                 # acima disto joga fora o mais antigo
SEM_NOTICIA = 5.0                      # s sem pacote: a fonte sumiu


# -- pacote ------------------------------------------------------------------


def chave_do_audio(segredo: str) -> bytes:
    """Chave AES-256 derivada do segredo, separada da chave do canal de controle."""
    return hashlib.sha256(b"multipc-kvm|audio|" + segredo.encode("utf-8")).digest()


def empacotar(chave: bytes, fonte_id: bytes, seq: int, nome: str, taxa: int,
              canais: int, pcm: bytes) -> bytes:
    cab = CABECALHO.pack(fonte_id, seq)
    rotulo = nome.encode("utf-8")[:255]
    claro = bytes([len(rotulo)]) + rotulo + FORMATO.pack(taxa, canais) + pcm
    return cab + AESGCM(chave).encrypt(cab, claro, cab)


def desempacotar(chave: bytes, pacote: bytes):
    """(fonte_id, seq, nome, taxa, canais, pcm), ou None se nao for de confianca."""
    if len(pacote) < CABECALHO.size + 16:
        return None
    cab = pacote[:CABECALHO.size]
    try:
        claro = AESGCM(chave).decrypt(cab, pacote[CABECALHO.size:], cab)
        fonte_id, seq = CABECALHO.unpack(cab)
        n = claro[0]
        nome = claro[1:1 + n].decode("utf-8")
        taxa, canais = FORMATO.unpack_from(claro, 1 + n)
    except (InvalidTag, ValueError, IndexError, struct.error, UnicodeDecodeError):
        return None
    pcm = claro[1 + n + FORMATO.size:]
    if canais < 1 or taxa < 8000 or len(pcm) % (canais * 2):
        return None
    return fonte_id, seq, nome, taxa, canais, pcm


# -- backends do Windows (trocaveis nos testes) ------------------------------


class _Captura:
    """Loopback do dispositivo de saida padrao, em modo callback."""

    def __init__(self, ao_receber):
        import pyaudiowpatch as pa  # aqui, e nao no topo: o programa abre sem som
        self._pa = pa.PyAudio()
        lb = self._pa.get_default_wasapi_loopback()
        self.taxa = int(lb["defaultSampleRate"])
        capturados = int(lb["maxInputChannels"])
        # Som surround (5.1, 7.1) vira estereo aqui: so' os dois primeiros
        # canais (frente esquerda e direita). Quem ouve quase sempre tem saida
        # estereo, e uma saida nao abre com mais canais do que o dispositivo tem.
        self.canais = min(capturados, 2)

        def callback(dados, quadros, tempo, estado):
            if dados:
                if capturados > 2:
                    todas = array.array("h")
                    todas.frombytes(dados)
                    esq, dir_ = todas[0::capturados], todas[1::capturados]
                    estereo = array.array("h", bytes(len(esq) * 4))
                    estereo[0::2], estereo[1::2] = esq, dir_
                    dados = estereo.tobytes()
                ao_receber(dados)
            return (None, pa.paContinue)

        self._stream = self._pa.open(
            format=pa.paInt16, channels=capturados, rate=self.taxa, input=True,
            input_device_index=lb["index"], frames_per_buffer=960,
            stream_callback=callback)

    def parar(self) -> None:
        for passo in (self._stream.stop_stream, self._stream.close,
                      self._pa.terminate):
            try:
                passo()
            except Exception:
                log.debug("falha ao fechar a captura", exc_info=True)


class _Saida:
    """Uma saida de audio no dispositivo padrao, que PUXA os bytes de `puxar`."""

    def __init__(self, taxa: int, canais: int, puxar):
        import pyaudiowpatch as pa
        self._pa = pa.PyAudio()
        info = self._pa.get_default_output_device_info()

        def callback(_dados, quadros, tempo, estado):
            return (puxar(quadros * canais * 2), pa.paContinue)

        self._stream = self._pa.open(
            format=pa.paInt16, channels=canais, rate=taxa, output=True,
            output_device_index=info["index"], frames_per_buffer=480,
            stream_callback=callback)

    def parar(self) -> None:
        for passo in (self._stream.stop_stream, self._stream.close,
                      self._pa.terminate):
            try:
                passo()
            except Exception:
                log.debug("falha ao fechar a saida", exc_info=True)


# -- o conjunto que cada papel (servidor ou cliente) carrega ------------------


class Audio:
    """A fonte e o ouvinte deste PC. Nada abre dispositivo ate' ser pedido."""

    def __init__(self, cfg: dict, eu: str):
        self.ouvinte = Ouvinte(cfg.get("chave", ""))
        self.ouvinte.volume = max(0, min(100, int(cfg.get("volume_audio", 100)))) / 100
        self.fonte = Fonte(cfg.get("chave", ""), eu)

    def ouvir(self, fontes) -> None:
        self.ouvinte.definir(fontes)

    def volume(self, valor: float) -> None:
        self.ouvinte.volume = max(0.0, min(1.0, valor))

    def enviar_para(self, destinos) -> None:
        self.fonte.definir_destinos(destinos)

    def renomear(self, eu: str) -> None:
        self.fonte.nome = eu

    def parar(self) -> None:
        self.fonte.parar()
        self.ouvinte.parar()


# -- fonte: manda o que este PC toca -----------------------------------------


class Fonte:
    """Captura o som deste PC e o manda, por UDP, a quem pediu."""

    def __init__(self, segredo: str, nome: str, abrir_captura=_Captura):
        self._chave = chave_do_audio(segredo)
        self.nome = nome
        self._abrir = abrir_captura
        self._destinos: list[tuple[str, int]] = []
        self._lock = threading.Lock()
        self._captura = None
        self._fila: queue.Queue = queue.Queue(maxsize=64)
        self._thread: threading.Thread | None = None
        self._id = b""
        self._seq = 0
        self._sock: socket.socket | None = None
        self.enviados = 0

    def definir_destinos(self, destinos) -> None:
        """Quem esta' ouvindo agora. Lista vazia para a captura."""
        destinos = [(str(ip), int(porta)) for ip, porta in destinos]
        with self._lock:
            self._destinos = destinos
        if destinos:
            self._ligar()
        else:
            self._desligar()

    def ativa(self) -> bool:
        return self._captura is not None

    def parar(self) -> None:
        self._desligar()

    def _ligar(self) -> None:
        with self._lock:
            if self._captura is not None:
                return
            try:
                self._captura = self._abrir(self._receber)
            except Exception as exc:
                log.warning("nao consegui capturar o audio deste PC: %s", exc)
                self._captura = None
                return
            self._id = os.urandom(4)
            self._seq = 0
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._thread = threading.Thread(target=self._enviar, args=(self._captura,),
                                            name="audio-tx", daemon=True)
            self._thread.start()
        log.info("audio: enviando o som deste PC (%d Hz, %d canais) para %s",
                 self._captura.taxa, self._captura.canais,
                 ", ".join(f"{ip}" for ip, _ in self._destinos))

    def _desligar(self) -> None:
        with self._lock:
            captura, self._captura = self._captura, None
            sock, self._sock = self._sock, None
        if captura is None:
            return
        captura.parar()
        self._fila.put(None)  # acorda a thread de envio
        if sock is not None:
            sock.close()
        log.info("audio: parei de enviar o som deste PC")

    def _receber(self, dados: bytes) -> None:
        """Roda na thread do WASAPI: so' enfileira, nunca bloqueia."""
        try:
            self._fila.put_nowait(dados)
        except queue.Full:
            pass  # preferivel um buraco a atrasar tudo

    def _enviar(self, captura) -> None:
        quadro = captura.canais * 2
        passo = (PCM_POR_PACOTE // quadro) * quadro
        while True:
            dados = self._fila.get()
            if dados is None or self._captura is not captura:
                return
            for i in range(0, len(dados), passo):
                with self._lock:
                    destinos, sock = list(self._destinos), self._sock
                if sock is None:
                    return
                pacote = empacotar(self._chave, self._id, self._seq, self.nome,
                                   captura.taxa, captura.canais, dados[i:i + passo])
                self._seq += 1
                for destino in destinos:
                    try:
                        sock.sendto(pacote, destino)
                    except OSError:
                        pass  # destino sumiu: o pedido sera' refeito pelo servidor
                self.enviados += 1


# -- ouvinte: toca o que os outros PCs mandam --------------------------------


class _Canal:
    """O som de UMA fonte: buffer de jitter e a saida que o toca."""

    def __init__(self, nome: str, taxa: int, canais: int, abrir_saida, volume):
        self.nome, self.taxa, self.canais = nome, taxa, canais
        self.ultimo_seq = -1
        self.visto = time.monotonic()
        self._fila: deque[bytes] = deque()
        self._bytes = 0
        self._tocando = False
        self._lock = threading.Lock()
        self._volume = volume
        por_ms = taxa * canais * 2 // 1000
        self._pre = por_ms * PRE_BUFFER_MS
        self._teto = por_ms * BUFFER_MAXIMO_MS
        self.saida = abrir_saida(taxa, canais, self._puxar)

    def empurrar(self, pcm: bytes) -> None:
        with self._lock:
            self._fila.append(pcm)
            self._bytes += len(pcm)
            while self._bytes > self._teto and len(self._fila) > 1:
                self._bytes -= len(self._fila.popleft())  # latencia nao pode crescer
            if not self._tocando and self._bytes >= self._pre:
                self._tocando = True

    def _puxar(self, quantos: int) -> bytes:
        """Roda na thread do WASAPI. Falta de dado vira silencio, nunca erro."""
        with self._lock:
            if not self._tocando or self._bytes < quantos:
                if self._tocando:
                    self._tocando = False  # esvaziou: reencher antes de voltar
                return bytes(quantos)
            saida = bytearray()
            while len(saida) < quantos:
                pedaco = self._fila.popleft()
                faltam = quantos - len(saida)
                if len(pedaco) > faltam:
                    self._fila.appendleft(pedaco[faltam:])
                    pedaco = pedaco[:faltam]
                saida += pedaco
            self._bytes -= quantos
        return _com_volume(bytes(saida), self._volume())

    def parar(self) -> None:
        self.saida.parar()


def _com_volume(pcm: bytes, volume: float) -> bytes:
    if volume >= 0.999:
        return pcm
    amostras = array.array("h")
    amostras.frombytes(pcm)
    for i, a in enumerate(amostras):
        amostras[i] = int(a * volume)
    return amostras.tobytes()


class Ouvinte:
    """Escuta a porta UDP e toca as fontes pedidas, uma saida para cada."""

    def __init__(self, segredo: str, abrir_saida=_Saida, porta: int = PORTA):
        self._chave = chave_do_audio(segredo)
        self._abrir_saida = abrir_saida
        self._porta = porta
        self._quer: set[str] = set()
        self._canais: dict[bytes, _Canal] = {}
        self._lock = threading.Lock()
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self.volume = 1.0
        self.descartados = 0

    def definir(self, fontes) -> None:
        """Quais PCs ouvir. Vazio fecha a porta e todas as saidas."""
        fontes = set(fontes)
        with self._lock:
            self._quer = fontes
            sobrando = [i for i, c in self._canais.items() if c.nome not in fontes]
            for i in sobrando:
                self._canais.pop(i).parar()
        if fontes:
            self._abrir_porta()
        else:
            self._fechar_porta()

    def porta(self) -> int:
        return self._sock.getsockname()[1] if self._sock else 0

    def parar(self) -> None:
        self.definir(())

    def _abrir_porta(self) -> None:
        with self._lock:
            if self._sock is not None:
                return
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("", self._porta))
            except OSError as exc:
                sock.close()
                log.warning("audio: nao consegui abrir a porta UDP %d: %s",
                            self._porta, exc)
                return
            sock.settimeout(0.5)
            self._sock = sock
            self._thread = threading.Thread(target=self._ouvir, args=(sock,),
                                            name="audio-rx", daemon=True)
            self._thread.start()
        log.info("audio: ouvindo %s na porta UDP %d", ", ".join(sorted(self._quer)),
                 self._porta)

    def _fechar_porta(self) -> None:
        with self._lock:
            sock, self._sock = self._sock, None
            canais, self._canais = list(self._canais.values()), {}
        for c in canais:
            c.parar()
        if sock is not None:
            sock.close()
            log.info("audio: parei de ouvir")

    def _ouvir(self, sock: socket.socket) -> None:
        ultima_faxina = time.monotonic()
        while self._sock is sock:
            try:
                pacote, _origem = sock.recvfrom(4096)
            except socket.timeout:
                pacote = None
            except OSError:
                return
            agora = time.monotonic()
            if agora - ultima_faxina > 1.0:
                ultima_faxina = agora
                self._faxinar(agora)
            if pacote is not None:
                self._tratar(pacote, agora)

    def _tratar(self, pacote: bytes, agora: float) -> None:
        dados = desempacotar(self._chave, pacote)
        if dados is None:
            self.descartados += 1
            return
        fonte_id, seq, nome, taxa, canais, pcm = dados
        with self._lock:
            if nome not in self._quer:
                return
            canal = self._canais.get(fonte_id)
            if canal is None:
                try:
                    canal = _Canal(nome, taxa, canais, self._abrir_saida,
                                   lambda: self.volume)
                except Exception as exc:
                    log.warning("audio: nao consegui abrir a saida (%d Hz, %d "
                                "canais) para '%s': %s", taxa, canais, nome, exc)
                    return
                self._canais[fonte_id] = canal
                log.info("audio: tocando o som de '%s' (%d Hz, %d canais)",
                         nome, taxa, canais)
            if seq <= canal.ultimo_seq:
                self.descartados += 1  # repetido ou velho
                return
            canal.ultimo_seq = seq
            canal.visto = agora
        canal.empurrar(pcm)

    def _faxinar(self, agora: float) -> None:
        with self._lock:
            sumidos = [i for i, c in self._canais.items()
                       if agora - c.visto > SEM_NOTICIA]
            for i in sumidos:
                self._canais.pop(i).parar()
