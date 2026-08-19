"""Liga e desliga o papel certo (servidor ou cliente) numa thread de fundo.

A interface e o modo console falam so' com esta classe -- e' ela que sabe qual
papel esta' maquina tem, segundo o layout.

E' tambem onde mora a trava de instancia unica: dois motores na mesma maquina
travam o teclado, e o jeito de descobrir isso e' penoso (ver `_tomar_a_trava`).
"""

from __future__ import annotations

import logging
import threading

import win32api
import win32event
import winerror

import cliente
import layout as lay
import servidor

log = logging.getLogger("motor")

# Global e nao Local: o agente do inicio automatico roda na sessao 0 como
# SYSTEM e a janela na sessao do usuario -- num nome local eles nao se veriam.
TRAVA = r"Global\MultiPCKVM-motor"


class JaRodando(RuntimeError):
    """Outro motor desta maquina ja' tem a trava.

    Tipo proprio, e nao ValueError junto com "configuracao incompleta": quem
    chama precisa dizer ao usuario a coisa certa. Foi uma mensagem errada aqui
    que fez esta falha custar caro para entender.
    """


def _tomar_a_trava(nome: str = TRAVA):
    """Mutex de instancia unica. Devolve (handle, consegui).

    Dois motores nesta maquina instalam dois jogos de hooks de teclado e mouse,
    e disputam a mesma porta: o teclado trava e o outro PC entra e sai numa
    reconexao sem fim. Aconteceu de verdade -- a janela auto-iniciava pelo
    `iniciar_ao_abrir` e o inicio automatico subia o segundo servidor por cima.
    Nenhuma mensagem de erro apontava para a causa.

    Se nem der para criar o mutex (janela sem elevacao nao entra no namespace
    Global), seguimos em frente: e' uma trava, nao um requisito.
    """
    try:
        trava = win32event.CreateMutex(None, True, nome)
    except Exception:
        log.warning("nao consegui criar a trava de instancia unica",
                    exc_info=True)
        return None, True
    if win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS:
        trava.Close()
        return None, False
    return trava, True


class Motor:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.papel: servidor.Servidor | cliente.Cliente | None = None
        self.thread: threading.Thread | None = None
        self.parada = threading.Event()
        self.trava = None
        self.ao_mudar = lambda: None
        self.ao_trocar = lambda de, para: None

    # -- consulta -----------------------------------------------------------

    def ativo(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def resumo(self) -> str:
        if not self.ativo() or self.papel is None:
            return "parado"
        s = self.papel.situacao()
        if s.get("erro"):  # o que impede de conectar importa mais que o resto
            return f"{s['papel']} '{s['eu']}' -- {s['erro']}"
        faltando = [n for n in s["esperados"] if n not in s["conectados"]]
        partes = [f"{s['papel']} '{s['eu']}'"]
        if s["conectados"]:
            partes.append("conectado: " + ", ".join(s["conectados"]))
        if faltando:
            partes.append("aguardando: " + ", ".join(faltando))
        if s["papel"] == "servidor":
            partes.append("cursor em " + (s["cursor_em"] or "?"))
            if s.get("comandante") and s["comandante"] != s["eu"]:
                partes.append(f"comandado por {s['comandante']}")
        elif s.get("comandando"):
            partes.append("comandando " + (s["cursor_em"] or "o outro PC"))
        elif s["cursor_em"]:
            partes.append("cursor aqui")
        return " | ".join(partes)

    # -- controle -----------------------------------------------------------

    def iniciar(self, cfg: dict) -> None:
        if self.ativo():
            return
        problemas = lay.Layout.de_config(cfg).validar()
        if problemas:
            raise ValueError("; ".join(problemas))
        self.trava, consegui = _tomar_a_trava()
        if not consegui:
            raise JaRodando(
                "o programa ja' esta' rodando nesta maquina -- pelo inicio "
                "automatico ou por outra janela. Dois motores disputam os "
                "mesmos hooks de teclado e a mesma porta, e nenhum funciona")
        self.cfg = cfg
        self.parada = threading.Event()

        layout = lay.Layout.de_config(cfg)
        pc_servidor = layout.servidor()
        if pc_servidor is not None and pc_servidor.nome == cfg["este_pc"]:
            servidor_novo = servidor.Servidor(cfg, self.parada)
            servidor_novo.controle.ao_trocar = self.ao_trocar
            self.papel = servidor_novo
        else:
            cliente_novo = cliente.Cliente(cfg, self.parada)
            cliente_novo.ao_trocar = self.ao_trocar
            self.papel = cliente_novo
        self.papel.ao_mudar = self.ao_mudar

        self.thread = threading.Thread(target=self._rodar, name="motor",
                                       daemon=True)
        self.thread.start()
        self.ao_mudar()

    def _rodar(self) -> None:
        try:
            self.papel.executar()
        except Exception:
            log.error("o motor parou com erro", exc_info=True)
        finally:
            self.ao_mudar()

    def parar(self) -> None:
        self.parada.set()
        if self.thread is not None:
            self.thread.join(timeout=3)
        self.thread = None
        if self.trava is not None:
            self.trava.Close()  # solta a vez para a proxima instancia
            self.trava = None
        self.ao_mudar()
