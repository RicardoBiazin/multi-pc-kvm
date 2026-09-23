"""Quantas teclas e cliques caem em cada PC, por dia.

Conta no SERVIDOR, e nao em cada maquina. E' ele que decide o destino de todo
evento -- `self.atual` quando o cursor esta' noutro PC, `self.eu` quando esta'
aqui --, entao um unico ponto de contagem cobre a rede toda e nada precisa
trafegar a mais. O preco e' que a janela de um CLIENTE nao tem numeros para
mostrar: quem conta e' o servidor.

GRAVA EM DISCO DE PROPOSITO. Com o inicio automatico ligado, o agente morre e
nasce a cada bloqueio de tela (ver servico.py) -- contagem so' na memoria
zeraria varias vezes por dia e o numero nao valeria nada.

A media e' por MINUTO ATIVO, nao por minuto corrido. Dividir o dia inteiro pelo
relogio daria quase zero em qualquer jornada normal: quem digitou 3000 teclas
das 8h as 18h veria "5 por minuto". Contando so' os minutos em que houve alguma
coisa, o numero responde a pergunta que se faz de verdade -- qual o ritmo
enquanto se esta' trabalhando.
"""

from __future__ import annotations

import datetime
import json
import logging
import pathlib
import threading
import time

log = logging.getLogger("contador")

# O disco so' e' tocado de tempos em tempos: `registrar` roda DENTRO do callback
# do hook de teclado e mouse, e gravar json a cada tecla seria absurdo.
INTERVALO_DE_GRAVACAO = 15.0


def _hoje() -> str:
    return datetime.date.today().isoformat()


