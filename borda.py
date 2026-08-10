"""Travessia de borda e roteamento do cursor entre N PCs (lado servidor).

O servidor guarda `atual`: de quem e' o cursor neste momento. Enquanto for dele
proprio, os hooks deixam tudo passar. Quando o cursor toca uma aresta que tem
vizinho no layout, entramos em modo remoto: os hooks passam a *bloquear* mouse e
teclado e o cursor vai para uma ancora no meio da tela.

O movimento do cursor remoto vem do **Raw Input** (`delta_bruto`), que entrega
deslocamento relativo. O hook de baixo nivel serve para outras tres coisas:
detectar o encosto na borda em modo local, bloquear o input, e repassar botoes,
roda e teclas. Ele nao serve para movimento porque so' informa posicao absoluta,
ja' presa aos limites da tela -- encostado numa borda ela para de variar.

Quem decide o retorno e' o servidor: o cliente so' avisa "sai' por tal aresta,
nesta altura" e aqui se procura o vizinho daquele lado -- que pode ser o proprio
servidor, outro cliente, ou ninguem (ai' o cursor volta para o mesmo cliente).

Comando bidirecional (v1.2)
---------------------------
`atual` diz de quem e' o **cursor**; `comandante`, de quem e' o **teclado e o
mouse** que estao mandando. Ate' a v1.1 o comandante era sempre o servidor. Agora
um cliente com periferico proprio pode pedir o comando, e nesse caso os papeis se
invertem: o servidor vira alvo (usa o `alvo.Alvo`, igual ao cliente) e o input
chega pela rede em vez de vir dos hooks. O roteamento continua todo aqui -- e' o
que impede os dois lados de divergirem.

Qualquer input **fisico** neste PC traz o comando de volta para ca'. Nao ha' risco
de laco com a injecao: o hook descarta o que vem com LLMHF_INJECTED, que inclui
o nosso proprio SetCursorPos.
"""

from __future__ import annotations

import logging
import time

import alvo as alv
import entrada_win as ew
import layout as lay

log = logging.getLogger("borda")

TRAVA_APOS_RETORNO = 0.4  # s ignorando a borda, para nao reentrar em seguida
MARGEM = 4  # px dentro da tela onde o cursor reaparece

# Destino interno: nao vai para a rede, e' uma acao no proprio servidor.
LOCAL = "\x00local"


