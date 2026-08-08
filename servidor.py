"""Papel servidor: o PC onde o teclado e o mouse estao fisicamente ligados.

E' ele quem manda: guarda o layout, sabe quem esta' conectado, decide de quem e'
o cursor a cada instante e retransmite a area de transferencia para todos.
"""

from __future__ import annotations

import logging
import queue
import socket
import threading
import time

import borda
import clipboard_win
import entrada_win as ew
import layout as lay
import protocolo

log = logging.getLogger("servidor")

FILA_MAXIMA = 2000  # eventos pendentes antes de comecar a descartar movimento
INTERVALO_PING = 5.0


class Servidor:
    def __init__(self, cfg: dict, parar: threading.Event | None = None):
        self.cfg = cfg
        self.layout = lay.Layout.de_config(cfg)
        self.eu = cfg["este_pc"]
        self.parar = parar or threading.Event()
        self.fila: queue.Queue[tuple[str, dict]] = queue.Queue(maxsize=FILA_MAXIMA)
        self.injetor = ew.Injetor()
        self.controle = borda.Controle(self.layout, self.eu, self._enfileirar)
        self.captura = ew.Captura(self.controle.tratar, self.controle.delta_bruto)
        self.clientes: dict[str, protocolo.Conexao] = {}
        self.sinc: clipboard_win.Sincronizador | None = None
        self._lock = threading.Lock()
        self._avisos: dict[str, float] = {}
        self.ultimo_erro = ""
        self.ao_mudar = lambda: None  # a interface liga aqui para atualizar o estado

    # -- fila de eventos ----------------------------------------------------

    def _enfileirar(self, destino: str, ev: dict) -> None:
        """Chamado de dentro do callback do hook: nunca pode bloquear."""
        try:
            self.fila.put_nowait((destino, ev))
        except queue.Full:
            if ev["t"] == "mv":
                return  # movimento e' descartavel; clique e tecla nao sao
            try:
                self.fila.get_nowait()  # abre espaco jogando fora o mais antigo
                self.fila.put_nowait((destino, ev))
            except (queue.Empty, queue.Full):
                pass

    def _avisar_uma_vez(self, chave: str, formato: str, *args) -> None:
        """Evita encher o log: o cliente reconecta a cada 3s e repetiria sempre."""
        agora = time.monotonic()
        if agora - self._avisos.get(chave, 0.0) < 30:
            return
        self._avisos[chave] = agora
        log.warning(formato, *args)

    # -- estado para a interface -------------------------------------------

    def situacao(self) -> dict:
        with self._lock:
            conectados = sorted(self.clientes)
        return {"papel": "servidor", "eu": self.eu, "conectados": conectados,
                "cursor_em": self.controle.atual,
                "erro": "" if conectados else self.ultimo_erro,
                "esperados": [p.nome for p in self.layout.pcs
                              if p.nome != self.eu]}

    # -- ciclo de vida ------------------------------------------------------

    def _endereco_de_escuta(self) -> str:
        """IP em que o servidor abre a porta.

        Escutar em 0.0.0.0 aceita conexao de QUALQUER rede da maquina, inclusive
        o Wi-Fi de um cafe'. Como este programa injeta teclas e cliques no PC, a
        porta merece ficar restrita a' placa que a configuracao ja' escolheu -- o
        handshake HMAC impede o uso sem a chave, e isto reduz quem consegue
        sequer tentar.

        O IP vem da propria entrada deste PC no layout, a mesma que os clientes
        usam para conectar. Se ele nao existe mais aqui (DHCP mudou, cabo saiu),
        volta para 0.0.0.0 com AVISO no log: nao escutar seria pior, e a
        protecao nao pode desaparecer em silencio.
        """
        entrada = next((p for p in self.cfg.get("pcs", [])
                        if p.get("nome") == self.eu), None)
        ip = (entrada or {}).get("ip", "").strip()
        if not ip:
            log.warning("este PC nao tem IP no layout - escutando em todas as "
                        "placas (0.0.0.0)")
            return "0.0.0.0"
        try:
            import redes
            if redes.placa_de(ip) is None:
                log.warning("o IP configurado (%s) nao existe nesta maquina - "
                            "escutando em todas as placas (0.0.0.0). Corrija o "
                            "IP na janela para restringir a porta.", ip)
                return "0.0.0.0"
        except Exception as e:                      # pragma: sem cobertura
            log.warning("nao foi possivel conferir as placas (%s) - escutando "
                        "em todas (0.0.0.0)", e)
            return "0.0.0.0"
        return ip

    def executar(self) -> None:
        if self.cfg.get("capturar", True):
            self.captura.start()
            self.captura.pronta.wait(5)
        else:
            log.warning("--sem-captura: so' a area de transferencia sera' "
                        "sincronizada")

        endereco_escuta = self._endereco_de_escuta()
        ouvinte = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        ouvinte.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        ouvinte.bind((endereco_escuta, self.cfg["porta"]))
        ouvinte.listen(8)
        ouvinte.settimeout(0.5)
        log.info("servidor '%s' ouvindo em %s:%d | panico: Ctrl+Alt+Shift+Esc",
                 self.eu, endereco_escuta, self.cfg["porta"])

        threading.Thread(target=self._enviar, name="rede-tx", daemon=True).start()
        threading.Thread(target=self._pingar, name="ping", daemon=True).start()
        sinc = clipboard_win.Sincronizador(self.difundir, self.parar)
        sinc.start()
        self.sinc = sinc

        try:
            while not self.parar.is_set():
                try:
                    sock, endereco = ouvinte.accept()
                except socket.timeout:
                    continue
                threading.Thread(target=self._receber_cliente,
                                 args=(sock, endereco), daemon=True).start()
        finally:
            ouvinte.close()
            self.parar.set()
            self.captura.parar()  # desinstala os hooks
            with self._lock:
                conexoes = list(self.clientes.values())
            for conn in conexoes:
                conn.fechar()
            log.info("servidor encerrado")

    # -- clientes -----------------------------------------------------------

    def _receber_cliente(self, sock: socket.socket, endereco) -> None:
        try:
            conn, ola = protocolo.aceitar(
                sock, self.cfg["chave"], {"papel": "servidor", "nome": self.eu})
        except Exception as exc:
            log.warning("handshake recusado de %s:%d: %s", *endereco, exc)
            sock.close()
            return

        anunciado = str(ola.get("nome", "")).strip()
        # Nome primeiro; se nao casar, o IP resolve e o cliente adota o nome
        # daqui. E' o que impede o layout dos dois lados de divergir.
        pc = (self.layout.por_nome(anunciado)
              or self.layout.por_ip(endereco[0], ignorar=self.eu))
        if pc is not None and pc.nome == self.eu:
            pc = None  # um cliente nao pode se apresentar como o servidor
        if pc is None:
            conhecidos = ", ".join(f"'{p.nome}' ({p.ip})" for p in self.layout.pcs)
            motivo = (f"nao achei este PC no layout do servidor '{self.eu}': ele se "
                      f"anunciou como '{anunciado}' vindo de {endereco[0]}, e aqui "
                      f"os PCs sao {conhecidos}. Adicione-o no servidor (o painel "
                      f"'Encontrados na rede' preenche nome e IP), ou corrija o IP.")
            # Avisar antes de fechar: sem isto o cliente so' ve 'conexao
            # encerrada' e nao tem como descobrir o que esta' errado.
            try:
                conn.enviar({"t": "recusado", "motivo": motivo})
            except Exception:
                pass
            self._avisar_uma_vez(f"recusa:{anunciado}", "recusei %s (%s): %s",
                                 anunciado, endereco[0], motivo)
            self.ultimo_erro = f"recusei '{anunciado}' ({endereco[0]}): fora do layout"
            self.ao_mudar()
            conn.fechar()
            return

        nome = pc.nome
        if nome != anunciado:
            log.info("'%s' (%s) foi reconhecido pelo IP como '%s'; mandando o "
                     "layout deste servidor", anunciado, endereco[0], nome)
        # O servidor e' a fonte unica do layout: o cliente adota o que vem daqui.
        try:
            conn.enviar({"t": "config", "seu_nome": nome, "servidor": self.eu,
                         "layout": self.layout.para_config()})
        except Exception as exc:
            log.warning("nao consegui mandar o layout para '%s': %s", nome, exc)
            conn.fechar()
            return

        with self._lock:
            antiga = self.clientes.pop(nome, None)
            self.clientes[nome] = conn
        if antiga is not None:
            antiga.fechar()  # reconexao: derruba a sessao velha
        self.controle.conectados.add(nome)
        telas = ola.get("monitores") or []
        log.info("'%s' conectado de %s (tela %s, %d monitor(es)%s)", nome,
                 endereco[0], ola.get("tela"), len(telas) or 1,
                 "".join(f" [{m[2]}x{m[3]} em {m[0]},{m[1]}]" for m in telas))
        self.ao_mudar()

        try:
            while not self.parar.is_set():
                msg = conn.receber()
                self._tratar(nome, msg)
        except Exception as exc:
            log.info("'%s' desconectou: %s", nome, exc)
        finally:
            with self._lock:
                if self.clientes.get(nome) is conn:
                    del self.clientes[nome]
                    self.controle.cliente_caiu(nome)
            conn.fechar()
            self.ao_mudar()

    def _tratar(self, nome: str, msg: dict) -> None:
        tipo = msg.get("t")
        if tipo == "sair":
            self.controle.saiu_do_cliente(nome, msg["dir"], msg["rel"])
            self.ao_mudar()
        elif tipo == "clip":
            if self.sinc is not None:
                self.sinc.aplicar(msg)
            self.difundir(msg, excluir=nome)  # repassa aos demais
        elif tipo == "pong":
            ida_volta = (time.perf_counter() - msg["ts"]) * 1000
            if ida_volta > 30:
                log.info("latencia alta com '%s': %.1f ms", nome, ida_volta)

    # -- envio --------------------------------------------------------------

    def difundir(self, msg: dict, excluir: str = "") -> None:
        """Manda a mesma mensagem a todos os clientes conectados."""
        with self._lock:
            alvos = [(n, c) for n, c in self.clientes.items() if n != excluir]
        for nome, conn in alvos:
            try:
                conn.enviar(msg)
            except Exception:
                log.debug("falha ao enviar clipboard para '%s'", nome)

    def _enviar(self) -> None:
        while not self.parar.is_set():
            try:
                destino, ev = self.fila.get(timeout=0.5)
            except queue.Empty:
                continue
            if destino == borda.LOCAL:
                if ev["t"] == "soltar_local":
                    self.injetor.soltar_modificadores()
                continue
            with self._lock:
                conn = self.clientes.get(destino)
            if conn is None:
                continue
            ev.pop("pos", None)  # 'pos' e' interno, o cliente usa dx/dy
            try:
                conn.enviar(ev)
            except Exception as exc:
                log.warning("falha ao enviar para '%s': %s", destino, exc)
                conn.fechar()

    def _pingar(self) -> None:
        while not self.parar.wait(INTERVALO_PING):
            self.difundir({"t": "ping", "ts": time.perf_counter()})
