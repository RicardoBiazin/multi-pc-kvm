"""Valida a injecao no PC cliente, sem envolver os hooks nem a travessia real.

Roda no lugar do servidor: escuta na porta, espera o cliente do outro PC
conectar e manda uma sequencia fixa de eventos. Use a mesma chave dos dois
lados e deixe o Bloco de Notas em foco no PC cliente antes de comecar.

    python teste_injecao.py
"""

from __future__ import annotations

import ctypes
import logging
import socket
import sys
import time

import configuracao as conf
import protocolo

user32 = ctypes.WinDLL("user32")
TEXTO = "2pc_1Kit ok - acentuacao: cao, agua, ninho"
PAUSA = 0.02


def _tecla(caractere: str) -> list[dict]:
    """Traduz um caractere para eventos de scancode (com Shift quando preciso)."""
    codigo = user32.VkKeyScanW(ord(caractere))
    if codigo == -1:
        return []
    vk = codigo & 0xFF
    precisa_shift = bool(codigo >> 8 & 1)
    scan = user32.MapVirtualKeyW(vk, 0)
    eventos = []
    if precisa_shift:
        eventos.append({"t": "key", "vk": 0x10, "sc": 0x2A, "ext": False, "down": True})
    eventos.append({"t": "key", "vk": vk, "sc": scan, "ext": False, "down": True})
    eventos.append({"t": "key", "vk": vk, "sc": scan, "ext": False, "down": False})
    if precisa_shift:
        eventos.append({"t": "key", "vk": 0x10, "sc": 0x2A, "ext": False, "down": False})
    return eventos


def sequencia() -> list[tuple[dict, float]]:
    passos: list[tuple[dict, float]] = [
        ({"t": "entrar", "de": "esquerda", "rel": 0.5}, 0.5)]

    # movimento: 40 passos para a direita e 20 para baixo
    for _ in range(40):
        passos.append(({"t": "mv", "dx": 8, "dy": 0}, PAUSA))
    for _ in range(20):
        passos.append(({"t": "mv", "dx": 0, "dy": 8}, PAUSA))

    # clique esquerdo (para dar foco onde o cursor parou)
    passos.append(({"t": "btn", "b": "esq", "down": True}, 0.05))
    passos.append(({"t": "btn", "b": "esq", "down": False}, 0.3))

    for caractere in TEXTO:
        for ev in _tecla(caractere):
            passos.append((ev, PAUSA))

    # teclas estendidas: Home, seta direita x5, End
    for vk, scan in ((0x24, 0x47), *(((0x27, 0x4D),) * 5), (0x23, 0x4F)):
        passos.append(({"t": "key", "vk": vk, "sc": scan, "ext": True, "down": True}, PAUSA))
        passos.append(({"t": "key", "vk": vk, "sc": scan, "ext": True, "down": False}, PAUSA))

    # roda: 3 cliques para baixo e 3 para cima
    for delta in (-120, -120, -120, 120, 120, 120):
        passos.append(({"t": "whl", "d": delta, "h": False}, 0.1))

    passos.append(({"t": "soltar"}, 0.0))
    return passos


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        datefmt="%H:%M:%S")
    log = logging.getLogger("teste")
    cfg = conf.carregar()
    porta = cfg.get("porta", protocolo.PORTA_PADRAO)

    ouvinte = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    ouvinte.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    ouvinte.bind(("0.0.0.0", porta))
    ouvinte.listen(1)
    log.info("aguardando o cliente na porta %d...", porta)

    sock, endereco = ouvinte.accept()
    conn, ola = protocolo.aceitar(sock, cfg["chave"],
                                  {"papel": "servidor", "nome": cfg["este_pc"]})
    log.info("cliente '%s' de %s:%d conectado (tela %s)",
             ola.get("nome"), *endereco, ola.get("tela"))
    log.info("coloque o Bloco de Notas em foco no PC cliente; comeca em 5s")
    time.sleep(5)

    passos = sequencia()
    for ev, espera in passos:
        conn.enviar(ev)
        if espera:
            time.sleep(espera)
    log.info("%d eventos enviados. Confira no PC cliente:", len(passos))
    log.info("  1. o cursor andou para a direita e para baixo")
    log.info("  2. o texto digitado foi: %s", TEXTO)
    log.info("  3. Home/setas/End moveram o caret (sem digitar numeros)")
    log.info("  4. a roda rolou para baixo e voltou")
    conn.fechar()


if __name__ == "__main__":
    sys.exit(main())
