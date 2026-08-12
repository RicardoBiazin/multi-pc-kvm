"""Carga e gravacao do config.json, e o registro de inicio automatico.

O config fica em %APPDATA%\\MultiPC-KVM\\config.json, e nao ao lado do .exe: assim
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

# Nome de EXIBICAO: janela, bandeja, rodape, relatorio, regra de firewall.
APP = "Multi PC - KVM"
# Nome usado em ARQUIVO e PASTA. Sem espacos de proposito: espaco em nome de
# executavel obriga aspas em todo caminho que o referencia -- script, regra de
# firewall e, principalmente, a chave de inicio automatico do Windows, que e'
# onde isso costuma quebrar em silencio.
APP_ARQUIVO = "MultiPC-KVM"
# Nome anterior do programa. Fica aqui so' para achar a configuracao gravada
# antes da renomeacao (v1.3): dentro dela esta' a CHAVE COMPARTILHADA, e perde-la
# obrigaria a reconfigurar os dois PCs. Ver `pasta_de_dados`.
APP_ANTIGO = "2pc_1Kit"
# Fonte unica da versao: janela, log, relatorio e o anuncio na rede leem daqui.
# O `empacotar.py` tambem gera o versao.txt do executavel a partir dela.
VERSAO = "2.0"
AUTOR = "Ricardo Biazin"
LINKEDIN = "https://www.linkedin.com/in/ricardo-biazin/"

PORTA_PADRAO = 24810
CHAVE_REGISTRO = r"Software\Microsoft\Windows\CurrentVersion\Run"


def pasta_do_executavel() -> pathlib.Path:
    if getattr(sys, "frozen", False):
        return pathlib.Path(sys.executable).parent
    return pathlib.Path(__file__).resolve().parent


def pasta_de_dados() -> pathlib.Path:
    base = os.environ.get("APPDATA") or str(pathlib.Path.home())
    destino = pathlib.Path(base) / APP_ARQUIVO
    destino.mkdir(parents=True, exist_ok=True)
    _herdar_config_antigo(pathlib.Path(base) / APP_ANTIGO, destino)
    return destino


def _herdar_config_antigo(antiga: pathlib.Path, nova: pathlib.Path) -> None:
    """Traz o config.json da pasta usada antes da renomeacao (v1.3).

    Sem isto, atualizar o programa apagaria a configuracao do ponto de vista do
    usuario: o arquivo continuaria no disco, mas numa pasta que ninguem mais le'.
    Dentro dele esta' a CHAVE COMPARTILHADA -- e uma chave nova de um lado so'
    faz o handshake falhar sem explicacao obvia.

    Copia, nao move: se o usuario voltar para a versao anterior por algum motivo,
    ela ainda encontra a configuracao dela onde esperava.
    """
    novo = nova / "config.json"
    antigo = antiga / "config.json"
    if novo.exists() or not antigo.is_file():
        return
    try:
        novo.write_bytes(antigo.read_bytes())
    except OSError:
        pass  # sem permissao ou disco cheio: segue com a configuracao padrao


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
    sonda = destino / f".{APP_ARQUIVO.lower()}-teste-de-escrita"
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
        _caminho_log = pasta_de_saida() / f"{APP_ARQUIVO.lower()}{sufixo}.log"
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
        # "sistema" segue o tema do Windows; "claro" e "escuro" mandam nele.
        "tema": "sistema",
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


def migrar_inicio_automatico() -> bool:
    """Passa o inicio automatico do nome antigo para o novo (v1.3).

    Sem isto o Windows continuaria abrindo o EXECUTAVEL ANTIGO a cada login: a
    entrada velha aponta para o 2pc_1Kit.exe, que sobrevive ao lado do novo na
    mesma pasta. Dois programas destes no ar disputam os hooks de mouse e
    teclado e nenhum funciona direito -- e a causa seria dificil de achar,
    porque nada no programa novo denuncia o velho.

    Devolve True se havia entrada antiga (para o chamador registrar no log).
    """
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, CHAVE_REGISTRO, 0,
                            winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE) as chave:
            try:
                winreg.QueryValueEx(chave, APP_ANTIGO)
            except FileNotFoundError:
                return False
            winreg.DeleteValue(chave, APP_ANTIGO)
            winreg.SetValueEx(chave, APP, 0, winreg.REG_SZ, _comando_de_inicio())
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
