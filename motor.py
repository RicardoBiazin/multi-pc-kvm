"""Liga e desliga o papel certo (servidor ou cliente) numa thread de fundo.

A interface e o modo console falam so' com esta classe -- e' ela que sabe qual
papel esta' maquina tem, segundo o layout.
"""

from __future__ import annotations

import logging
import threading

import cliente
import layout as lay
import servidor

log = logging.getLogger("motor")


class Motor:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.papel: servidor.Servidor | cliente.Cliente | None = None
        self.thread: threading.Thread | None = None
        self.parada = threading.Event()
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
        self.ao_mudar()
