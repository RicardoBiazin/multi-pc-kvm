"""Papel cliente: recebe o input do servidor e injeta como se fosse local.

O cliente mantem a *posicao virtual* do cursor (ver `alvo.Alvo`). Ela avanca com
os deltas que chegam pela rede; quando passa de uma das quatro arestas, ele avisa
o servidor por qual lado saiu e para de injetar. Quem decide para onde o cursor
vai e' o servidor, que e' quem tem o layout -- o cliente nao precisa saber quem
sao os vizinhos dele.

Comando bidirecional (v1.2)
---------------------------
Se este PC tambem tiver teclado e mouse fisicos, eles nao ficam parados: o
cliente instala os mesmos hooks do servidor e, ao encostar o cursor *local* numa
aresta que tem vizinho, pede o comando (`assumir`). Concedido, ele passa a
bloquear o input local e a manda'-lo para o servidor, que continua sendo o unico
a decidir o roteamento -- so' que agora a fonte do input pode ser qualquer PC.
Ver `ComandoLocal`, no fim deste arquivo.
"""

from __future__ import annotations

import logging
import queue
import threading
import time

import alvo as alv
import clipboard_win
import configuracao as conf
import entrada_win as ew
import layout as lay
import protocolo

log = logging.getLogger("cliente")

ESPERA_RECONEXAO = 3.0
ESPERA_APOS_RECUSA = 15.0  # o servidor recusou por configuracao: insistir nao adianta
FILA_MAXIMA = 2000  # eventos pendentes antes de comecar a descartar movimento
TRAVA_APOS_RETORNO = 0.4  # s ignorando a borda local, para nao reentrar em seguida
INTERVALO_DO_PEDIDO = 0.5  # s entre dois 'assumir' -- o mouse fica encostado
# Teto para a espera da resposta do servidor a um 'sair'. Generoso de proposito:
# a conexao e' TCP, entao mensagem nao se perde -- ou chega, ou o socket cai e a
# reconexao resolve. Este prazo existe para o servidor que trava ou morre sem
# fechar o socket, e nao para latencia. Cortar cedo demais faria a travessia
# falhar em rede lenta (ja' houve ida-e-volta de 6 s neste par de PCs).
ESPERA_MAXIMA = 5.0  # s


class Recusado(Exception):
    """O servidor respondeu dizendo por que nao aceita este PC."""


