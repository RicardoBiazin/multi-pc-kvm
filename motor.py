"""Liga e desliga o papel certo (servidor ou cliente) numa thread de fundo.

A interface e o modo console falam so' com esta classe -- e' ela que sabe qual
papel esta' maquina tem, segundo o layout.

E' tambem onde mora a trava de instancia unica: dois motores na mesma maquina
travam o teclado, e o jeito de descobrir isso e' penoso (ver `_tomar_a_trava`).
"""

from __future__ import annotations

import logging
import sys
import threading

import win32api
import win32event
import winerror

import atualizacao
import cliente
import configuracao as conf
import contador
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


def outro_motor_no_ar(nome: str = TRAVA) -> bool:
    """Algum OUTRO processo desta maquina segura a trava do motor agora?

    So' abre o mutex para consultar, sem toma'-lo. Nao depende do COM do
    Agendador, que numa janela lancada como SYSTEM pode nao responder.
    """
    try:
        trava = win32event.OpenMutex(win32event.SYNCHRONIZE, False, nome)
    except Exception:
        return False
    trava.Close()
    return True


class Motor:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.papel: servidor.Servidor | cliente.Cliente | None = None
        self.thread: threading.Thread | None = None
        self.parada = threading.Event()
        self.trava = None
        # So' existe quando esta maquina e' o servidor; a janela de um cliente
        # nao tem numeros proprios para mostrar.
        self.contador = None
        self.ao_mudar = lambda: None
        self.ao_trocar = lambda de, para: None
        # Balao de aviso na bandeja ("Troca pela borda DESLIGADA"): quem tem o
        # icone liga aqui.
        self.aviso = lambda texto: None
        # Atualizacao pelos Releases do GitHub (atualizacao.py). A consulta periodica
        # so' liga com o motor no ar e no .exe; verificar e atualizar a' mao
        # funcionam sempre. pedir_saida fecha o processo depois que o ajudante
        # foi disparado (a janela se encerra; o agente morre pela tarefa).
        self.pedir_saida = lambda: None
        self.atualizador = atualizacao.Atualizador(
            modo=self._modo_de_atualizacao, avisar=lambda t: self.aviso(t),
            ocioso=self._ocioso, ao_atualizar=lambda: self.pedir_saida(),
            via_tarefa=self._via_tarefa)

    # -- atualizacao --------------------------------------------------------

    def _modo_de_atualizacao(self) -> str:
        politica = getattr(self.papel, "politica", None)
        if politica is not None:
            return politica.atualizacao
        return (self.cfg.get("opcoes") or {}).get("atualizacao", "avisar")

    def _ocioso(self) -> bool:
        """Momento calmo para reiniciar: ninguem esta' comandando outro PC."""
        papel = self.papel
        if papel is None or not self.ativo():
            return False
        controle = getattr(papel, "controle", None)
        if controle is not None:  # servidor
            return not controle.remoto and not controle.multi and controle.comandante == controle.eu
        return not getattr(papel, "remoto", False) and not papel.comando.comandando

    @staticmethod
    def _via_tarefa() -> bool:
        """O programa roda sob a tarefa do inicio automatico? (decide como o
        ajudante da atualizacao sobe: ver atualizacao.lancar_ajudante)."""
        # O supervisor e o agente SAO a tarefa: sem consultar o Agendador, que
        # por COM nao funciona numa thread sem CoInitialize e diria "nao".
        if "--agente" in sys.argv or "--servico" in sys.argv:
            return True
        try:
            import pythoncom
            import servico
            pythoncom.CoInitialize()
            try:
                return servico.instalado()
            finally:
                pythoncom.CoUninitialize()
        except Exception:
            return False

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

    # -- audio --------------------------------------------------------------

    def ouvir(self, fontes) -> None:
        """De quais PCs tocar o som aqui. Vale ja' se o motor estiver no ar; se
        nao estiver, vale quando ele subir (o papel le' `cfg["ouvir"]`)."""
        fontes = list(fontes)
        self.cfg["ouvir"] = fontes
        if self.papel is not None and self.ativo():
            self.papel.ouvir(fontes)

    def opcoes(self, novas: dict) -> None:
        """Muda opcoes da sessao (politica.py). Vale ja' se o motor esta' no ar:
        o servidor as repassa aos clientes."""
        atuais = dict(self.cfg.get("opcoes") or {})
        atuais.update(novas)
        self.cfg["opcoes"] = atuais
        if self.papel is not None and self.ativo():
            self.papel.aplicar_opcoes(novas)

    def atualizar_conexoes(self) -> None:
        """Servidor: manda os clientes reconectarem. Cliente: reconecta este."""
        if self.papel is None or not self.ativo():
            return
        if hasattr(self.papel, "atualizar_conexoes"):
            self.papel.atualizar_conexoes()
        else:
            self.papel.reconectar()

    def volume(self, valor: float) -> None:
        self.cfg["volume_audio"] = int(round(valor * 100))
        if self.papel is not None:
            self.papel.audio.volume(valor)

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
            # A contagem de uso mora no servidor: e' o unico ponto que sabe o
            # destino de cada tecla e de cada clique (ver contador.py).
            self.contador = contador.Contador(
                conf.pasta_de_saida() / "uso.json",
                ativo=bool(cfg.get("contar_uso", True)))
            servidor_novo.controle.contar = self.contador.registrar
            self.papel = servidor_novo
        else:
            cliente_novo = cliente.Cliente(cfg, self.parada)
            cliente_novo.ao_trocar = self.ao_trocar
            self.papel = cliente_novo
        self.papel.ao_mudar = self.ao_mudar
        self.papel.avisar = lambda texto: self.aviso(texto)
        if getattr(sys, "frozen", False):
            self.atualizador.iniciar()

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

    def verificar_atualizacao(self) -> str:
        """Consulta o GitHub agora. Devolve o texto para mostrar ao usuario."""
        release = self.atualizador.verificar()
        if release is not None:
            return (f"Nova versao v{release.versao} disponivel (esta e' a v"
                    f"{conf.VERSAO}).")
        if self.atualizador.ultimo_erro:
            return self.atualizador.ultimo_erro
        return f"Esta e' a versao mais recente (v{conf.VERSAO})."

    def atualizar_agora(self) -> None:
        """Instala a versao achada por erificar_atualizacao. Levanta
        atualizacao.ErroDeAtualizacao se nao der; se der, o processo vai fechar."""
        self.atualizador.atualizar_agora()

    def parar(self) -> None:
        self.atualizador.parar()
        self.parada.set()
        if self.thread is not None:
            self.thread.join(timeout=3)
        self.thread = None
        if self.contador is not None:
            self.contador.gravar()  # o que ainda nao foi para o disco
        if self.trava is not None:
            self.trava.Close()  # solta a vez para a proxima instancia
            self.trava = None
        self.ao_mudar()