class Controle:
    """Decide, dentro do callback do hook, o que e' local e o que vai pra rede.

    `enfileirar(destino, msg)` -- `destino` e' o nome do PC, ou `LOCAL`.
    """

    def __init__(self, layout: lay.Layout, eu: str, enfileirar):
        self.layout = layout
        self.eu = eu
        self.enfileirar = enfileirar
        self.x0, self.y0, self.largura, self.altura = ew.geometria_virtual()
        self.monitores = [m[:4] for m in ew.monitores()]
        # Centro do monitor PRINCIPAL, nao do retangulo todo: com mais de um
        # monitor o centro do retangulo pode cair num buraco entre telas, ou
        # exatamente na divisa entre duas.
        centro = lay.centro_principal(self.monitores, self.x0, self.y0,
                                      self.largura, self.altura)
        self.ancora = (int(centro[0]), int(centro[1]))
        self.atual = eu
        # Quem esta' com o teclado e o mouse na mao. Comeca sendo este PC; um
        # cliente com periferico proprio pode pedir (ver `pedir_comando`).
        self.comandante = eu
        # Usado so' quando quem comanda e' outro PC: ai' este aqui vira alvo.
        self.alvo = alv.Alvo(self.x0, self.y0, self.largura, self.altura,
                             self.monitores)
        self.conectados: set[str] = set()
        self.ao_trocar = lambda de, para: None  # a interface avisa na tela
        self._liberado_em = 0.0
        self._engolidas: set[int] = set()

    @property
    def remoto(self) -> bool:
        return self.atual != self.eu

    @property
    def sou_o_alvo(self) -> bool:
        """Este PC esta' recebendo cursor comandado de fora."""
        return self.alvo.ativo and self.comandante != self.eu

    # -- transicoes ---------------------------------------------------------

    def _assumir(self, pc: lay.PC, aresta_de_chegada: str, rel: float) -> None:
        if self.remoto:
            self.enfileirar(self.atual, {"t": "soltar"})
        else:
            # Solta os modificadores locais: o keydown ja' foi entregue a este PC
            # e o keyup vai ser bloqueado, o que deixaria Ctrl/Alt preso aqui.
            # Vai pela fila porque chamar SendInput de dentro do callback do hook
            # e' re-entrar na pilha de input -- quem executa e' a thread de envio.
            self.enfileirar(LOCAL, {"t": "soltar_local"})
            if self.alvo.ativo:
                self.alvo.parar()
            elif self.comandante == self.eu:
                # Tira o cursor da borda por estetica: em modo remoto ele fica
                # parado, e parado no meio da tela incomoda menos que colado na
                # beirada. O movimento remoto nao depende disto.
                ew.mover_cursor(*self.ancora)
        anterior = self.atual
        self.atual = pc.nome
        if pc.nome == self.comandante:
            # O cursor voltou para quem comanda: la' nao ha' injecao nenhuma, e'
            # so' largar o bloqueio e devolver o mouse local.
            self.enfileirar(pc.nome, {"t": "devolver", "de": aresta_de_chegada,
                                      "rel": rel})
            log.info("cursor -> %s (de volta a quem comanda, pela %s)",
                     pc.nome, aresta_de_chegada)
        else:
            self.enfileirar(pc.nome, {"t": "entrar", "de": aresta_de_chegada,
                                      "rel": rel})
            log.info("cursor -> %s (entrando pela %s, rel=%.2f)",
                     pc.nome, aresta_de_chegada, rel)
        self._anunciar_troca(anterior, pc.nome)

    def _anunciar_troca(self, de: str, para: str) -> None:
        if de == para:
            return
        try:
            self.ao_trocar(de, para)
        except Exception:
            log.debug("falha ao anunciar a troca", exc_info=True)

    def voltar_para_mim(self, aresta_de_chegada: str, rel: float,
                        motivo: str = "borda") -> None:
        if not self.remoto:
            return
        if self.comandante != self.eu:
            self._virar_alvo(aresta_de_chegada, rel, motivo)
            return
        self.enfileirar(self.atual, {"t": "soltar"})
        anterior, self.atual = self.atual, self.eu
        self._liberado_em = time.monotonic()
        x, y = lay.ponto_de_entrada(aresta_de_chegada, rel, self.x0, self.y0,
                                    self.largura, self.altura, MARGEM,
                                    self.monitores)
        ew.mover_cursor(int(x), int(y))
        log.info("cursor -> %s (%s)", self.eu, motivo)
        self._anunciar_troca(anterior, self.eu)

    def _virar_alvo(self, aresta_de_chegada: str, rel: float,
                    motivo: str) -> None:
        """Este PC passa a RECEBER o cursor, comandado pelo teclado de outro.

        E' o espelho do que o cliente faz em `cliente.Cliente._aplicar`: a
        posicao virtual passa a mandar, e o cursor real vira injecao. A injecao
        nao pode sair daqui -- pode estar rodando dentro do callback do hook --,
        entao ela vai pela fila, como `por_cursor`.
        """
        anterior = self.atual
        if anterior != self.eu:
            self.enfileirar(anterior, {"t": "soltar"})
        self.atual = self.eu
        self._liberado_em = time.monotonic()
        x, y = self.alvo.entrar(aresta_de_chegada, rel)
        self.enfileirar(LOCAL, {"t": "por_cursor", "x": x, "y": y})
        log.info("cursor -> %s (%s, comandado por '%s')", self.eu, motivo,
                 self.comandante)
        self._anunciar_troca(anterior, self.eu)

    def largar(self, motivo: str) -> None:
        """Devolve cursor e comando a este PC, sem depender de aresta.

        E' a saida de emergencia (panico, queda de cliente): depois dela o
        teclado e o mouse deste PC voltam a mandar, aconteca o que acontecer.
        """
        self._retomar_comando(motivo, avisar=True)
        if not self.remoto:
            return
        self.enfileirar(self.atual, {"t": "soltar"})
        anterior, self.atual = self.atual, self.eu
        self._liberado_em = time.monotonic()
        self.alvo.parar()
        ew.mover_cursor(*self.ancora)
        log.info("cursor -> %s (%s)", self.eu, motivo)
        self._anunciar_troca(anterior, self.eu)

    # -- de quem e' o teclado e o mouse -------------------------------------

    def pedir_comando(self, origem: str, direcao: str, rel: float) -> None:
        """Um cliente encostou o cursor DELE numa aresta e quer comandar o vizinho."""
        vizinho = self.layout.vizinho(origem, direcao)
        if vizinho is None:
            self.enfileirar(origem, {"t": "comando", "ok": False,
                                     "motivo": "nao ha' PC desse lado no layout"})
            return
        if vizinho.nome != self.eu and vizinho.nome not in self.conectados:
            self.enfileirar(origem, {"t": "comando", "ok": False,
                                     "motivo": f"{vizinho.nome} nao esta' conectado"})
            return
        if self.comandante != origem:
            anterior = self.comandante
            self.comandante = origem
            if anterior != self.eu:
                self.enfileirar(anterior, {"t": "comando", "ok": False,
                                           "motivo": f"'{origem}' assumiu o comando"})
            log.info("o teclado e o mouse de '%s' assumiram o comando", origem)
        self.enfileirar(origem, {"t": "comando", "ok": True})
        chegada = lay.OPOSTA[direcao]
        if vizinho.nome == self.eu:
            self._virar_alvo(chegada, rel, "pedido de comando")
        else:
            self._assumir(vizinho, chegada, rel)

    def devolveu_o_comando(self, origem: str, motivo: str = "") -> None:
        """O cliente que comandava desistiu (panico dele, ou o programa fechou)."""
        if self.comandante != origem:
            return
        self._retomar_comando(motivo or f"'{origem}' devolveu o comando",
                              avisar=False)

    def _retomar_comando(self, motivo: str, avisar: bool) -> None:
        """O teclado e o mouse deste PC voltam a mandar."""
        anterior = self.comandante
        if anterior == self.eu:
            return
        self.comandante = self.eu
        self.alvo.parar()
        if avisar:
            self.enfileirar(anterior, {"t": "comando", "ok": False,
                                       "motivo": motivo})
        log.info("comando de volta para '%s' (%s)", self.eu, motivo)

    def do_comandante(self, origem: str, ev: dict) -> None:
        """Input que chegou pela rede, vindo do PC que esta' comandando."""
        if origem != self.comandante:
            return  # evento atrasado de quem ja' nao comanda
        if self.atual != self.eu:
            self.enfileirar(self.atual, ev)  # so' repassa: o alvo e' outro
            return
        if not self.alvo.ativo:
            return  # nao somos alvo (ainda): nada a injetar
        if ev.get("t") != "mv":
            self.enfileirar(LOCAL, {"t": "injetar", "ev": ev})
            return
        self.alvo.mover(ev["dx"], ev["dy"])
        saida = self.alvo.saida()
        if saida is None:
            x, y = self.alvo.ponto()
            self.enfileirar(LOCAL, {"t": "por_cursor", "x": x, "y": y})
            return
        direcao, rel = saida
        vizinho = self.layout.vizinho(self.eu, direcao)
        if vizinho is None or (vizinho.nome != self.comandante
                               and vizinho.nome not in self.conectados):
            return  # nao ha' para onde ir: o cursor fica encostado
        self._assumir(vizinho, lay.OPOSTA[direcao], rel)

    def saiu_do_cliente(self, origem: str, direcao: str, rel: float) -> None:
        """Um cliente avisou que o cursor passou de uma das arestas dele."""
        if self.atual != origem:
            return  # aviso atrasado de quem ja' nao tem o cursor
        vizinho = self.layout.vizinho(origem, direcao)
        chegada = lay.OPOSTA[direcao]

        if vizinho is None:
            # Nao ha' PC daquele lado: o cursor fica onde estava, encostado.
            self.enfileirar(origem, {"t": "entrar", "de": direcao, "rel": rel})
            return
        if vizinho.nome == self.eu:
            self.voltar_para_mim(chegada, rel)
            return
        if vizinho.nome not in self.conectados:
            log.info("%s esta' desconectado; cursor fica em %s",
                     vizinho.nome, origem)
            self.enfileirar(origem, {"t": "entrar", "de": direcao, "rel": rel})
            return
        self._assumir(vizinho, chegada, rel)

    def cliente_caiu(self, nome: str) -> None:
        self.conectados.discard(nome)
        if self.comandante == nome:
            # Quem comandava sumiu: sem isto o servidor ficaria esperando um
            # input que nao vem mais, com o teclado local ainda bloqueado.
            self._retomar_comando(f"'{nome}' desconectou", avisar=False)
        if self.atual == nome:
            self.largar(f"{nome} desconectou")

    # -- callback do hook ---------------------------------------------------

    def delta_bruto(self, dx: int, dy: int) -> None:
        """Deslocamento relativo vindo do Raw Input.

        E' a unica fonte confiavel de movimento para o cursor remoto. O hook de
        baixo nivel entrega posicao absoluta, ja' presa aos limites da tela:
        encostado numa borda ela para de variar, e derivar delta de uma ancora
        exige que o SetCursorPos tenha sido aplicado -- o que, chamado de dentro
        do proprio callback do hook, nao acontece de forma confiavel.
        """
        if not self.remoto or self.comandante != self.eu:
            return  # quem comanda e' outro: este mouse nao move o cursor de la'
        self.enfileirar(self.atual, {"t": "mv", "dx": dx, "dy": dy})

    def tratar(self, ev: dict) -> bool:
        """True = engolir o evento (estamos controlando outro PC)."""
        tipo = ev["t"]

        if tipo == "key":
            if ev["down"] and self._e_panico(ev):
                self._engolir(ev["vk"])
                self.largar("atalho de panico")
                return True
            destino = self._pc_do_atalho(ev)
            if destino is not None:
                self._engolir(ev["vk"])
                # O atalho e' deste teclado: se outro PC comandava, ele perde.
                self._retomar_comando("atalho usado neste PC", avisar=True)
                self._pular_para(destino)
                return True
            if ev["vk"] in self._engolidas:
                # keyup da tecla cujo keydown viramos atalho: nao pode escapar
                if not ev["down"]:
                    self._engolidas.discard(ev["vk"])
                return True

        # Daqui para baixo o evento e' input FISICO deste PC (o injetado nao
        # chega: o hook descarta LLMHF_INJECTED e a nossa MARCA). Se quem
        # comanda e' outro, mexer aqui traz o comando de volta -- e' o unico
        # jeito de o dono deste teclado retomar o que e' dele.
        if self.comandante != self.eu:
            self._comando_volta_por_input()
            return False

        if tipo == "key":
            if not self.remoto:
                return False
            self.enfileirar(self.atual, ev)
            return True

        if tipo == "mv":
            x, y = ev["pos"]
            if not self.remoto:
                return self._talvez_atravessar(x, y)
            # Em modo remoto o movimento vem do Raw Input (delta_bruto); aqui so'
            # engolimos o evento para nao mexer no cursor deste PC.
            return True

        # botoes e roda
        if not self.remoto:
            return False
        self.enfileirar(self.atual, ev)
        return True

    def _comando_volta_por_input(self) -> None:
        """Alguem mexeu no teclado/mouse deste PC enquanto outro comandava."""
        self._retomar_comando("mexeram no teclado/mouse deste PC", avisar=True)
        if self.remoto:
            self.enfileirar(self.atual, {"t": "soltar"})
            anterior, self.atual = self.atual, self.eu
            self._anunciar_troca(anterior, self.eu)
        # Sem mover o cursor: ele ja' esta' onde a mao do usuario o levou, e um
        # SetCursorPos aqui daria um pulo em plena travessia.
        self._liberado_em = time.monotonic()

    def _talvez_atravessar(self, x: int, y: int) -> bool:
        if time.monotonic() - self._liberado_em < TRAVA_APOS_RETORNO:
            return False
        direcao = self._aresta_encostada(x, y)
        if direcao is None:
            return False
        vizinho = self.layout.vizinho(self.eu, direcao)
        if vizinho is None or vizinho.nome not in self.conectados:
            return False
        rel = lay.relativo_na_aresta(x, y, direcao, self.x0, self.y0,
                                     self.largura, self.altura)
        self._assumir(vizinho, lay.OPOSTA[direcao], rel)
        return True

    def _aresta_encostada(self, x: int, y: int) -> str | None:
        """O Windows ja' prende o cursor na tela, entao 'encostado' e' 'na aresta'."""
        if x >= self.x0 + self.largura - 1:
            return "direita"
        if x <= self.x0:
            return "esquerda"
        if y >= self.y0 + self.altura - 1:
            return "baixo"
        if y <= self.y0:
            return "cima"
        return None

    def _engolir(self, vk: int) -> None:
        self._engolidas.add(vk)

    def _pc_do_atalho(self, ev: dict) -> lay.PC | None:
        """Ctrl+Alt+1..9 leva o cursor direto para o N-esimo PC da lista.

        Sem Shift, para nao colidir com o atalho de panico (Ctrl+Alt+Shift+Esc).
        """
        if not ev["down"] or not (0x31 <= ev["vk"] <= 0x39):
            return None
        if not (ew.modificador_pressionado(ew.VK_CONTROL)
                and ew.modificador_pressionado(ew.VK_MENU)):
            return None
        if ew.modificador_pressionado(ew.VK_SHIFT):
            return None
        indice = ev["vk"] - 0x31
        if indice >= len(self.layout.pcs):
            return None
        return self.layout.pcs[indice]

    def _pular_para(self, pc: lay.PC) -> None:
        if pc.nome == self.eu:
            self.largar(f"atalho Ctrl+Alt+{self._indice(pc) + 1}")
            return
        if pc.nome == self.atual:
            return
        if pc.nome not in self.conectados:
            log.info("atalho ignorado: '%s' nao esta' conectado", pc.nome)
            return
        # Vem de atalho, nao de borda: o cursor aparece no meio da tela.
        self._assumir(pc, "centro", 0.5)

    def _indice(self, pc: lay.PC) -> int:
        return next((i for i, p in enumerate(self.layout.pcs)
                     if p.nome == pc.nome), 0)

    def _e_panico(self, ev: dict) -> bool:
        """Ctrl+Alt+Shift+Esc devolve o teclado e o mouse a este PC."""
        return (
            ev["vk"] == ew.VK_ESCAPE
            and ew.modificador_pressionado(ew.VK_CONTROL)
            and ew.modificador_pressionado(ew.VK_MENU)
            and ew.modificador_pressionado(ew.VK_SHIFT)
        )
