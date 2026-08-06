"""Descoberta na rede local: cada programa aberto anuncia-se e escuta os outros.

E' um farol UDP simples na porta 24811: de 2 em 2 segundos manda um pacote de
broadcast dizendo quem e', e vai anotando quem responde. Serve so' para a
interface poder oferecer "achei estes PCs, quer adicionar?" -- a conexao de
verdade continua sendo TCP na porta 24810, com a chave compartilhada.

O pacote **nao leva a chave**, so' os 8 primeiros digitos do hash dela. Isso e'
suficiente para a interface avisar "este PC esta' com outra chave" sem que o
anuncio sirva para alguem entrar.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import socket
import threading
import time

import redes

log = logging.getLogger("descoberta")

PORTA = 24811
INTERVALO = 2.0    # s entre anuncios
VALIDADE = 8.0     # s sem noticias e' considerado sumido
MARCA = "2pc_1Kit"


def impressao_da_chave(chave: str) -> str:
    """Identifica a chave sem revelar nada util sobre ela."""
    if not chave:
        return ""
    return hashlib.sha256(chave.encode("utf-8")).hexdigest()[:8]


INTERVALO_PLACAS = 20.0  # s entre releituras da lista de placas


class Farol(threading.Thread):
    """Anuncia este PC e mantem a lista dos que responderam."""

    def __init__(self, descrever, parar: threading.Event | None = None):
        super().__init__(name="descoberta", daemon=True)
        self.descrever = descrever  # devolve {"nome","porta","papel","chave"}
        self.parar = parar or threading.Event()
        self.id = secrets.token_hex(6)  # para ignorar o proprio eco
        self._achados: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._saidas: dict[str, tuple] = {}  # ip da placa -> (socket, broadcast)
        self._placas_ate = 0.0

    def lista(self) -> list[dict]:
        """Quem foi visto ha' pouco, em ordem de nome."""
        agora = time.monotonic()
        with self._lock:
            vivos = [d for d in self._achados.values()
                     if agora - d["visto_em"] < VALIDADE]
        return sorted(vivos, key=lambda d: d["nome"].lower())

    def run(self) -> None:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.bind(("", PORTA))
            sock.settimeout(0.5)
        except OSError as exc:
            log.warning("nao consegui abrir o farol na porta %d: %s", PORTA, exc)
            return

        log.info("procurando outros PCs na rede (UDP %d)", PORTA)
        proximo_anuncio = 0.0
        try:
            while not self.parar.is_set():
                if time.monotonic() >= proximo_anuncio:
                    self._anunciar()
                    proximo_anuncio = time.monotonic() + INTERVALO
                self._ouvir(sock)
        finally:
            sock.close()
            for saida, _ in self._saidas.values():
                saida.close()
            self._saidas.clear()

    def _atualizar_saidas(self) -> None:
        """Um socket por placa, preso ao IP dela.

        Sem prender, o Windows manda o broadcast so' pela rota padrao -- numa
        maquina com Wi-Fi e cabo em redes diferentes, metade dos PCs nunca
        receberia o anuncio.
        """
        if time.monotonic() < self._placas_ate and self._saidas:
            return
        self._placas_ate = time.monotonic() + INTERVALO_PLACAS
        placas = redes.listar()
        if {p.ip for p in placas} == set(self._saidas):
            return
        for saida, _ in self._saidas.values():
            saida.close()
        self._saidas = {}
        for placa in placas:
            try:
                saida = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                saida.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                saida.bind((placa.ip, 0))
                self._saidas[placa.ip] = (saida, placa.broadcast)
            except OSError as exc:
                log.debug("sem broadcast por %s: %s", placa.ip, exc)
        if self._saidas:
            log.info("anunciando por: %s",
                     ", ".join(f"{ip} -> {bc}" for ip, (_, bc) in self._saidas.items()))
        else:
            log.warning("nenhuma placa de rede utilizavel para a busca")

    def _anunciar(self) -> None:
        try:
            info = self.descrever()
        except Exception:
            return
        self._atualizar_saidas()
        for ip_placa, (saida, broadcast) in list(self._saidas.items()):
            pacote = json.dumps({"app": MARCA, "id": self.id, **info,
                                 "ip": ip_placa}).encode("utf-8")
            for destino in (broadcast, "255.255.255.255"):
                try:
                    saida.sendto(pacote, (destino, PORTA))
                except OSError:
                    pass  # placa caiu: a proxima releitura conserta

    def _ouvir(self, sock: socket.socket) -> None:
        try:
            dados, origem = sock.recvfrom(2048)
        except socket.timeout:
            return
        except OSError:
            return
        try:
            msg = json.loads(dados.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        if msg.get("app") != MARCA or msg.get("id") == self.id:
            return
        nome = str(msg.get("nome", "")).strip()
        if not nome:
            return
        with self._lock:
            self._achados[msg["id"]] = {
                "nome": nome,
                "ip": origem[0],  # o IP de quem mandou vale mais que o anunciado
                "porta": int(msg.get("porta", 0)),
                "papel": str(msg.get("papel", "")),
                "chave": str(msg.get("chave", "")),
                "ips": [str(i) for i in (msg.get("ips") or [])],
                "visto_em": time.monotonic(),
            }


def descritor(cfg: dict):
    """Fabrica a funcao que o Farol usa para se apresentar.

    O campo `ip` e' preenchido pelo Farol com o IP da placa por onde cada copia
    do anuncio sai; aqui vao os demais IPs, para o outro lado poder oferece'-los
    caso precise digitar um a' mao.
    """
    def descrever() -> dict:
        eu = cfg.get("este_pc", "")
        sou_servidor = any(p.get("nome") == eu and p.get("servidor")
                           for p in cfg.get("pcs", []))
        return {
            "nome": eu,
            "ips": [p.ip for p in redes.listar()],
            "porta": int(cfg.get("porta", 24810)),
            "papel": "servidor" if sou_servidor else "cliente",
            "chave": impressao_da_chave(cfg.get("chave", "")),
        }
    return descrever
