"""Copiar arquivo num PC e colar no outro, pela area de transferencia.

O Windows nao guarda arquivo no clipboard: guarda uma LISTA DE CAMINHOS, no
formato CF_HDROP. Mandar a lista pela rede nao serve para nada -- aqueles
caminhos nao existem do outro lado. Entao o conteudo vai junto, em pedacos, e o
PC que recebe grava os arquivos numa pasta propria e poe no clipboard dele o
CF_HDROP apontando para *essas* copias. Colar no Explorer copia de lá, como
qualquer outra origem.

POR QUE EM PEDACOS: o protocolo tem teto de 32 MB por frame, e base64 mais cifra
inflam o payload em uns 35%. Mandar arquivo inteiro numa mensagem limitaria tudo
a uns 20 MB. Em blocos, o teto passa a ser politica (TETO_TRANSFERENCIA), nao
limitacao de transporte.

POR QUE NAO PASTA: arvore recursiva traz caminho acima de 260 caracteres, link
simbolico e o risco de mandar uma pasta gigante sem perceber. Pasta selecionada
e' recusada com aviso -- para isso existe o compartilhamento de rede.

A struct DROPFILES e' montada como `bytes` e entregue pronta ao pywin32, que faz
a alocacao. Nada de GlobalAlloc nosso: este projeto ja' tem uma corrupcao de
heap em aberto e nao vale a pena somar mais um candidato.
"""

from __future__ import annotations

import base64
import logging
import pathlib
import shutil
import struct
import time

import configuracao as conf

log = logging.getLogger("arquivos")

TETO_TRANSFERENCIA = 500 * 1024 * 1024  # soma dos arquivos de uma colagem
BLOCO = 2 * 1024 * 1024                 # bytes CRUS por mensagem
# Pastas de transferencias antigas que ficam no disco. Nao apagamos na hora de
# colar: o Explorer copia do nosso caminho DEPOIS de o usuario colar, e sumir com
# o arquivo antes disso quebraria a colagem. Ficam as N ultimas.
RECEBIDOS_A_MANTER = 5

_CABECALHO_DROPFILES = struct.Struct("<IiiII")  # pFiles, pt.x, pt.y, fNC, fWide


# -- CF_HDROP ---------------------------------------------------------------


def montar_hdrop(caminhos) -> bytes:
    """Blob de CF_HDROP para uma lista de caminhos.

    Layout: a struct DROPFILES (20 bytes em x64), depois os caminhos em
    UTF-16LE, cada um terminado em NUL, e um NUL extra fechando a lista.
    `fWide=1` diz que os caminhos sao UTF-16 e nao ANSI.
    """
    lista = "".join(f"{c}\0" for c in caminhos) + "\0"
    cabecalho = _CABECALHO_DROPFILES.pack(
        _CABECALHO_DROPFILES.size, 0, 0, 0, 1)
    return cabecalho + lista.encode("utf-16-le")


# -- lado que envia ---------------------------------------------------------


def _recusar(motivo: str) -> None:
    log.info("nao vou mandar os arquivos: %s", motivo)


def preparar(caminhos: list[str]) -> tuple[list[pathlib.Path], int] | None:
    """(arquivos validos, soma dos tamanhos), ou None se nao da' para mandar.

    Recusa em bloco, e nao arquivo por arquivo: mandar "parte da selecao" seria
    pior que nao mandar, porque quem cola nao tem como saber o que faltou.
    """
    itens = [pathlib.Path(c) for c in caminhos]
    pastas = [i.name for i in itens if i.is_dir()]
    if pastas:
        _recusar(f"'{pastas[0]}' e' uma pasta; use o compartilhamento de rede "
                 f"para pasta inteira")
        return None

    # Ping-pong: o que acabou de chegar do outro PC fica na nossa pasta de
    # recebidos. Sem esta regra, colar de um lado devolveria tudo para o outro,
    # que devolveria de novo, para sempre.
    recebidos = pasta_de_recebidos().resolve()
    for item in itens:
        try:
            if recebidos in item.resolve().parents:
                _recusar("estes arquivos acabaram de chegar do outro PC")
                return None
        except OSError:
            pass

    validos, total = [], 0
    for item in itens:
        try:
            total += item.stat().st_size
        except OSError as erro:
            _recusar(f"nao consegui ler '{item.name}': {erro.strerror}")
            return None
        validos.append(item)

    if not validos:
        return None
    if total > TETO_TRANSFERENCIA:
        _recusar(f"{total / 1e6:.0f} MB acima do teto de "
                 f"{TETO_TRANSFERENCIA / 1e6:.0f} MB")
        return None
    return validos, total


