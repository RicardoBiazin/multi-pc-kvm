"""Papel cliente: recebe o input do servidor e injeta como se fosse local.

O cliente mantem a *posicao virtual* do cursor. Ela avanca com os deltas que
chegam pela rede; quando passa de uma das quatro arestas, ele avisa o servidor
por qual lado saiu e para de injetar. Quem decide para onde o cursor vai e' o
servidor, que e' quem tem o layout -- o cliente nao precisa saber quem sao os
vizinhos dele.
"""

from __future__ import annotations

import logging
import threading
import time

import clipboard_win
import entrada_win as ew
import layout as lay
import protocolo

log = logging.getLogger("cliente")

MARGEM = 4  # px dentro da tela onde o cursor aparece ao chegar
# Logo apos entrar, o cursor esta' a MARGEM px da borda e qualquer tremor de mao
# o devolveria. A trava cai por distancia (o normal: quem atravessa continua o
# movimento para dentro) OU por tempo (para quem atravessa e para na hora).
# Sem o criterio de tempo, atravessar e querer voltar em seguida ficaria preso.
FOLGA_DE_ENTRADA = 40  # px
TRAVA_DE_ENTRADA = 0.6  # s
ESPERA_RECONEXAO = 3.0
ESPERA_APOS_RECUSA = 15.0  # o servidor recusou por configuracao: insistir nao adianta


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
        self.remoto = False
        self.conectado = False
        # True entre mandar 'sair' e o servidor responder: nao adianta injetar
        # nem reavisar enquanto ele nao decide para onde o cursor vai.
        self.aguardando = False
        self.aresta_de_entrada = ""
        self.pode_voltar = True
        self.entrou_em = 0.0
        self.vx = 0.0
        self.vy = 0.0
        self.ao_mudar = lambda: None
        self.ao_trocar = lambda de, para: None
        self.ultimo_erro = ""
        self.config_mudou = False  # a interface redesenha quando ve isto
        self._avisos: dict[str, float] = {}

    def situacao(self) -> dict:
        servidor = self.layout.servidor()
        return {"papel": "cliente", "eu": self.eu,
                "conectados": [servidor.nome] if (servidor and self.conectado) else [],
                "cursor_em": self.eu if self.remoto else "",
                "erro": "" if self.conectado else self.ultimo_erro,
                "esperados": [servidor.nome] if servidor else []}

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

        while not self.parar.is_set():
            # Relido a cada volta: o layout pode ter sido trocado pelo servidor.
            servidor = self.layout.servidor()
            if servidor is None:
                log.error("nenhum PC marcado como servidor no layout")
                return
            alvo = (servidor.ip, self.cfg["porta"])
            try:
                conn, _ = protocolo.conectar(
                    *alvo, self.cfg["chave"],
                    {"papel": "cliente", "nome": self.eu,
                     "tela": [self.largura, self.altura],
                     "monitores": self.monitores},
                )
            except Exception as exc:
                self.ultimo_erro = str(exc)
                self._avisar_uma_vez(
                    f"conectar:{exc}",
                    "sem servidor em %s:%d (%s); tentando a cada %.0fs",
                    *alvo, exc, ESPERA_RECONEXAO)
                self.ao_mudar()
                if self.parar.wait(ESPERA_RECONEXAO):
                    return
                continue
            log.info("conectado a '%s' (%s:%d)", servidor.nome, *alvo)
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
                     f"mesma coisa que o 2pc_1Kit: os dois disputam os hooks de "
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
            self._encerrar_remoto()
            fim.set()
            conn.fechar()
        return espera

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
        if tipo == "entrar":
            self.aguardando = False
            self.vx, self.vy = lay.ponto_de_entrada(
                msg["de"], msg["rel"], self.x0, self.y0,
                self.largura, self.altura, MARGEM, self.monitores)
            # O cursor entra a poucos px da borda: qualquer tremida na mao o
            # jogaria de volta na hora. So' liberamos a saida por esta aresta
            # depois que ele se afastar dela.
            self.aresta_de_entrada = msg["de"]
            self.pode_voltar = False
            self.entrou_em = time.monotonic()
            self.injetor.mover_para(self.vx, self.vy)
            if not self.remoto:
                self.remoto = True
                log.info("controle recebido (entrou pela %s, cursor em %d,%d)",
                         msg["de"], self.vx, self.vy)
                self.ao_trocar("", self.eu)
                self.ao_mudar()
            return
        if tipo == "soltar":
            self._encerrar_remoto()
            return

        if not self.remoto or self.aguardando:
            return  # evento atrasado, ou a espera da decisao do servidor

        if tipo == "mv":
            self.vx += msg["dx"]
            self.vy += msg["dy"]
            direcao = lay.direcao_de_saida(self.vx, self.vy, self.x0, self.y0,
                                           self.largura, self.altura)
            if direcao is not None and not self._pode_sair_por(direcao):
                direcao = None  # ainda colado na aresta por onde acabou de entrar
            if direcao is not None:
                rel = lay.relativo_na_aresta(self.vx, self.vy, direcao, self.x0,
                                             self.y0, self.largura, self.altura)
                # Nao encerramos aqui: quem decide se o cursor vai embora ou
                # fica encostado e' o servidor, que responde com 'entrar' ou
                # 'soltar'. Ate' la', so' paramos de injetar movimento.
                self.vx = min(self.x0 + self.largura - 1, max(self.x0, self.vx))
                self.vy = min(self.y0 + self.altura - 1, max(self.y0, self.vy))
                self.aguardando = True
                conn.enviar({"t": "sair", "dir": direcao, "rel": rel})
                log.info("cursor saiu pela %s; aguardando o servidor", direcao)
                return
            self.vx = min(self.x0 + self.largura - 1, max(self.x0, self.vx))
            self.vy = min(self.y0 + self.altura - 1, max(self.y0, self.vy))
            # Buraco entre monitores: o Windows prenderia o cursor na tela mais
            # proxima, e a nossa posicao virtual ficaria adiantada. Puxamos nos
            # dois para eles nao divergirem.
            self.vx, self.vy = lay.ponto_visivel(self.vx, self.vy, self.monitores)
            self._talvez_liberar()
            self.injetor.mover_para(self.vx, self.vy)
        elif tipo == "btn":
            self.injetor.botao(msg["b"], msg["down"])
        elif tipo == "whl":
            self.injetor.roda(msg["d"], msg["h"])
        elif tipo == "key":
            self.injetor.tecla(msg["vk"], msg["sc"], msg["ext"], msg["down"])

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

    def _encerrar_remoto(self) -> None:
        self.aguardando = False
        self.pode_voltar = True
        if not self.remoto:
            return
        self.remoto = False
        self.injetor.soltar_modificadores()
        log.info("controle devolvido")
        servidor = self.layout.servidor()
        self.ao_trocar(self.eu, servidor.nome if servidor else "")
        self.ao_mudar()