class Cliente:
    def __init__(self, cfg: dict, parar: threading.Event | None = None):
        self.cfg = cfg
        self.layout = lay.Layout.de_config(cfg)
        self.eu = cfg["este_pc"]
        self.parar = parar or threading.Event()
        self.injetor = ew.Injetor()
        self.x0, self.y0, self.largura, self.altura = ew.geometria_virtual()
        # Retangulos reais das telas: com mais de um monitor o retangulo que os
        # envolve tem buracos, e o cursor nao pode parar neles.
        self.monitores = [m[:4] for m in ew.monitores()]
        self.alvo = alv.Alvo(self.x0, self.y0, self.largura, self.altura,
                             self.monitores)
        self.remoto = False
        self.conectado = False
        # True entre mandar 'sair' e o servidor responder: nao adianta injetar
        # nem reavisar enquanto ele nao decide para onde o cursor vai.
        self.aguardando = False
        self._esperando_desde = 0.0
        # Teclado e mouse ligados a ESTE PC. So' entra em acao se houver hooks;
        # num cliente sem periferico ele simplesmente nunca pede o comando.
        self.comando = ComandoLocal(self)
        self.fila: queue.Queue[dict] = queue.Queue(maxsize=FILA_MAXIMA)
        self.ao_mudar = lambda: None
        self.ao_trocar = lambda de, para: None
        self.ultimo_erro = ""
        self.config_mudou = False  # a interface redesenha quando ve isto
        self._avisos: dict[str, float] = {}

    def situacao(self) -> dict:
        servidor = self.layout.servidor()
        if self.remoto:
            cursor = self.eu
        elif self.comando.comandando:
            cursor = servidor.nome if servidor else ""
        else:
            cursor = ""
        return {"papel": "cliente", "eu": self.eu,
                "conectados": [servidor.nome] if (servidor and self.conectado) else [],
                "cursor_em": cursor,
                "comandando": self.comando.comandando,
                "erro": "" if self.conectado else self.ultimo_erro,
                "esperados": [servidor.nome] if servidor else []}

    # -- fila de envio ------------------------------------------------------

    def enfileirar(self, ev: dict) -> None:
        """Chamado de dentro do callback do hook: nunca pode bloquear."""
        try:
            self.fila.put_nowait(ev)
        except queue.Full:
            if ev.get("t") == "in" and ev.get("ev", {}).get("t") == "mv":
                return  # movimento e' descartavel; clique e tecla nao sao
            try:
                self.fila.get_nowait()  # abre espaco jogando fora o mais antigo
                self.fila.put_nowait(ev)
            except (queue.Empty, queue.Full):
                pass

    # -- ciclo de vida ------------------------------------------------------

    def executar(self) -> None:
        servidor = self.layout.servidor()
        if servidor is None:
            log.error("nenhum PC marcado como servidor no layout")
            return
        log.info("cliente '%s': tela %dx%d em (%d,%d) | servidor '%s' em %s:%d",
                 self.eu, self.largura, self.altura, self.x0, self.y0,
                 servidor.nome, servidor.ip, self.cfg["porta"])
        self._conferir_injecao()
        if self.cfg.get("capturar", True):
            self.comando.iniciar()
        try:
            self._laco()
        finally:
            # Desinstala os hooks: sem isto, parar e reiniciar deixaria a
            # instalacao antiga no lugar e o input passaria por dois conjuntos.
            self.comando.parar()

    def _laco(self) -> None:
        while not self.parar.is_set():
            # Relido a cada volta: o layout pode ter sido trocado pelo servidor.
            servidor = self.layout.servidor()
            if servidor is None:
                log.error("nenhum PC marcado como servidor no layout")
                return
            destino = (servidor.ip, self.cfg["porta"])
            try:
                conn, _ = protocolo.conectar(
                    *destino, self.cfg["chave"],
                    {"papel": "cliente", "nome": self.eu,
                     "tela": [self.largura, self.altura],
                     "monitores": self.monitores},
                )
            except Exception as exc:
                self.ultimo_erro = str(exc)
                self._avisar_uma_vez(
                    f"conectar:{exc}",
                    "sem servidor em %s:%d (%s); tentando a cada %.0fs",
                    *destino, exc, ESPERA_RECONEXAO)
                self.ao_mudar()
                if self.parar.wait(ESPERA_RECONEXAO):
                    return
                continue
            log.info("conectado a '%s' (%s:%d)", servidor.nome, *destino)
            self.conectado = True
            self.ultimo_erro = ""
            self.ao_mudar()
            espera = self._atender(conn)
            self.conectado = False
            self.ao_mudar()
            if self.parar.wait(espera):
                return

    def _adotar_layout(self, msg: dict) -> None:
        """Aceita a lista de PCs e o mapa que vieram do servidor.

        O servidor e' a fonte unica: assim os dois lados nao podem divergir, e
        configurar um cliente e' so' chave + IP do servidor.
        """
        import configuracao as conf
        novos = msg.get("layout") or []
        meu_nome = str(msg.get("seu_nome", "")).strip()
        if not novos or not meu_nome:
            return
        antes = (self.cfg.get("este_pc"), self.cfg.get("pcs"))
        self.cfg["pcs"] = novos
        self.cfg["este_pc"] = meu_nome
        if antes == (meu_nome, novos):
            return  # nada mudou; nao mexe no disco nem avisa a interface

        self.eu = meu_nome
        self.layout = lay.Layout.de_config(self.cfg)
        try:
            conf.salvar(self.cfg)
        except OSError as exc:
            log.warning("nao consegui gravar o layout recebido: %s", exc)
        self.config_mudou = True
        log.info("layout adotado do servidor '%s': aqui eu sou '%s'; PCs: %s",
                 msg.get("servidor"), meu_nome,
                 ", ".join(p["nome"] for p in novos))
        self.ao_mudar()

    def _conferir_injecao(self) -> None:
        """Sem isto, o cliente aceita o controle, injeta, e nada aparece."""
        import diagnostico
        ok, detalhe = ew.injecao_funciona()
        conflitos = diagnostico.programas_conflitantes()
        if ok and not conflitos:
            log.info("%s | administrador: %s", detalhe,
                     "sim" if diagnostico.elevado() else "NAO")
            return
        if conflitos:
            aviso = (f"{', '.join(conflitos)} esta' rodando neste PC e faz a "
                     f"mesma coisa que o {conf.APP}: os dois disputam os hooks de "
                     f"mouse e teclado e nenhum funciona direito. Feche-o.")
        else:
            aviso = (f"{detalhe}. Verifique se o programa esta' como "
                     f"Administrador e se nao ha' outro programa de teclado/"
                     f"mouse compartilhado no ar.")
        self.ultimo_erro = aviso
        log.error("A INJECAO DE MOUSE NAO ESTA' FUNCIONANDO: %s", aviso)
        self.ao_mudar()

    def _atender(self, conn: protocolo.Conexao) -> float:
        """Devolve quantos segundos esperar antes de tentar de novo."""
        fim = threading.Event()
        sinc = clipboard_win.Sincronizador(conn.enviar, fim)
        sinc.start()
        # O input local sai por uma fila, e nao direto do callback do hook: o
        # `sendall` pode bloquear, e um hook lento e' desinstalado pelo Windows.
        threading.Thread(target=self._escoar, args=(conn, fim),
                         name="rede-tx", daemon=True).start()
        espera = ESPERA_RECONEXAO
        try:
            while not self.parar.is_set():
                self._aplicar(conn, sinc, conn.receber())
        except Recusado as exc:
            # Configuracao errada: reconectar em 3s so' encheria o log.
            self.ultimo_erro = str(exc)
            log.error("O SERVIDOR RECUSOU ESTE PC: %s", exc)
            espera = ESPERA_APOS_RECUSA
        except Exception as exc:
            self.ultimo_erro = str(exc)
            self._avisar_uma_vez(f"queda:{exc}", "conexao encerrada: %s", exc)
        finally:
            # Antes de tudo: se estavamos comandando, o input local esta'
            # bloqueado e sem a rede ele nunca mais seria liberado.
            self.comando.largar("a conexao com o servidor caiu")
            self._encerrar_remoto()
            fim.set()
            conn.fechar()
        return espera

    def _escoar(self, conn: protocolo.Conexao, fim: threading.Event) -> None:
        """Manda ao servidor o que o teclado e o mouse daqui produziram."""
        while not fim.is_set() and not self.parar.is_set():
            try:
                ev = self.fila.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                conn.enviar(ev)
            except Exception:
                return  # a conexao caiu; quem trata e' o laco de recepcao

    def _avisar_uma_vez(self, chave: str, formato: str, *args) -> None:
        """Evita encher o log: tentamos reconectar a cada 3s, sempre com o mesmo erro."""
        agora = time.monotonic()
        if agora - self._avisos.get(chave, 0.0) < 30:
            return
        self._avisos[chave] = agora
        log.warning(formato, *args)

    # -- aplicacao das mensagens -------------------------------------------

    def _aplicar(self, conn, sinc, msg: dict) -> None:
        tipo = msg.get("t")

        if tipo == "clip":
            sinc.aplicar(msg)
            return
        if tipo == "ping":
            conn.enviar({"t": "pong", "ts": msg["ts"]})
            return
        if tipo == "recusado":
            raise Recusado(msg.get("motivo", "sem motivo informado"))
        if tipo == "config":
            self._adotar_layout(msg)
            return
        if tipo == "comando":
            # Resposta ao nosso 'assumir': o teclado e o mouse daqui passam (ou
            # nao) a comandar os outros PCs.
            self.comando.responder(bool(msg.get("ok")), msg.get("motivo", ""))
            return
        if tipo == "devolver":
            # Estamos comandando e o cursor voltou para ca': nada de injecao
            # remota, e' so' largar o bloqueio e deixar o mouse local em paz.
            self.comando.devolver(msg["de"], msg["rel"])
            return
        if tipo == "entrar":
            self.aguardando = False
            x, y = self.alvo.entrar(msg["de"], msg["rel"])
            self.injetor.mover_para(x, y)
            if not self.remoto:
                self.remoto = True
                log.info("controle recebido (entrou pela %s, cursor em %d,%d)",
                         msg["de"], x, y)
                self.ao_trocar("", self.eu)
                self.ao_mudar()
            return
        if tipo == "soltar":
            self._encerrar_remoto()
            return

        if not self.remoto:
            return  # evento atrasado
        if self.aguardando and not self._desistiu_de_esperar():
            return  # a espera da decisao do servidor sobre o nosso 'sair'

        if tipo == "mv":
            self.alvo.mover(msg["dx"], msg["dy"])
            saida = self.alvo.saida()
            if saida is not None:
                direcao, rel = saida
                # Nao encerramos aqui: quem decide se o cursor vai embora ou
                # fica encostado e' o servidor, que responde com 'entrar' ou
                # 'soltar'. Ate' la', so' paramos de injetar movimento.
                self.aguardando = True
                self._esperando_desde = time.monotonic()
                conn.enviar({"t": "sair", "dir": direcao, "rel": rel})
                log.info("cursor saiu pela %s; aguardando o servidor", direcao)
                return
            self.injetor.mover_para(*self.alvo.ponto())
        elif tipo == "btn":
            self.injetor.botao(msg["b"], msg["down"])
        elif tipo == "whl":
            self.injetor.roda(msg["d"], msg["h"])
        elif tipo == "key":
            self.injetor.tecla(msg["vk"], msg["sc"], msg["ext"], msg["down"])

    def _desistiu_de_esperar(self) -> bool:
        """Passou de ESPERA_MAXIMA sem resposta ao 'sair': volta a injetar.

        Sem isto a espera nao tem fim: `aguardando` so' e' desligado por 'entrar'
        ou 'soltar'. Se nenhum dos dois chegar -- servidor travado, ou uma
        transicao que o deixou sem responder --, todo movimento e' descartado
        deste ponto em diante, em silencio, e o cursor fica parado onde estava.
        O teclado continua, porque nao passa por aqui.

        Voltar a injetar e' seguro: encostar de novo na aresta manda outro
        'sair', e o servidor ignora aviso de quem ja' nao tem o cursor.
        """
        if time.monotonic() - self._esperando_desde < ESPERA_MAXIMA:
            return False
        log.warning("o servidor nao respondeu ao 'sair' em %.0fs; voltando a "
                    "mover o cursor aqui", ESPERA_MAXIMA)
        self.aguardando = False
        return True

    def _encerrar_remoto(self) -> None:
        self.aguardando = False
        self.alvo.parar()
        if not self.remoto:
            return
        self.remoto = False
        self.injetor.soltar_modificadores()
        log.info("controle devolvido")
        servidor = self.layout.servidor()
        self.ao_trocar(self.eu, servidor.nome if servidor else "")
        self.ao_mudar()


