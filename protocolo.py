"""Transporte do 2pc_1Kit: frames com prefixo de tamanho, handshake e cifra.

Protocolo de linha
------------------
Cada frame e' `>I` (4 bytes, big-endian, tamanho do payload) seguido do payload.
Depois do handshake todo payload e' um token Fernet contendo JSON UTF-8.

Handshake
---------
1. servidor -> cliente: frame *em claro* com um nonce de 32 bytes.
2. cliente -> servidor: frame cifrado com JSON {"hmac": <hex>, ...}.
   Conseguir decifrar ja' prova conhecimento da chave; o HMAC sobre o nonce
   prova que a resposta e' fresca (impede replay de uma sessao gravada).
3. servidor -> cliente: frame cifrado {"ok": true}.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import socket
import struct
import threading

from cryptography.fernet import Fernet, InvalidToken

PORTA_PADRAO = 24810
TAMANHO_MAXIMO = 32 * 1024 * 1024  # teto de sanidade por frame
_CABECALHO = struct.Struct(">I")


class ErroProtocolo(Exception):
    """Falha de handshake, de cifra ou frame malformado."""


def chave_fernet(segredo: str) -> Fernet:
    """Deriva a chave Fernet do segredo compartilhado do config.json."""
    digest = hashlib.sha256(segredo.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


class Conexao:
    """Socket TCP com frames JSON cifrados. Seguro para um leitor + N escritores."""

    def __init__(self, sock: socket.socket, fernet: Fernet):
        self.sock = sock
        self.fernet = fernet
        self._lock_envio = threading.Lock()
        self._fechada = False
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    # -- frames crus ---------------------------------------------------------

    def _enviar_bruto(self, payload: bytes) -> None:
        with self._lock_envio:
            self.sock.sendall(_CABECALHO.pack(len(payload)) + payload)

    def _receber_exato(self, n: int) -> bytes:
        partes = []
        faltam = n
        while faltam:
            pedaco = self.sock.recv(faltam)
            if not pedaco:
                raise ConnectionError("conexao encerrada pelo outro lado")
            partes.append(pedaco)
            faltam -= len(pedaco)
        return b"".join(partes)

    def _receber_bruto(self) -> bytes:
        (tamanho,) = _CABECALHO.unpack(self._receber_exato(4))
        if tamanho > TAMANHO_MAXIMO:
            raise ErroProtocolo(f"frame de {tamanho} bytes excede o teto")
        return self._receber_exato(tamanho)

    # -- frames cifrados ----------------------------------------------------

    def enviar(self, msg: dict) -> None:
        dados = json.dumps(msg, separators=(",", ":")).encode("utf-8")
        self._enviar_bruto(self.fernet.encrypt(dados))

    def receber(self) -> dict:
        bruto = self._receber_bruto()
        try:
            dados = self.fernet.decrypt(bruto)
        except InvalidToken as exc:
            raise ErroProtocolo("frame com chave invalida ou corrompido") from exc
        return json.loads(dados)

    def fechar(self) -> None:
        if self._fechada:
            return
        self._fechada = True
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()


# -- handshake --------------------------------------------------------------


def _assinar(segredo: str, nonce: bytes) -> str:
    return hmac.new(segredo.encode("utf-8"), nonce, hashlib.sha256).hexdigest()


def aceitar(sock: socket.socket, segredo: str, info: dict) -> tuple[Conexao, dict]:
    """Lado servidor do handshake. Devolve (conexao, hello do cliente)."""
    conn = Conexao(sock, chave_fernet(segredo))
    nonce = secrets.token_bytes(32)
    conn._enviar_bruto(nonce)

    try:
        resposta = conn.receber()
    except ErroProtocolo:
        conn.fechar()
        raise ErroProtocolo(
            "a CHAVE deste PC e' diferente da do outro -- compare as duas e "
            "use a mesma nos dois") from None
    except (ConnectionError, OSError) as exc:
        conn.fechar()
        raise ErroProtocolo(
            f"o outro PC fechou a conexao durante o handshake ({exc}). Pode ser "
            f"chave diferente do lado de la', ou versao diferente do programa"
        ) from None

    if not hmac.compare_digest(resposta.get("hmac", ""), _assinar(segredo, nonce)):
        conn.fechar()
        raise ErroProtocolo("a CHAVE deste PC e' diferente da do outro -- "
                            "compare as duas e use a mesma nos dois")

    conn.enviar({"ok": True, **info})
    return conn, resposta


def conectar(ip: str, porta: int, segredo: str, info: dict) -> tuple[Conexao, dict]:
    """Lado cliente do handshake. Devolve (conexao, hello do servidor)."""
    sock = socket.create_connection((ip, porta), timeout=10)
    sock.settimeout(None)
    conn = Conexao(sock, chave_fernet(segredo))

    nonce = conn._receber_bruto()
    if len(nonce) != 32:
        conn.fechar()
        raise ErroProtocolo("nonce do servidor com tamanho inesperado")

    conn.enviar({"hmac": _assinar(segredo, nonce), **info})
    try:
        ola = conn.receber()
    except (ErroProtocolo, ConnectionError, OSError):
        conn.fechar()
        # O servidor fecha sem responder justamente quando a chave nao confere.
        raise ErroProtocolo(
            "o servidor recusou a conexao: a CHAVE deste PC provavelmente e' "
            "diferente da dele -- compare as duas e use a mesma nos dois"
        ) from None
    if not ola.get("ok"):
        conn.fechar()
        raise ErroProtocolo("servidor recusou o handshake")
    return conn, ola
