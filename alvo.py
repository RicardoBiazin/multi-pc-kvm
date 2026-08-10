"""Lado que RECEBE o cursor: posicao virtual, trava de entrada e saida por aresta.

Ate' a v1.1 so' o cliente podia ser alvo, e esta conta morava dentro dele. Com o
comando bidirecional o **servidor tambem vira alvo** -- quando quem comanda e' o
teclado de um cliente --, e a conta passou a ser exatamente a mesma nos dois
lados. Fica aqui uma vez so'.

Nao injeta nada e nao fala com a rede: so' guarda onde o cursor virtual esta' e
responde "saiu por tal aresta" ou "esta' neste ponto". Quem injeta e' o chamador,
que sabe em que thread pode fazer isso -- chamar SendInput de dentro do callback
de um hook e' re-entrar na pilha de input, e nao e' confiavel.
"""

from __future__ import annotations

import time

import layout as lay

MARGEM = 4  # px dentro da tela onde o cursor aparece ao chegar
# Logo apos entrar, o cursor esta' a MARGEM px da borda e qualquer tremor de mao
# o devolveria. A trava cai por distancia (o normal: quem atravessa continua o
# movimento para dentro) OU por tempo (para quem atravessa e para na hora).
# Sem o criterio de tempo, atravessar e querer voltar em seguida ficaria preso.
FOLGA_DE_ENTRADA = 40  # px
TRAVA_DE_ENTRADA = 0.6  # s


class Alvo:
    """Posicao virtual do cursor neste PC enquanto ele e' comandado de fora."""

    def __init__(self, x0: int, y0: int, largura: int, altura: int,
                 monitores: list | None = None):
        self.x0, self.y0 = x0, y0
        self.largura, self.altura = largura, altura
        # Retangulos reais das telas: com mais de um monitor o retangulo que os
        # envolve tem buracos, e o cursor nao pode parar neles.
        self.monitores = monitores or []
        self.ativo = False
        self.vx = 0.0
        self.vy = 0.0
        self.aresta_de_entrada = ""
        self.pode_voltar = True
        self.entrou_em = 0.0

    # -- transicoes ---------------------------------------------------------

    def entrar(self, de: str, rel: float) -> tuple[float, float]:
        """O cursor chegou por `de`. Devolve o ponto onde ele deve aparecer."""
        self.vx, self.vy = lay.ponto_de_entrada(
            de, rel, self.x0, self.y0, self.largura, self.altura, MARGEM,
            self.monitores)
        # O cursor entra a poucos px da borda: qualquer tremida na mao o jogaria
        # de volta na hora. So' liberamos a saida por esta aresta depois que ele
        # se afastar dela.
        self.aresta_de_entrada = de
        self.pode_voltar = False
        self.entrou_em = time.monotonic()
        self.ativo = True
        return self.vx, self.vy

    def parar(self) -> None:
        self.ativo = False
        self.aresta_de_entrada = ""
        self.pode_voltar = True

    # -- movimento ----------------------------------------------------------

    def mover(self, dx: float, dy: float) -> None:
        self.vx += dx
        self.vy += dy

    def saida(self) -> tuple[str, float] | None:
        """(aresta, posicao relativa) se o cursor saiu; None se continua aqui.

        Em qualquer dos casos a posicao virtual fica presa dentro da tela: quem
        decide para onde o cursor vai e' o servidor, e ate' ele responder o
        cursor nao pode ficar num ponto que nao existe.
        """
        direcao = lay.direcao_de_saida(self.vx, self.vy, self.x0, self.y0,
                                       self.largura, self.altura)
        if direcao is not None and not self._pode_sair_por(direcao):
            direcao = None  # ainda colado na aresta por onde acabou de entrar
        if direcao is not None:
            rel = lay.relativo_na_aresta(self.vx, self.vy, direcao, self.x0,
                                         self.y0, self.largura, self.altura)
            self._prender()
            return direcao, rel
        self._prender()
        # Buraco entre monitores: o Windows prenderia o cursor na tela mais
        # proxima, e a nossa posicao virtual ficaria adiantada. Puxamos nos dois
        # para eles nao divergirem.
        self.vx, self.vy = lay.ponto_visivel(self.vx, self.vy, self.monitores)
        self._talvez_liberar()
        return None

    def ponto(self) -> tuple[float, float]:
        return self.vx, self.vy

    # -- trava da aresta de entrada -----------------------------------------

    def _prender(self) -> None:
        self.vx = min(self.x0 + self.largura - 1, max(self.x0, self.vx))
        self.vy = min(self.y0 + self.altura - 1, max(self.y0, self.vy))

    def _pode_sair_por(self, direcao: str) -> bool:
        """Trava so' a aresta por onde o cursor acabou de entrar."""
        if self.pode_voltar or direcao != self.aresta_de_entrada:
            return True
        return time.monotonic() - self.entrou_em >= TRAVA_DE_ENTRADA

    def _talvez_liberar(self) -> None:
        if self.pode_voltar or not self.aresta_de_entrada:
            return
        if self.aresta_de_entrada == "esquerda":
            distancia = self.vx - self.x0
        elif self.aresta_de_entrada == "direita":
            distancia = (self.x0 + self.largura - 1) - self.vx
        elif self.aresta_de_entrada == "cima":
            distancia = self.vy - self.y0
        else:
            distancia = (self.y0 + self.altura - 1) - self.vy
        if distancia >= FOLGA_DE_ENTRADA:
            self.pode_voltar = True