class ComandoLocal:
    """O teclado e o mouse ligados a ESTE cliente, comandando os outros PCs.

    Espelha o `borda.Controle` do servidor, com uma diferenca importante: o
    roteamento continua sendo decidido la'. Aqui so' fazemos duas coisas --
    detectar o encosto na borda para *pedir* o comando, e, enquanto ele for
    nosso, bloquear o input local e manda'-lo para o servidor.

    Enquanto este PC estiver sendo comandado de fora (`cliente.remoto`), o mouse
    local fica **solto**: nao bloqueamos nem tomamos o comando de volta. Foi a
    escolha do usuario, e evita que um esbarrao no mouse roube o cursor de quem
    esta' do outro lado.
    """

    def __init__(self, cliente: "Cliente"):
        self.cliente = cliente
        self.captura: ew.Captura | None = None
        self.comandando = False
        self.ancora = (0, 0)
        self._liberado_em = 0.0
        self._pedido_em = 0.0
        self._engolidas: set[int] = set()

    def iniciar(self) -> None:
        if self.captura is not None:
            return
        centro = lay.centro_principal(self.cliente.monitores, self.cliente.x0,
                                      self.cliente.y0, self.cliente.largura,
                                      self.cliente.altura)
        self.ancora = (int(centro[0]), int(centro[1]))
        self.captura = ew.Captura(self.tratar, self.delta_bruto)
        # Mesma disputa do lado servidor: enquanto ESTE PC comanda outro, as
        # teclas tem de ser engolidas aqui. Ver INTERVALO_DISPUTA.
        self.captura.em_disputa = lambda: self.comandando
        self.captura.start()
        if not self.captura.pronta.wait(5):
            log.warning("nao consegui instalar os hooks neste cliente: o teclado "
                        "e o mouse daqui nao vao comandar os outros PCs (o "
                        "controle vindo do servidor continua funcionando)")
            return
        log.info("teclado e mouse deste PC podem comandar os outros: encoste o "
                 "cursor na borda do lado do PC desejado | panico: "
                 "Ctrl+Alt+Shift+Esc")

    def parar(self) -> None:
        if self.captura is not None:
            self.captura.parar()
            self.captura = None

    # -- callbacks do hook (thread dos hooks; tem de ser baratos) -----------

    def tratar(self, ev: dict) -> bool:
        """True = engolir o evento (estamos comandando outro PC)."""
        tipo = ev["t"]

        if tipo == "key":
            if ev["down"] and self._e_panico(ev):
                self._engolidas.add(ev["vk"])
                self._panico()
                return True
            if ev["vk"] in self._engolidas:
                # keyup da tecla cujo keydown viramos atalho: nao pode escapar
                if not ev["down"]:
                    self._engolidas.discard(ev["vk"])
                return True
            if not self.comandando:
                return False
            self._mandar(ev)
            return True

        if tipo == "mv":
            if self.comandando:
                return True  # o movimento vai pelo Raw Input, em delta_bruto
            return self._talvez_pedir(*ev["pos"])

        # botoes e roda
        if not self.comandando:
            return False
        self._mandar(ev)
        return True

    def delta_bruto(self, dx: int, dy: int) -> None:
        if not self.comandando:
            return
        self._mandar({"t": "mv", "dx": dx, "dy": dy})

    # -- pedido e concessao -------------------------------------------------

    def _talvez_pedir(self, x: int, y: int) -> bool:
        """O cursor LOCAL encostou numa borda: pedir o comando do vizinho."""
        c = self.cliente
        if c.remoto or not c.conectado:
            return False  # sendo comandado, ou sem servidor para pedir
        agora = time.monotonic()
        if agora - self._liberado_em < TRAVA_APOS_RETORNO:
            return False
        if agora - self._pedido_em < INTERVALO_DO_PEDIDO:
            return False  # o mouse fica encostado; nao repetir a cada evento
        direcao = lay.aresta_encostada(x, y, c.x0, c.y0, c.largura, c.altura)
        if direcao is None:
            return False
        if c.layout.vizinho(c.eu, direcao) is None:
            return False  # nao ha' PC desse lado: borda comum, deixa passar
        rel = lay.relativo_na_aresta(x, y, direcao, c.x0, c.y0, c.largura,
                                     c.altura)
        self._pedido_em = agora
        c.enfileirar({"t": "assumir", "dir": direcao, "rel": rel})
        # Nao engolimos: ate' o servidor conceder, o mouse continua sendo daqui.
        return False

    def responder(self, ok: bool, motivo: str = "") -> None:
        """O servidor respondeu ao nosso pedido de comando."""
        if not ok:
            if motivo:
                log.info("comando negado: %s", motivo)
            self._encerrar()
            return
        if self.comandando:
            return
        self.comandando = True
        # Tira o cursor da borda: em modo comando ele fica parado, e parado no
        # meio da tela incomoda menos que colado na beirada.
        self.cliente.injetor.mover_para(*self.ancora)
        # O keydown dos modificadores ja' foi entregue a este PC e o keyup vai
        # ser bloqueado -- sem isto, Ctrl/Alt ficariam presos aqui.
        self.cliente.injetor.soltar_modificadores()
        log.info("este PC assumiu o comando: teclado e mouse daqui vao para o "
                 "outro lado")
        self.cliente.ao_mudar()

    def devolver(self, de: str, rel: float) -> None:
        """O cursor voltou para este PC, que continua sendo quem comanda."""
        c = self.cliente
        x, y = lay.ponto_de_entrada(de, rel, c.x0, c.y0, c.largura, c.altura,
                                    alv.MARGEM, c.monitores)
        self._encerrar()
        c.injetor.mover_para(x, y)
        log.info("cursor de volta a este PC (entrou pela %s)", de)

    def largar(self, motivo: str) -> None:
        """Para de comandar e avisa o servidor. Seguro de chamar a qualquer hora."""
        if not self.comandando:
            self._encerrar()
            return
        self.cliente.enfileirar({"t": "largar"})
        self._encerrar()
        log.info("comando devolvido ao servidor (%s)", motivo)

    def _encerrar(self) -> None:
        """Solta o bloqueio local. Nunca pode falhar: e' a saida de emergencia."""
        estava = self.comandando
        self.comandando = False
        self._liberado_em = time.monotonic()
        self._pedido_em = 0.0
        if estava:
            self.cliente.injetor.soltar_modificadores()
            self.cliente.ao_mudar()

    # -- panico -------------------------------------------------------------

    def _panico(self) -> None:
        if self.comandando:
            self.largar("atalho de panico")
            return
        if self.cliente.remoto:
            # Estamos sendo comandados e o dono deste PC quer o teclado de volta:
            # so' o servidor pode soltar, entao pedimos a ele.
            self.cliente.enfileirar({"t": "panico"})
            log.info("panico local: pedindo ao servidor que devolva o cursor")

    def _e_panico(self, ev: dict) -> bool:
        return (
            ev["vk"] == ew.VK_ESCAPE
            and ew.modificador_pressionado(ew.VK_CONTROL)
            and ew.modificador_pressionado(ew.VK_MENU)
            and ew.modificador_pressionado(ew.VK_SHIFT)
        )

    def _mandar(self, ev: dict) -> None:
        self.cliente.enfileirar({"t": "in", "ev": ev})