class Contador:
    """Contagem diaria de teclas e cliques por PC.

    Tudo o que `registrar` faz e' somar inteiros sob um lock -- ele e' chamado
    do caminho do hook, onde o Windows da' 300 ms antes de desinstalar tudo.
    Gravar, virar o dia e formatar ficam fora dali.
    """

    def __init__(self, caminho: pathlib.Path, ativo: bool = True):
        self.caminho = caminho
        self.ativo = ativo
        self._lock = threading.RLock()
        self._dia = _hoje()
        self._pcs: dict[str, dict[str, int]] = {}
        self._minutos: dict[str, int] = {}   # minutos ativos por PC
        self._ultimo_minuto: dict[str, int] = {}
        # Teclas fisicamente presas agora, por codigo virtual. E' o que separa
        # "digitou" de "segurou" -- ver `registrar`. Global e nao por PC: o
        # teclado e' um so', e uma tecla esta' presa ou nao, independente de
        # para qual PC o toque foi.
        self._pressionadas: set[int] = set()
        self._sujo = False
        self._carregar()
        threading.Thread(target=self._gravar_de_tempos_em_tempos,
                         name="contador", daemon=True).start()

    # -- contagem -----------------------------------------------------------

    def registrar(self, pc: str, ev: dict) -> None:
        """Soma uma tecla ou um clique para `pc`. Ignora o resto."""
        tipo = ev.get("t")
        if tipo == "key" and not ev.get("down"):
            # A SOLTURA e' processada mesmo com a contagem parada: e' ela que
            # limpa o registro de "esta' pressionada". Sem isso, religar a
            # contagem com uma tecla ainda presa engoliria o proximo toque dela.
            with self._lock:
                self._pressionadas.discard(ev.get("vk"))
            return
        if not self.ativo or not pc:
            return
        if not ev.get("down"):
            return  # so' a descida conta: senao cada tecla valeria por duas
        if tipo == "key":
            # SEGURAR A TECLA NAO E' DIGITAR VARIAS VEZES. O Windows repete o
            # keydown enquanto a tecla fica presa (auto-repeat), e sem esta
            # guarda uma tecla segurada por dois segundos entrava como dezenas
            # de toques -- o numero do painel deixava de significar alguma
            # coisa. O hook de baixo nivel nao marca o repeat, entao quem
            # lembra somos nos: conta so' a descida que vem depois de uma
            # soltura.
            vk = ev.get("vk")
            with self._lock:
                if vk in self._pressionadas:
                    return
                self._pressionadas.add(vk)
            campo = "teclas"
        elif tipo == "btn":
            campo = "cliques"  # botao de mouse nao tem auto-repeat
        else:
            return  # movimento e roda nao sao "digitacao" nem "clique"

        agora = int(time.time() // 60)
        with self._lock:
            if self._dia != _hoje():
                self._virar_o_dia()
            conta = self._pcs.setdefault(pc, {"teclas": 0, "cliques": 0})
            conta[campo] += 1
            if self._ultimo_minuto.get(pc) != agora:
                self._ultimo_minuto[pc] = agora
                self._minutos[pc] = self._minutos.get(pc, 0) + 1
            self._sujo = True

    # -- leitura ------------------------------------------------------------

    def resumo(self) -> list[dict]:
        """Uma linha por PC: nome, teclas, cliques e media por minuto ativo."""
        with self._lock:
            if self._dia != _hoje():
                self._virar_o_dia()
            linhas = []
            for pc, conta in sorted(self._pcs.items()):
                minutos = max(1, self._minutos.get(pc, 0))
                total = conta["teclas"] + conta["cliques"]
                linhas.append({
                    "pc": pc,
                    "teclas": conta["teclas"],
                    "cliques": conta["cliques"],
                    "minutos": self._minutos.get(pc, 0),
                    "por_minuto": total / minutos,
                })
            return linhas

    def limpar(self) -> None:
        with self._lock:
            self._dia = _hoje()
            self._pcs.clear()
            self._minutos.clear()
            self._ultimo_minuto.clear()
            self._pressionadas.clear()
            self._sujo = True
        self.gravar()
        log.info("contagem de uso zerada")

    # -- disco --------------------------------------------------------------

    def _virar_o_dia(self) -> None:
        """Chamado com o lock tomado. O dia anterior fica no arquivo do dia."""
        anterior = self._dia
        self._arquivar(anterior)
        self._dia = _hoje()
        self._pcs.clear()
        self._minutos.clear()
        self._ultimo_minuto.clear()
        self._sujo = True
        log.info("contagem de uso virou o dia (%s -> %s)", anterior, self._dia)

    def _arquivar(self, dia: str) -> None:
        """Guarda o fechamento do dia, para nao se perder na virada."""
        if not self._pcs:
            return
        historico = self.caminho.with_name("uso-historico.jsonl")
        linha = json.dumps({"dia": dia, "pcs": self._pcs,
                            "minutos": self._minutos}, ensure_ascii=False)
        try:
            with open(historico, "a", encoding="utf-8") as f:
                f.write(linha + "\n")
        except OSError:
            log.warning("nao consegui arquivar a contagem de %s", dia,
                        exc_info=True)

    def _carregar(self) -> None:
        try:
            dados = json.loads(self.caminho.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return  # primeira vez, ou arquivo estragado: comeca do zero
        if dados.get("dia") != self._dia:
            return  # contagem de outro dia: nao mistura
        self._pcs = {str(k): {"teclas": int(v.get("teclas", 0)),
                              "cliques": int(v.get("cliques", 0))}
                     for k, v in (dados.get("pcs") or {}).items()}
        self._minutos = {str(k): int(v)
                         for k, v in (dados.get("minutos") or {}).items()}

    def gravar(self) -> None:
        with self._lock:
            if not self._sujo:
                return
            dados = {"dia": self._dia, "pcs": self._pcs,
                     "minutos": self._minutos}
            self._sujo = False
        try:
            self.caminho.write_text(
                json.dumps(dados, indent=2, ensure_ascii=False),
                encoding="utf-8")
        except OSError:
            log.warning("nao consegui gravar a contagem de uso", exc_info=True)

    def _gravar_de_tempos_em_tempos(self) -> None:
        pausa = threading.Event()  # daemon: morre com o processo
        while not pausa.wait(INTERVALO_DE_GRAVACAO):
            try:
                self.gravar()
            except Exception:
                log.debug("falha gravando a contagem", exc_info=True)
