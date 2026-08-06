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
"""

from __future__ import annotations

import logging
import time

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
        self.conectados: set[str] = set()
        self.ao_trocar = lambda de, para: None  # a interface avisa na tela
        self._liberado_em = 0.0
        self._engolidas: set[int] = set()

    @property
    def remoto(self) -> bool:
        return self.atual != self.eu

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
            # Tira o cursor da borda por estetica: em modo remoto ele fica
            # parado, e parado no meio da tela incomoda menos que colado na
            # beirada. O movimento remoto nao depende disto.
            ew.mover_cursor(*self.ancora)
        anterior = self.atual
        self.atual = pc.nome
        self.enfileirar(pc.nome, {"t": "entrar", "de": aresta_de_chegada, "rel": rel})
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
        self.enfileirar(self.atual, {"t": "soltar"})
        anterior, self.atual = self.atual, self.eu
        self._liberado_em = time.monotonic()
        x, y = lay.ponto_de_entrada(aresta_de_chegada, rel, self.x0, self.y0,
                                    self.largura, self.altura, MARGEM,
                                    self.monitores)
        ew.mover_cursor(int(x), int(y))
        log.info("cursor -> %s (%s)", self.eu, motivo)
        self._anunciar_troca(anterior, self.eu)

    def largar(self, motivo: str) -> None:
        """Devolve o cursor a este PC sem depender de aresta (panico, queda)."""
        if not self.remoto:
            return
        self.enfileirar(self.atual, {"t": "soltar"})
        anterior, self.atual = self.atual, self.eu
        self._liberado_em = time.monotonic()
        ew.mover_cursor(*self.ancora)
        log.info("cursor -> %s (%s)", self.eu, motivo)
        self._anunciar_troca(anterior, self.eu)

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
        if not self.remoto:
            return
        self.enfileirar(self.atual, {"t": "mv", "dx": dx, "dy": dy})

    def tratar(self, ev: dict) -> bool:
        """True = engolir o evento (estamos controlando outro PC)."""
        tipo = ev["t"]

        if tipo == "key":
            if ev["down"] and self._e_panico(ev):
                self._engolir(ev["vk"])
                self.largar("atalho de panico")
                return True
            alvo = self._pc_do_atalho(ev)
            if alvo is not None:
                self._engolir(ev["vk"])
                self._pular_para(alvo)
                return True
            if ev["vk"] in self._engolidas:
                # keyup da tecla cujo keydown viramos atalho: nao pode escapar
                if not ev["down"]:
                    self._engolidas.discard(ev["vk"])
                return True
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
