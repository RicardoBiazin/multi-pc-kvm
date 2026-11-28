"""Regras da travessia de borda: quando o cursor PODE trocar de PC.

Ate' a v2.5 qualquer encosto numa borda com vizinho trocava de PC. Estas opcoes
(as do "Mouse sem Fronteiras" do PowerToys) dao ao usuario controle sobre isso:

* **troca ligada** -- liga e desliga a troca pela borda (atalho Ctrl+Alt+M);
* **bloquear cantos** -- encostar num canto da tela nao troca, para nao passar
  de PC sem querer ao buscar o botao de fechar de uma janela;
* **tecla de troca** -- so' troca com Shift ou Ctrl apertado;
* **tela cheia** -- nao troca enquanto um app esta' em tela cheia, salvo os
  da lista de liberados (Area de Trabalho Remota, jogo, apresentacao);
* **dar a volta** -- a borda do ultimo PC leva ao primeiro (ver Layout.vizinho).

Quem decide e' o servidor, no unico lugar por onde toda troca passa
(`borda.Controle`). Cada PC so' informa o contexto que so' ele conhece: o app em
tela cheia e o ponto da borda.

Tambem moram aqui as opcoes que o servidor repassa aos clientes e que nao sao de
borda: bloquear o protetor de tela e compartilhar a area de transferencia.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Ate' onde, ao longo da aresta, conta como canto: 4% do comprimento dela. Em
# fracao, e nao em pixels, porque o servidor decide pela posicao relativa que
# o cliente mandou, sem saber a resolucao dele.
FRACAO_DO_CANTO = 0.04

MODIFICADORES = ("nenhum", "shift", "ctrl")

# Chaves aceitas no config.json (`opcoes`) e o valor de cada uma sem config.
PADRAO = {
    "troca_ligada": True,
    "bloquear_cantos": False,
    "modificador": "nenhum",
    "ignorar_tela_cheia": False,
    "liberados": [],
    "dar_a_volta": False,
    "bloquear_protetor": False,
    "compartilhar_clipboard": True,
    "atalhos": True,
    "avisos_na_bandeja": True,
    "so_mesma_subrede": False,
    "validar_dns_reverso": False,
    "desenhar_cursor": False,
}

# O que os clientes recebem do servidor: o que vale para a SESSAO. As duas
# ultimas de seguranca valem so' no servidor, e `desenhar_cursor` e' de cada PC.
DO_SERVIDOR = ("troca_ligada", "bloquear_cantos", "modificador",
               "ignorar_tela_cheia", "liberados", "dar_a_volta",
               "bloquear_protetor", "compartilhar_clipboard", "atalhos",
               "avisos_na_bandeja")


def _como(tipo_padrao, valor):
    """Converte um valor vindo do config para o tipo do padrao, sem levantar."""
    if isinstance(tipo_padrao, bool):
        return bool(valor)
    if isinstance(tipo_padrao, list):
        if isinstance(valor, str):
            valor = valor.replace(";", ",").split(",")
        return [str(v).strip().lower() for v in (valor or []) if str(v).strip()]
    return valor


# -- atalhos de acao ---------------------------------------------------------
#
# Ctrl+Alt + letra. Sem Shift: Ctrl+Alt+Shift+Esc e' o panico, e Ctrl+Alt+1..9
# leva o cursor a um PC (ver borda.Controle._pc_do_atalho).
ATALHOS = {
    0x4D: "alternar_troca",    # M: liga/desliga a troca pela borda
    0x4B: "varias_maquinas",   # K: a mesma entrada vai a todos os PCs
    0x52: "reconectar",        # R: atualiza as conexoes
    0x4C: "bloquear_todos",    # L, duas vezes: bloqueia a sessao de todos
}
DUPLO_TOQUE = 1.5  # s para o segundo Ctrl+Alt+L (bloquear nao pode ser acidental)


class Atalhos:
    """Reconhece as acoes acima a partir de um evento de tecla."""

    def __init__(self, relogio):
        self._relogio = relogio
        self._primeiro_l = -1e9

    def detectar(self, vk: int, down: bool, ctrl: bool, alt: bool,
                 shift: bool) -> str | None:
        if not down or not (ctrl and alt) or shift:
            return None
        acao = ATALHOS.get(vk)
        if acao != "bloquear_todos":
            return acao
        agora = self._relogio()
        if agora - self._primeiro_l <= DUPLO_TOQUE:
            self._primeiro_l = -1e9
            return acao
        self._primeiro_l = agora
        return None


@dataclass
class Politica:
    troca_ligada: bool = True
    bloquear_cantos: bool = False
    modificador: str = "nenhum"
    ignorar_tela_cheia: bool = False
    liberados: list = field(default_factory=list)
    dar_a_volta: bool = False
    bloquear_protetor: bool = False
    compartilhar_clipboard: bool = True
    atalhos: bool = True
    avisos_na_bandeja: bool = True
    so_mesma_subrede: bool = False
    validar_dns_reverso: bool = False
    desenhar_cursor: bool = False

    @classmethod
    def de_dict(cls, d: dict | None) -> "Politica":
        p = cls()
        p.atualizar(d or {})
        return p

    def atualizar(self, d: dict) -> None:
        """Aceita so' chaves conhecidas, convertidas para o tipo certo."""
        for chave, padrao in PADRAO.items():
            if chave in d:
                setattr(self, chave, _como(padrao, d[chave]))
        if self.modificador not in MODIFICADORES:
            self.modificador = "nenhum"

    def para_dict(self) -> dict:
        return {chave: getattr(self, chave) for chave in PADRAO}

    def para_clientes(self) -> dict:
        d = self.para_dict()
        return {k: d[k] for k in DO_SERVIDOR}

    def decidir(self, direcao: str, rel: float, cheia: bool = False,
                exe: str = "", mods: frozenset | set = frozenset()) -> str:
        """"" se pode trocar; senao o motivo (curto, para o registro).

        `mods` e' o que esta' apertado no teclado de quem comanda: {"shift",
        "ctrl"}. `exe` e' o nome do programa em tela cheia, se houver.
        """
        if not self.troca_ligada:
            return "troca pela borda desligada"
        if self.bloquear_cantos and (rel < FRACAO_DO_CANTO
                                     or rel > 1 - FRACAO_DO_CANTO):
            return "canto da tela"
        if self.ignorar_tela_cheia and cheia and exe.lower() not in self.liberados:
            return f"aplicativo em tela cheia ({exe or 'desconhecido'})"
        if self.modificador != "nenhum" and self.modificador not in mods:
            return f"falta apertar {self.modificador.capitalize()}"
        return ""
