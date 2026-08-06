"""Carga e gravacao do config.json, e o registro de inicio automatico.

O config fica em %APPDATA%\\2pc_1Kit\\config.json, e nao ao lado do .exe: assim
sobrevive a troca do executavel e nao esbarra em permissao de Program Files.
Se houver um config.json ao lado do .exe, ele ganha -- e' a forma de levar a
mesma configuracao pronta para o outro PC num pendrive.
"""

from __future__ import annotations

import json
import os
import pathlib
import secrets
import socket
import sys

APP = "2pc_1Kit"
PORTA_PADRAO = 24810
CHAVE_REGISTRO = r"Software\Microsoft\Windows\CurrentVersion\Run"


def pasta_do_executavel() -> pathlib.Path:
    if getattr(sys, "frozen", False):
        return pathlib.Path(sys.executable).parent
    return pathlib.Path(__file__).resolve().parent


def pasta_de_dados() -> pathlib.Path:
    base = os.environ.get("APPDATA") or str(pathlib.Path.home())
    destino = pathlib.Path(base) / APP
    destino.mkdir(parents=True, exist_ok=True)
    return destino


def caminho_config() -> pathlib.Path:
    ao_lado = pasta_do_executavel() / "config.json"
    if ao_lado.exists():
        return ao_lado
    return pasta_de_dados() / "config.json"


_pasta_de_saida: pathlib.Path | None = None
_caminho_log: pathlib.Path | None = None


def pasta_de_saida() -> pathlib.Path:
    """Onde ficam o log e os relatorios: **ao lado do executavel**.

    E' onde o usuario vai procurar. Se a pasta nao aceitar escrita (o .exe numa
    pasta protegida, ou num pendrive travado), cai para %APPDATA% em vez de
    impedir o programa de abrir.
    """
    global _pasta_de_saida
    if _pasta_de_saida is not None:
        return _pasta_de_saida
    destino = pasta_do_executavel()
    sonda = destino / ".2pc_1kit-teste-de-escrita"
    try:
        sonda.write_bytes(b"")
        sonda.unlink()
        _pasta_de_saida = destino
    except OSError:
        _pasta_de_saida = pasta_de_dados()
    return _pasta_de_saida


def nome_de_arquivo(texto: str) -> str:
    """Deixa `texto` usavel em nome de arquivo."""
    return "".join(c if c.isalnum() or c in "-_" else "-" for c in texto)


def caminho_log(nome_do_pc: str = "") -> pathlib.Path:
    """Caminho do log. O nome do PC entra no arquivo na primeira chamada.

    Fica fixo depois disso, para o relatorio ler exatamente o log que esta'
    sendo escrito.
    """
    global _caminho_log
    if _caminho_log is None:
        sufixo = f"-{nome_de_arquivo(nome_do_pc)}" if nome_do_pc else ""
        _caminho_log = pasta_de_saida() / f"2pc_1kit{sufixo}.log"
    return _caminho_log


def padrao() -> dict:
    """Configuracao de primeira execucao: so' este PC, ja' como servidor."""
    eu = socket.gethostname()
    return {
        "este_pc": eu,
        "porta": PORTA_PADRAO,
        "chave": secrets.token_urlsafe(32),
        "iniciar_ao_abrir": True,
        "usar_bandeja": True,
        "descoberta": True,
        "avisar_troca": True,
        "pcs": [
            {"nome": eu, "ip": ip_local(), "coluna": 2, "linha": 1,
             "servidor": True},
        ],
    }


def ip_local() -> str:
    """IP a sugerir para esta maquina.

    Delega ao `redes`, que enumera as placas de verdade: o truque antigo do
    "socket UDP para fora" devolvia so' a placa da rota padrao, e numa maquina
    com Wi-Fi e cabo em redes diferentes isso escondia metade das opcoes.
    """
    import redes
    return redes.ip_padrao()


def carregar() -> dict:
    caminho = caminho_config()
    cfg = padrao()
    if caminho.exists():
        try:
            cfg.update(json.loads(caminho.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            pass  # config corrompido: segue com o padrao, o usuario reconfigura
    cfg.setdefault("capturar", True)
    return cfg


def salvar(cfg: dict) -> pathlib.Path:
    caminho = caminho_config()
    caminho.parent.mkdir(parents=True, exist_ok=True)
    gravavel = {k: v for k, v in cfg.items() if k != "capturar"}
    caminho.write_text(json.dumps(gravavel, indent=2, ensure_ascii=False),
                       encoding="utf-8")
    return caminho


# -- inicio automatico com o Windows ----------------------------------------


def _comando_de_inicio() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    return f'"{sys.executable}" "{pasta_do_executavel() / "app.py"}"'


def inicio_automatico() -> bool:
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, CHAVE_REGISTRO) as chave:
            winreg.QueryValueEx(chave, APP)
        return True
    except OSError:
        return False


def definir_inicio_automatico(ligado: bool) -> None:
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, CHAVE_REGISTRO, 0,
                        winreg.KEY_SET_VALUE) as chave:
        if ligado:
            winreg.SetValueEx(chave, APP, 0, winreg.REG_SZ, _comando_de_inicio())
        else:
            try:
                winreg.DeleteValue(chave, APP)
            except FileNotFoundError:
                pass
