"""Layout dos monitores: quem esta' ao lado de quem, e por onde o cursor entra.

Cada PC ocupa uma celula (coluna, linha) numa grade. O vizinho numa direcao e' o
PC mais proximo na mesma linha (ou coluna) daquele lado -- buracos na grade nao
atrapalham, entao o usuario pode espalhar os PCs como quiser na interface.
"""

from __future__ import annotations

from dataclasses import dataclass

DIRECOES = ("esquerda", "direita", "cima", "baixo")
DELTAS = {"esquerda": (-1, 0), "direita": (1, 0), "cima": (0, -1), "baixo": (0, 1)}
OPOSTA = {"esquerda": "direita", "direita": "esquerda",
          "cima": "baixo", "baixo": "cima"}


@dataclass
class PC:
    nome: str
    ip: str
    coluna: int
    linha: int
    servidor: bool = False

    @classmethod
    def de_dict(cls, d: dict) -> "PC":
        return cls(
            nome=str(d["nome"]).strip(),
            ip=str(d.get("ip", "")).strip(),
            coluna=int(d.get("coluna", 0)),
            linha=int(d.get("linha", 0)),
            servidor=bool(d.get("servidor", False)),
        )

    def para_dict(self) -> dict:
        return {"nome": self.nome, "ip": self.ip, "coluna": self.coluna,
                "linha": self.linha, "servidor": self.servidor}


class Layout:
    """Conjunto de PCs posicionados numa grade, com busca de vizinhos."""

    def __init__(self, pcs: list[PC]):
        self.pcs = pcs

    @classmethod
    def de_config(cls, cfg: dict) -> "Layout":
        return cls([PC.de_dict(d) for d in cfg.get("pcs", [])])

    def validar(self) -> list[str]:
        """Devolve a lista de problemas; vazia quer dizer configuracao usavel."""
        problemas = []
        if len(self.pcs) < 2:
            problemas.append("e' preciso pelo menos 2 PCs")
        nomes = [p.nome for p in self.pcs]
        if "" in nomes:
            problemas.append("ha' PC sem nome")
        if len(set(nomes)) != len(nomes):
            problemas.append("ha' nomes repetidos")
        celulas = [(p.coluna, p.linha) for p in self.pcs]
        if len(set(celulas)) != len(celulas):
            problemas.append("ha' dois PCs na mesma celula do layout")
        servidores = [p for p in self.pcs if p.servidor]
        if len(servidores) != 1:
            problemas.append("marque exatamente um PC como servidor "
                             "(o que tem o teclado e o mouse)")
        for p in self.pcs:
            if not p.servidor and not p.ip:
                problemas.append(f"{p.nome or '(sem nome)'} esta' sem IP")
        if servidores and not servidores[0].ip:
            problemas.append(f"{servidores[0].nome} e' o servidor e precisa do "
                             "proprio IP, para os outros o encontrarem")
        # Um PC sem nenhum vizinho fica inalcancavel pelo cursor.
        for p in self.pcs:
            if not any(self.vizinho(p.nome, d) for d in DIRECOES):
                problemas.append(f"{p.nome} nao faz fronteira com nenhum outro PC")
        return problemas

    def por_nome(self, nome: str) -> PC | None:
        return next((p for p in self.pcs if p.nome == nome), None)

    def por_ip(self, ip: str, ignorar: str = "") -> PC | None:
        """Resolve pelo IP quando o nome nao casa.

        E' o que evita o erro classico de o cliente se chamar 'esq' e no layout
        do servidor estar como 'PC-esq': o IP decide, e o cliente adota o nome
        do servidor. So' vale se um unico PC restar como candidato.

        `ignorar` tira um nome da busca -- o servidor passa o proprio, porque uma
        conexao que chega nunca e' dele mesmo (e no mesmo PC, em teste local, os
        dois IPs coincidem).
        """
        if not ip:
            return None
        candidatos = [p for p in self.pcs if p.ip == ip and p.nome != ignorar]
        return candidatos[0] if len(candidatos) == 1 else None

    def para_config(self) -> list[dict]:
        return [p.para_dict() for p in self.pcs]

    def servidor(self) -> PC | None:
        return next((p for p in self.pcs if p.servidor), None)

    def vizinho(self, nome: str, direcao: str) -> PC | None:
        """PC mais proximo daquele lado, na mesma linha (ou coluna)."""
        origem = self.por_nome(nome)
        if origem is None:
            return None
        dc, dl = DELTAS[direcao]
        if dl == 0:
            candidatos = [p for p in self.pcs
                          if p.linha == origem.linha
                          and (p.coluna - origem.coluna) * dc > 0]
            if not candidatos:
                return None
            return min(candidatos, key=lambda p: abs(p.coluna - origem.coluna))
        candidatos = [p for p in self.pcs
                      if p.coluna == origem.coluna
                      and (p.linha - origem.linha) * dl > 0]
        if not candidatos:
            return None
        return min(candidatos, key=lambda p: abs(p.linha - origem.linha))


# -- geometria de entrada/saida ---------------------------------------------


def direcao_de_saida(x: float, y: float, x0: int, y0: int,
                     largura: int, altura: int) -> str | None:
    """Por qual aresta o ponto saiu da tela, ou None se ainda esta' dentro."""
    if x < x0:
        return "esquerda"
    if x > x0 + largura - 1:
        return "direita"
    if y < y0:
        return "cima"
    if y > y0 + altura - 1:
        return "baixo"
    return None


def relativo_na_aresta(x: float, y: float, aresta: str, x0: int, y0: int,
                       largura: int, altura: int) -> float:
    """Posicao 0..1 ao longo da aresta -- o que preserva a altura do cursor
    quando ele reaparece na tela seguinte."""
    if aresta in ("esquerda", "direita"):
        bruto = (y - y0) / max(1, altura - 1)
    else:
        bruto = (x - x0) / max(1, largura - 1)
    return min(1.0, max(0.0, bruto))


def ponto_de_entrada(aresta: str, rel: float, x0: int, y0: int, largura: int,
                     altura: int, margem: int = 4) -> tuple[float, float]:
    """Onde o cursor reaparece ao chegar por `aresta`, `margem` px para dentro.

    `aresta` pode ser "centro": e' o caso do atalho de teclado, que pula direto
    para um PC sem vir de borda nenhuma.
    """
    rel = min(1.0, max(0.0, rel))
    if aresta == "centro":
        return x0 + largura / 2, y0 + altura / 2
    if aresta == "esquerda":
        return x0 + margem, y0 + rel * (altura - 1)
    if aresta == "direita":
        return x0 + largura - 1 - margem, y0 + rel * (altura - 1)
    if aresta == "cima":
        return x0 + rel * (largura - 1), y0 + margem
    return x0 + rel * (largura - 1), y0 + altura - 1 - margem