def mensagens(arquivos: list[pathlib.Path], total: int, identificador: str):
    """Gera as mensagens de uma transferencia, na ordem em que devem ir.

    Generator de proposito: um arquivo de 400 MB nao pode ser carregado inteiro
    na memoria para depois ser fatiado.
    """
    yield {"t": "arq", "e": "inicio", "id": identificador,
           "itens": [{"nome": a.name, "tam": a.stat().st_size} for a in arquivos],
           "total": total}
    enviados = 0
    marco = 0
    for indice, caminho in enumerate(arquivos):
        try:
            with open(caminho, "rb") as f:
                while True:
                    pedaco = f.read(BLOCO)
                    if not pedaco:
                        break
                    enviados += len(pedaco)
                    yield {"t": "arq", "e": "bloco", "id": identificador,
                           "i": indice,
                           "dados": base64.b64encode(pedaco).decode()}
                    # Progresso a cada 20%: em rede lenta, uma transferencia
                    # grande parecia travamento por nao dizer nada.
                    if total and enviados * 5 // total > marco:
                        marco = enviados * 5 // total
                        log.info("enviando arquivos: %d%%",
                                 round(100 * enviados / total))
        except OSError as erro:
            log.warning("falha lendo '%s': %s", caminho.name, erro.strerror)
            yield {"t": "arq", "e": "aborta", "id": identificador,
                   "motivo": f"falha lendo '{caminho.name}'"}
            return
    yield {"t": "arq", "e": "fim", "id": identificador}


# -- lado que recebe --------------------------------------------------------


def pasta_de_recebidos() -> pathlib.Path:
    """Onde os arquivos que chegam sao gravados.

    No `%APPDATA%` de quem vai COLAR, que nem sempre e' o de quem esta'
    rodando: o agente do inicio automatico e' SYSTEM, e o `%APPDATA%` dele e'
    `C:\\Windows\\system32\\config\\systemprofile\\...`, pasta que o usuario
    logado nao consegue ler. Os arquivos chegavam inteiros, o clipboard
    recebia esses caminhos, e colar no Explorador dava acesso negado -- sem uma
    linha de erro em lugar nenhum.

    Quando quem roda e' a janela do proprio usuario, `appdata_do_usuario...`
    devolve None (falta o privilegio SE_TCB) e caimos no caminho de sempre, que
    ali ja' e' o certo.
    """
    base = None
    try:
        import sessao_win
        base = sessao_win.appdata_do_usuario_do_console()
    except Exception:
        log.debug("sem o %APPDATA% do usuario do console", exc_info=True)
    destino = (base / conf.APP_ARQUIVO if base else conf.pasta_de_dados()) \
        / "recebidos"
    try:
        destino.mkdir(parents=True, exist_ok=True)
    except OSError:
        destino = conf.pasta_de_dados() / "recebidos"
        destino.mkdir(parents=True, exist_ok=True)
    return destino


def limpar_recebidos(manter: int = RECEBIDOS_A_MANTER) -> None:
    """Apaga as transferencias mais antigas, deixando as `manter` ultimas."""
    try:
        pastas = sorted((p for p in pasta_de_recebidos().iterdir() if p.is_dir()),
                        key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return
    for velha in pastas[manter:]:
        shutil.rmtree(velha, ignore_errors=True)


class Recepcao:
    """Junta os blocos de uma transferencia e grava os arquivos no disco.

    Uma instancia por conexao. Trata uma transferencia por vez: se comecar outra
    antes de a anterior acabar, a anterior e' descartada -- o clipboard tambem so'
    tem um conteudo, entao nao ha' o que preservar.
    """

    def __init__(self):
        self.id: str | None = None
        self.destino: pathlib.Path | None = None
        self.itens: list[dict] = []
        self.total = 0
        self.recebido = 0
        self._aberto = None
        self._indice = -1

    def _fechar(self) -> None:
        if self._aberto is not None:
            try:
                self._aberto.close()
            except OSError:
                pass
            self._aberto = None

    def descartar(self, motivo: str) -> None:
        self._fechar()
        if self.destino is not None:
            shutil.rmtree(self.destino, ignore_errors=True)
        if self.id is not None:
            log.info("transferencia descartada: %s", motivo)
        self.__init__()

    def aplicar(self, msg: dict) -> list[pathlib.Path] | None:
        """Consome uma mensagem. Devolve os caminhos quando a transferencia acaba."""
        etapa, identificador = msg.get("e"), msg.get("id")

        if etapa == "inicio":
            if self.id is not None:
                self.descartar("comecou outra antes de esta terminar")
            self.id = identificador
            self.itens = msg.get("itens", [])
            self.total = msg.get("total", 0)
            self.recebido = 0
            self._indice = -1
            # O nome da pasta e' o identificador para duas colagens seguidas nao
            # se misturarem, e para o caminho no clipboard mudar -- se fosse
            # sempre o mesmo, o Explorer poderia mostrar o arquivo anterior.
            self.destino = pasta_de_recebidos() / str(identificador)
            self.destino.mkdir(parents=True, exist_ok=True)
            return None

        if identificador != self.id:
            return None  # sobra de uma transferencia que ja' foi descartada

        if etapa == "aborta":
            self.descartar(msg.get("motivo", "o outro lado desistiu"))
            return None

        if etapa == "bloco":
            indice = msg.get("i", 0)
            if indice != self._indice:
                self._fechar()
                if not 0 <= indice < len(self.itens):
                    self.descartar("bloco de um arquivo que nao foi anunciado")
                    return None
                nome = _nome_seguro(self.itens[indice]["nome"])
                try:
                    self._aberto = open(self.destino / nome, "wb")
                except OSError as erro:
                    self.descartar(f"nao consegui gravar '{nome}': {erro.strerror}")
                    return None
                self._indice = indice
            pedaco = base64.b64decode(msg["dados"])
            self.recebido += len(pedaco)
            try:
                self._aberto.write(pedaco)
            except OSError as erro:
                self.descartar(f"falha gravando: {erro.strerror}")
            return None

        if etapa == "fim":
            self._fechar()
            caminhos = [self.destino / _nome_seguro(i["nome"]) for i in self.itens]
            faltando = [c.name for c in caminhos if not c.is_file()]
            if faltando:
                self.descartar(f"faltou chegar '{faltando[0]}'")
                return None
            log.info("%d arquivo(s) recebido(s) em %s", len(caminhos), self.destino)
            self.__init__()
            limpar_recebidos()
            return caminhos

        return None


def _nome_seguro(nome: str) -> str:
    """So' o nome do arquivo, sem nada que escape da pasta de destino.

    O nome chega pela rede. Sem isto, um `..\\..\\algo.exe` do outro lado
    gravaria fora da pasta de recebidos -- e a conexao ser cifrada e autenticada
    nao muda isso: protege contra estranho, nao contra bug do outro lado.
    """
    limpo = pathlib.PurePath(nome.replace("\\", "/")).name
    return limpo or f"arquivo-{int(time.time())}"
