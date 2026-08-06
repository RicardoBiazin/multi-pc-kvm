"""Janela de configuracao e controle do 2pc_1Kit (tkinter).

Quatro partes: o papel desta maquina, a lista de PCs (nome e IP), o mapa onde se
arrasta cada PC para a posicao do monitor dele, e os PCs achados na rede.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

import aviso
import configuracao as conf
import descoberta
import diagnostico
import layout as lay
import motor
import redes
import relatorio

COLUNAS, LINHAS = 6, 4  # tamanho do mapa de posicoes
CELULA_L, CELULA_A = 118, 74
MARGEM_MAPA = 10

COR_FUNDO = "#f4f5f7"
COR_CELULA = "#e2e5ea"
COR_PC = "#4a7fd4"
COR_PC_SERVIDOR = "#2f6b3f"
COR_PC_EU = "#c9752b"
COR_TEXTO = "#ffffff"


class ManipuladorDeLog(logging.Handler):
    """Empilha as linhas de log; a janela as consome no proprio laco do tkinter."""

    def __init__(self, fila: queue.Queue):
        super().__init__()
        self.fila = fila

    def emit(self, registro: logging.LogRecord) -> None:
        try:
            self.fila.put_nowait(self.format(registro))
        except queue.Full:
            pass


class Janela(tk.Tk):
    def __init__(self, cfg: dict, farol: descoberta.Farol | None = None):
        super().__init__()
        self.cfg = cfg
        self.farol = farol
        self.motor = motor.Motor(cfg)
        self.motor.ao_mudar = self._agendar_estado
        self.motor.ao_trocar = self._agendar_troca
        self.tarja = aviso.Tarja(self)
        self.fila_log: queue.Queue[str] = queue.Queue(maxsize=2000)
        self.selecionado: str | None = None
        self._arrastando: str | None = None
        self._icone_bandeja = None
        self._achados: dict[str, dict] = {}
        self._aviso_ate = 0.0

        self.title("2pc_1Kit -- um teclado e um mouse para varios PCs")
        self.configure(bg=COR_FUNDO)
        self.resizable(False, False)
        self._montar()
        self._carregar_na_tela()
        self.protocol("WM_DELETE_WINDOW", self._ao_fechar)

        self._ligar_log()
        self.after(120, self._consumir_log)
        self._laco_estado()
        self._laco_rede()
        self._verificar_rede()

        if cfg.get("iniciar_ao_abrir") and not lay.Layout.de_config(cfg).validar():
            self.after(400, self._iniciar)

    # -- construcao da janela ----------------------------------------------

    def _montar(self) -> None:
        estilo = ttk.Style(self)
        try:
            estilo.theme_use("vista")
        except tk.TclError:
            pass

        # -- identidade e papel desta maquina
        topo = ttk.LabelFrame(self, text=" Esta maquina ")
        topo.grid(row=0, column=0, columnspan=2, sticky="ew", padx=10, pady=(10, 4))
        ttk.Label(topo, text="Este PC e':").grid(row=0, column=0, padx=(8, 4), pady=8)
        self.var_eu = tk.StringVar()
        self.combo_eu = ttk.Combobox(topo, textvariable=self.var_eu, width=20,
                                     state="readonly")
        self.combo_eu.grid(row=0, column=1, pady=8)
        self.combo_eu.bind("<<ComboboxSelected>>", self._ao_trocar_eu)

        self.var_papel = tk.StringVar(value="servidor")
        ttk.Radiobutton(topo, text="servidor -- tem o teclado e o mouse",
                        value="servidor", variable=self.var_papel,
                        command=self._definir_papel).grid(row=0, column=2, padx=(18, 6))
        ttk.Radiobutton(topo, text="cliente -- recebe o teclado e o mouse",
                        value="cliente", variable=self.var_papel,
                        command=self._definir_papel).grid(row=0, column=3, padx=6)

        ttk.Label(topo, text="Porta:").grid(row=1, column=0, padx=(8, 4), pady=(0, 8))
        self.var_porta = tk.StringVar()
        ttk.Entry(topo, textvariable=self.var_porta, width=8).grid(
            row=1, column=1, sticky="w", pady=(0, 8))
        chave = ttk.Frame(topo)
        chave.grid(row=1, column=2, columnspan=2, sticky="w", pady=(0, 8))
        ttk.Label(chave, text="Chave (igual em todos):").grid(row=0, column=0, padx=(8, 4))
        self.var_chave = tk.StringVar()
        ttk.Entry(chave, textvariable=self.var_chave, width=32).grid(row=0, column=1)
        ttk.Button(chave, text="Gerar", width=7,
                   command=self._gerar_chave).grid(row=0, column=2, padx=4)
        ttk.Button(chave, text="Copiar", width=7,
                   command=self._copiar_chave).grid(row=0, column=3)

        # -- lista de PCs
        esquerda = ttk.LabelFrame(self, text=" PCs ")
        esquerda.grid(row=1, column=0, sticky="new", padx=(10, 5), pady=4)
        self.arvore = ttk.Treeview(esquerda, columns=("ip", "papel"),
                                   show="tree headings", height=6, selectmode="browse")
        self.arvore.heading("#0", text="Nome")
        self.arvore.heading("ip", text="IP")
        self.arvore.heading("papel", text="Papel")
        self.arvore.column("#0", width=150)
        self.arvore.column("ip", width=120)
        self.arvore.column("papel", width=80, anchor="center")
        self.arvore.grid(row=0, column=0, columnspan=3, padx=8, pady=(8, 4))
        self.arvore.bind("<<TreeviewSelect>>", self._ao_selecionar)

        ttk.Button(esquerda, text="Adicionar",
                   command=self._adicionar).grid(row=1, column=0, padx=(8, 2), pady=4)
        ttk.Button(esquerda, text="Remover",
                   command=self._remover).grid(row=1, column=1, padx=2, pady=4)
        self.botao_servidor = ttk.Button(esquerda, text="Este e' o servidor",
                                         command=self._marcar_servidor)
        self.botao_servidor.grid(row=1, column=2, padx=(2, 8), pady=4)

        edicao = ttk.Frame(esquerda)
        edicao.grid(row=2, column=0, columnspan=3, sticky="ew", padx=8, pady=(6, 10))
        ttk.Label(edicao, text="Nome:").grid(row=0, column=0, sticky="w")
        self.var_nome = tk.StringVar()
        e_nome = ttk.Entry(edicao, textvariable=self.var_nome, width=22)
        e_nome.grid(row=0, column=1, padx=4, pady=2)
        ttk.Label(edicao, text="IP:").grid(row=0, column=2, sticky="w", padx=(12, 0))
        self.var_ip = tk.StringVar()
        # Combobox e nao Entry: com Wi-Fi e cabo na mesma maquina, escolher o IP
        # certo (e saber qual e' qual) e' metade do problema de conectar.
        self.combo_ip = ttk.Combobox(edicao, textvariable=self.var_ip, width=26)
        self.combo_ip.grid(row=0, column=3, padx=4, pady=2)
        self.combo_ip.bind("<<ComboboxSelected>>", lambda _e: self._aplicar_edicao())
        for campo in (e_nome, self.combo_ip):
            campo.bind("<FocusOut>", lambda _e: self._aplicar_edicao())
            campo.bind("<Return>", lambda _e: self._aplicar_edicao())
        ttk.Button(edicao, text="Testar conexao",
                   command=self._testar_conexao).grid(row=0, column=4, padx=(12, 0))

        # -- PCs achados na rede
        rede = ttk.LabelFrame(self, text=" Encontrados na rede ")
        rede.grid(row=2, column=0, sticky="new", padx=(10, 5), pady=4)
        self.arvore_rede = ttk.Treeview(rede, columns=("ip", "papel", "chave"),
                                        show="tree headings", height=4,
                                        selectmode="browse")
        self.arvore_rede.heading("#0", text="Nome")
        self.arvore_rede.heading("ip", text="IP")
        self.arvore_rede.heading("papel", text="Papel")
        self.arvore_rede.heading("chave", text="Chave")
        self.arvore_rede.column("#0", width=150)
        self.arvore_rede.column("ip", width=120)
        self.arvore_rede.column("papel", width=70, anchor="center")
        self.arvore_rede.column("chave", width=80, anchor="center")
        self.arvore_rede.grid(row=0, column=0, columnspan=2, padx=8, pady=(8, 4))
        self.arvore_rede.bind("<Double-1>", lambda _e: self._adicionar_encontrado())
        ttk.Button(rede, text="Adicionar ao layout",
                   command=self._adicionar_encontrado).grid(row=1, column=0,
                                                            padx=(8, 4), pady=(0, 8))
        self.var_rede = tk.StringVar(value="procurando...")
        ttk.Label(rede, textvariable=self.var_rede).grid(row=1, column=1, sticky="w",
                                                         pady=(0, 8))

        # -- mapa de posicoes
        direita = ttk.LabelFrame(self, text=" Posicao dos monitores "
                                            "(arraste cada PC) ")
        direita.grid(row=1, column=1, rowspan=2, sticky="new", padx=(5, 10), pady=4)
        self.mapa = tk.Canvas(direita, bg=COR_FUNDO, highlightthickness=0,
                              width=COLUNAS * CELULA_L + 2 * MARGEM_MAPA,
                              height=LINHAS * CELULA_A + 2 * MARGEM_MAPA)
        self.mapa.grid(row=0, column=0, padx=8, pady=8)
        self.mapa.bind("<Button-1>", self._mapa_clique)
        self.mapa.bind("<B1-Motion>", self._mapa_arrasto)
        self.mapa.bind("<ButtonRelease-1>", self._mapa_solta)

        # -- opcoes e acoes
        baixo = ttk.Frame(self)
        baixo.grid(row=3, column=0, columnspan=2, sticky="ew", padx=10, pady=(4, 0))
        self.var_auto = tk.BooleanVar()
        ttk.Checkbutton(baixo, text="Conectar ao abrir", variable=self.var_auto,
                        command=self._salvar_silencioso).grid(row=0, column=0)
        self.var_windows = tk.BooleanVar(value=conf.inicio_automatico())
        ttk.Checkbutton(baixo, text="Iniciar com o Windows",
                        variable=self.var_windows,
                        command=self._alternar_windows).grid(row=0, column=1, padx=12)
        self.var_bandeja = tk.BooleanVar()
        ttk.Checkbutton(baixo, text="Fechar para a bandeja",
                        variable=self.var_bandeja,
                        command=self._salvar_silencioso).grid(row=0, column=2)
        self.var_avisar = tk.BooleanVar()
        ttk.Checkbutton(baixo, text="Avisar ao trocar de PC",
                        variable=self.var_avisar,
                        command=self._salvar_silencioso).grid(row=0, column=3,
                                                              padx=(12, 0))

        ttk.Button(baixo, text="Liberar no Firewall",
                   command=self._liberar_firewall).grid(row=0, column=4, padx=(20, 4))
        ttk.Button(baixo, text="Salvar", width=10,
                   command=self._salvar).grid(row=0, column=5, padx=(4, 4))
        self.botao_iniciar = ttk.Button(baixo, text="Iniciar", width=10,
                                        command=self._iniciar)
        self.botao_iniciar.grid(row=0, column=6, padx=4)
        self.botao_parar = ttk.Button(baixo, text="Parar", width=10,
                                      command=self._parar, state="disabled")
        self.botao_parar.grid(row=0, column=7, padx=4)

        self.var_aviso = tk.StringVar()
        self.rotulo_aviso = tk.Label(self, textvariable=self.var_aviso, anchor="w",
                                     bg="#fde7c8", fg="#7a4a10", justify="left",
                                     font=("Segoe UI", 8), padx=8, pady=4)
        # so' aparece quando ha' o que avisar -- ver _verificar_rede()

        # A faixa de aviso fica na linha 4 e a barra de estado na 5: as duas na
        # mesma linha se sobrepunham e o aviso ficava ilegivel.
        self.var_estado = tk.StringVar(value="parado")
        ttk.Label(self, textvariable=self.var_estado, anchor="w").grid(
            row=5, column=0, columnspan=2, sticky="ew", padx=14, pady=(6, 0))

        quadro_log = ttk.LabelFrame(self, text=" Registro ")
        quadro_log.grid(row=6, column=0, columnspan=2, sticky="nsew",
                        padx=10, pady=(4, 10))
        self.texto_log = tk.Text(quadro_log, height=8, width=125, wrap="none",
                                 font=("Consolas", 8), state="disabled",
                                 bg="#1e1f22", fg="#d6d8dc", relief="flat")
        self.texto_log.grid(row=0, column=0, padx=6, pady=6, sticky="nsew")
        barra = ttk.Scrollbar(quadro_log, command=self.texto_log.yview)
        barra.grid(row=0, column=1, sticky="ns", pady=6)
        self.texto_log.configure(yscrollcommand=barra.set)
        # Um Text desabilitado deixa selecionar, mas o Ctrl+C nao chega ate' ele.
        self.texto_log.bind("<Control-c>", self._copiar_selecao)
        self.texto_log.bind("<Control-C>", self._copiar_selecao)
        self.texto_log.bind("<Control-a>", self._selecionar_tudo)
        self.texto_log.bind("<Control-A>", self._selecionar_tudo)

        acoes_log = ttk.Frame(quadro_log)
        acoes_log.grid(row=1, column=0, columnspan=2, sticky="w", padx=6,
                       pady=(0, 8))
        ttk.Button(acoes_log, text="Copiar registro",
                   command=self._copiar_registro).grid(row=0, column=0)
        ttk.Button(acoes_log, text="Gerar relatorio para compartilhar",
                   command=self._gerar_relatorio).grid(row=0, column=1, padx=6)
        ttk.Button(acoes_log, text="Abrir pasta",
                   command=self._abrir_pasta).grid(row=0, column=2)
        ttk.Button(acoes_log, text="Limpar",
                   command=self._limpar_registro).grid(row=0, column=3, padx=6)

    # -- config <-> tela ----------------------------------------------------

    def _pcs(self) -> list[dict]:
        return self.cfg["pcs"]

    def _pc(self, nome: str) -> dict | None:
        return next((p for p in self._pcs() if p["nome"] == nome), None)

    def _eu(self) -> dict | None:
        return self._pc(self.cfg.get("este_pc", ""))

    def _carregar_na_tela(self) -> None:
        self.var_porta.set(str(self.cfg.get("porta", conf.PORTA_PADRAO)))
        self.var_chave.set(self.cfg.get("chave", ""))
        self.var_auto.set(bool(self.cfg.get("iniciar_ao_abrir", True)))
        self.var_bandeja.set(bool(self.cfg.get("usar_bandeja", True)))
        self.var_avisar.set(bool(self.cfg.get("avisar_troca", True)))
        self._redesenhar()

    def _redesenhar(self) -> None:
        nomes = [p["nome"] for p in self._pcs()]
        self.combo_eu["values"] = nomes
        if self.cfg.get("este_pc") not in nomes and nomes:
            self.cfg["este_pc"] = nomes[0]
        self.var_eu.set(self.cfg.get("este_pc", ""))
        eu = self._eu()
        self.var_papel.set("servidor" if (eu and eu.get("servidor")) else "cliente")

        self.arvore.delete(*self.arvore.get_children())
        for pc in self._pcs():
            papel = "servidor" if pc.get("servidor") else "cliente"
            self.arvore.insert("", "end", iid=pc["nome"], text=pc["nome"],
                               values=(pc.get("ip", ""), papel))
        if self.selecionado and self._pc(self.selecionado):
            self.arvore.selection_set(self.selecionado)
        self._atualizar_botao_servidor()
        self._desenhar_mapa()

    def _desenhar_mapa(self) -> None:
        self.mapa.delete("all")
        for coluna in range(COLUNAS):
            for linha in range(LINHAS):
                x = MARGEM_MAPA + coluna * CELULA_L
                y = MARGEM_MAPA + linha * CELULA_A
                self.mapa.create_rectangle(x + 3, y + 3, x + CELULA_L - 3,
                                           y + CELULA_A - 3, fill=COR_CELULA,
                                           outline="#cfd3da", dash=(2, 3))
        for pc in self._pcs():
            self._desenhar_pc(pc)

    def _desenhar_pc(self, pc: dict) -> None:
        x = MARGEM_MAPA + pc["coluna"] * CELULA_L
        y = MARGEM_MAPA + pc["linha"] * CELULA_A
        if pc.get("servidor"):
            cor = COR_PC_SERVIDOR
        elif pc["nome"] == self.cfg.get("este_pc"):
            cor = COR_PC_EU
        else:
            cor = COR_PC
        largura = 3 if pc["nome"] == self.selecionado else 1
        etiqueta = f"pc:{pc['nome']}"
        self.mapa.create_rectangle(x + 6, y + 6, x + CELULA_L - 6, y + CELULA_A - 6,
                                   fill=cor, outline="#20242b", width=largura,
                                   tags=etiqueta)
        legenda = pc["nome"]
        if pc.get("servidor"):
            legenda += "\n(teclado)"
        if pc["nome"] == self.cfg.get("este_pc"):
            legenda += "\n(este PC)"
        self.mapa.create_text(x + CELULA_L / 2, y + CELULA_A / 2, text=legenda,
                              fill=COR_TEXTO, font=("Segoe UI", 8, "bold"),
                              justify="center", tags=etiqueta)

    # -- papel desta maquina -----------------------------------------------

    def _ao_trocar_eu(self, _evento=None) -> None:
        self.cfg["este_pc"] = self.var_eu.get().strip()
        self._redesenhar()

    def _definir_papel(self) -> None:
        eu = self._eu()
        if eu is None:
            return
        if self.var_papel.get() == "servidor":
            for pc in self._pcs():  # so' pode haver um servidor
                pc["servidor"] = pc["nome"] == eu["nome"]
        else:
            eu["servidor"] = False
            if not any(p.get("servidor") for p in self._pcs()):
                self._avisar("agora nenhum PC e' o servidor: selecione na lista "
                             "qual e' e clique em 'Este e' o servidor'", 10)
        self._redesenhar()

    def _marcar_servidor(self) -> None:
        """Marca o PC selecionado na lista -- inclusive um que nao seja este."""
        alvo = self._pc(self.selecionado) if self.selecionado else None
        if alvo is None:
            return
        for pc in self._pcs():
            pc["servidor"] = pc["nome"] == alvo["nome"]
        self._redesenhar()

    # -- edicao da lista ----------------------------------------------------

    def _ao_selecionar(self, _evento=None) -> None:
        # Nao chamar _redesenhar aqui: ele repovoa a arvore e reemite
        # <<TreeviewSelect>>, o que voltaria para ca' em laco infinito.
        selecao = self.arvore.selection()
        self.selecionado = selecao[0] if selecao else None
        pc = self._pc(self.selecionado) if self.selecionado else None
        self.var_nome.set(pc["nome"] if pc else "")
        self.var_ip.set(pc.get("ip", "") if pc else "")
        self.combo_ip["values"] = self._ips_possiveis(pc)
        self._atualizar_botao_servidor()
        self._desenhar_mapa()

    def _atualizar_botao_servidor(self) -> None:
        pc = self._pc(self.selecionado) if self.selecionado else None
        self.botao_servidor.configure(
            state="disabled" if (pc is None or pc.get("servidor")) else "normal")

    def _celula_livre(self, perto_de: dict | None = None) -> tuple[int, int]:
        """De preferencia ao lado de `perto_de`, para ja' nascer fazendo fronteira."""
        ocupadas = {(p["coluna"], p["linha"]) for p in self._pcs()}
        if perto_de is not None:
            linha = perto_de["linha"]
            for coluna in (perto_de["coluna"] + 1, perto_de["coluna"] - 1):
                if 0 <= coluna < COLUNAS and (coluna, linha) not in ocupadas:
                    return coluna, linha
        for linha in range(LINHAS):
            for coluna in range(COLUNAS):
                if (coluna, linha) not in ocupadas:
                    return coluna, linha
        return 0, 0

    def _novo_pc(self, nome: str, ip: str) -> None:
        coluna, linha = self._celula_livre(self._eu())
        self._pcs().append({"nome": nome, "ip": ip, "coluna": coluna,
                            "linha": linha, "servidor": False})
        self.selecionado = nome
        self._redesenhar()
        self._ao_selecionar()

    def _adicionar(self) -> None:
        n = 2
        while any(p["nome"] == f"PC-{n}" for p in self._pcs()):
            n += 1
        self._novo_pc(f"PC-{n}", "")

    def _remover(self) -> None:
        if not self.selecionado:
            return
        if len(self._pcs()) <= 1:
            messagebox.showinfo("2pc_1Kit", "Tem de sobrar pelo menos um PC.")
            return
        self.cfg["pcs"] = [p for p in self._pcs() if p["nome"] != self.selecionado]
        self.selecionado = None
        self._redesenhar()
        self._ao_selecionar()

    def _aplicar_edicao(self) -> None:
        pc = self._pc(self.selecionado) if self.selecionado else None
        if pc is None:
            return
        novo = self.var_nome.get().strip()
        if novo and novo != pc["nome"]:
            if any(p["nome"] == novo for p in self._pcs()):
                messagebox.showwarning("2pc_1Kit", f"Ja' existe um PC '{novo}'.")
                self.var_nome.set(pc["nome"])
            else:
                if self.cfg.get("este_pc") == pc["nome"]:
                    self.cfg["este_pc"] = novo
                pc["nome"] = novo
                self.selecionado = novo
        # O combo mostra "192.168.1.10 (Wi-Fi)"; guardamos so' o IP.
        pc["ip"] = self.var_ip.get().split(" (")[0].strip()
        self._redesenhar()

    # -- rede: IPs desta maquina, teste e Firewall --------------------------

    def _ips_possiveis(self, pc: dict | None) -> list[str]:
        """Sugestoes para o campo IP do PC selecionado."""
        if pc is None:
            return []
        if pc["nome"] == self.cfg.get("este_pc"):
            return [p.rotulo() for p in redes.listar()]  # os IPs desta maquina
        achado = self._achados.get(pc["nome"])
        if achado:
            # o IP de onde o anuncio chegou vem primeiro: e' o que alcanca
            outros = [i for i in achado.get("ips", []) if i != achado["ip"]]
            return [achado["ip"]] + outros
        return []

    def _verificar_rede(self) -> None:
        """Avisa sobre rede Publica, que e' o que costuma barrar tudo.

        A consulta chama o PowerShell e leva ~1s, entao roda fora da thread da
        interface para nao congelar a janela.
        """
        def consultar() -> None:
            try:
                publicas = diagnostico.redes_publicas()
            except Exception:
                return
            self._na_interface(lambda: self._mostrar_aviso(publicas))

        threading.Thread(target=consultar, name="perfil-rede", daemon=True).start()

    def _na_interface(self, acao) -> None:
        """Executa `acao` na thread do tkinter, sem estourar se a janela ja' fechou."""
        try:
            self.after(0, acao)
        except (RuntimeError, tk.TclError):
            pass  # janela fechada enquanto a consulta corria

    def _mostrar_aviso(self, publicas: list[str]) -> None:
        if publicas:
            nomes = ", ".join(sorted(set(publicas)))
            self.var_aviso.set(
                f"A rede '{nomes}' esta' marcada como Publica. Nesse modo o "
                f"Windows bloqueia a busca na rede e as conexoes de entrada, "
                f"mesmo com regra de Firewall.\n"
                f"Abra Configuracoes > Rede e Internet, clique na rede e mude "
                f"para 'Rede particular' -- nos dois PCs.")
            self.rotulo_aviso.grid(row=4, column=0, columnspan=2, sticky="ew",
                                   padx=10, pady=(6, 0))
        else:
            self.rotulo_aviso.grid_remove()

    def _testar_conexao(self) -> None:
        pc = self._pc(self.selecionado) if self.selecionado else None
        if pc is None:
            self._avisar("selecione na lista o PC que quer testar")
            return
        if pc["nome"] == self.cfg.get("este_pc"):
            self._avisar("este e' o proprio PC -- selecione o outro para testar")
            return
        try:
            porta = int(self.var_porta.get())
        except ValueError:
            self._avisar("porta invalida")
            return
        ip, nome = pc.get("ip", ""), pc["nome"]
        self._avisar(f"testando {ip}:{porta}...", 30)

        def testar() -> None:  # fora da thread da interface: leva ate' 3s
            ok, mensagem = diagnostico.testar(ip, porta)
            registro = logging.getLogger("diagnostico")
            (registro.info if ok else registro.warning)(
                "teste com '%s': %s", nome, mensagem)
            self._na_interface(
                lambda: self._avisar(("OK -- " if ok else "FALHOU -- ")
                                     + mensagem, 20))

        threading.Thread(target=testar, name="teste-conexao", daemon=True).start()

    def _liberar_firewall(self) -> None:
        try:
            porta = int(self.var_porta.get())
        except ValueError:
            self._avisar("porta invalida")
            return
        if not messagebox.askyesno(
                "2pc_1Kit",
                f"Criar regras no Firewall do Windows liberando a entrada em\n"
                f"TCP {porta} e UDP {descoberta.PORTA}, para as redes particular "
                f"e de dominio?\n\nIsso altera a configuracao do Windows."):
            return
        ok, mensagem = diagnostico.liberar(porta, descoberta.PORTA)
        logging.getLogger("diagnostico").info("firewall: %s", mensagem)
        if ok:
            messagebox.showinfo("2pc_1Kit", mensagem)
        else:
            messagebox.showerror("2pc_1Kit", mensagem)
        self._verificar_rede()

    # -- arrastar no mapa ---------------------------------------------------

    def _pc_em(self, x: int, y: int) -> str | None:
        for item in reversed(self.mapa.find_overlapping(x, y, x, y)):
            for etiqueta in self.mapa.gettags(item):
                if etiqueta.startswith("pc:"):
                    return etiqueta[3:]
        return None

    def _celula_em(self, x: int, y: int) -> tuple[int, int]:
        coluna = int((x - MARGEM_MAPA) // CELULA_L)
        linha = int((y - MARGEM_MAPA) // CELULA_A)
        return (min(COLUNAS - 1, max(0, coluna)), min(LINHAS - 1, max(0, linha)))

    def _mapa_clique(self, evento) -> None:
        nome = self._pc_em(evento.x, evento.y)
        if nome is None:
            return
        self._arrastando = nome
        self.selecionado = nome
        self.arvore.selection_set(nome)
        self._ao_selecionar()

    def _mapa_arrasto(self, evento) -> None:
        if self._arrastando is None:
            return
        self.mapa.delete("fantasma")
        coluna, linha = self._celula_em(evento.x, evento.y)
        x = MARGEM_MAPA + coluna * CELULA_L
        y = MARGEM_MAPA + linha * CELULA_A
        self.mapa.create_rectangle(x + 6, y + 6, x + CELULA_L - 6, y + CELULA_A - 6,
                                   outline="#20242b", width=2, dash=(4, 3),
                                   tags="fantasma")

    def _mapa_solta(self, evento) -> None:
        if self._arrastando is None:
            return
        nome, self._arrastando = self._arrastando, None
        self.mapa.delete("fantasma")
        coluna, linha = self._celula_em(evento.x, evento.y)
        movido = self._pc(nome)
        if movido is None:
            return
        ocupante = next((p for p in self._pcs()
                         if (p["coluna"], p["linha"]) == (coluna, linha)), None)
        if ocupante is not None and ocupante["nome"] != nome:
            # troca de lugar com quem ja' estava na celula
            ocupante["coluna"], ocupante["linha"] = movido["coluna"], movido["linha"]
        movido["coluna"], movido["linha"] = coluna, linha
        self._redesenhar()

    # -- descoberta na rede -------------------------------------------------

    def _laco_rede(self) -> None:
        self._atualizar_rede()
        self.after(2000, self._laco_rede)

    def _atualizar_rede(self) -> None:
        if self.farol is None:
            self.var_rede.set("busca na rede desligada")
            return
        achados = self.farol.lista()
        self._achados = {d["nome"]: d for d in achados}
        selecao = self.arvore_rede.selection()
        anterior = selecao[0] if selecao else None
        self.arvore_rede.delete(*self.arvore_rede.get_children())
        minha_chave = descoberta.impressao_da_chave(self.var_chave.get().strip())
        novos = 0
        for d in achados:
            if d["chave"] and minha_chave and d["chave"] != minha_chave:
                situacao = "outra"
            elif d["chave"] and d["chave"] == minha_chave:
                situacao = "confere"
            else:
                situacao = "?"
            ja_esta = self._pc(d["nome"]) is not None
            novos += 0 if ja_esta else 1
            self.arvore_rede.insert("", "end", iid=d["nome"],
                                    text=d["nome"] + ("  (ja' no layout)"
                                                      if ja_esta else ""),
                                    values=(d["ip"], d["papel"], situacao))
        if anterior and self.arvore_rede.exists(anterior):
            self.arvore_rede.selection_set(anterior)
        if not achados:
            self.var_rede.set("procurando... (abra o programa nos outros PCs)")
        else:
            self.var_rede.set(f"{len(achados)} encontrado(s), {novos} fora do layout")

    def _adicionar_encontrado(self) -> None:
        selecao = self.arvore_rede.selection()
        if not selecao:
            return
        achado = getattr(self, "_achados", {}).get(selecao[0])
        if achado is None:
            return
        existente = self._pc(achado["nome"])
        if existente is not None:
            existente["ip"] = achado["ip"]  # atualiza o IP, que pode ter mudado
            self.selecionado = existente["nome"]
            self._redesenhar()
            self._avisar(f"IP de '{achado['nome']}' atualizado para {achado['ip']}")
            return
        self._novo_pc(achado["nome"], achado["ip"])
        # Se ele se anuncia como servidor e aqui ninguem e', aceita a indicacao.
        if achado["papel"] == "servidor" and not any(p.get("servidor")
                                                     for p in self._pcs()):
            self._marcar_servidor()
        self._avisar(f"'{achado['nome']}' adicionado -- arraste-o no mapa para a "
                     "posicao do monitor dele")

    # -- chave, opcoes, gravacao -------------------------------------------

    def _gerar_chave(self) -> None:
        import secrets
        self.var_chave.set(secrets.token_urlsafe(32))
        self.cfg["chave"] = self.var_chave.get()

    def _copiar_chave(self) -> None:
        self.clipboard_clear()
        self.clipboard_append(self.var_chave.get())
        self._avisar("chave copiada -- cole a mesma chave nos outros PCs")

    def _alternar_windows(self) -> None:
        try:
            conf.definir_inicio_automatico(self.var_windows.get())
        except OSError as exc:
            messagebox.showerror("2pc_1Kit", f"Nao consegui alterar o inicio "
                                             f"automatico:\n{exc}")
            self.var_windows.set(conf.inicio_automatico())

    def _recolher(self) -> list[str]:
        """Passa a tela para o cfg. Devolve os problemas encontrados."""
        self._aplicar_edicao()
        self.cfg["este_pc"] = self.var_eu.get().strip()
        self.cfg["chave"] = self.var_chave.get().strip()
        self.cfg["iniciar_ao_abrir"] = self.var_auto.get()
        self.cfg["usar_bandeja"] = self.var_bandeja.get()
        self.cfg["avisar_troca"] = self.var_avisar.get()
        try:
            self.cfg["porta"] = int(self.var_porta.get())
        except ValueError:
            return ["porta invalida"]

        problemas = lay.Layout.de_config(self.cfg).validar()
        if not self.cfg["chave"]:
            problemas.append("defina uma chave (a mesma em todos os PCs)")
        if self._pc(self.cfg["este_pc"]) is None:
            problemas.append("escolha em 'Este PC e'' qual da lista e' esta maquina")
        return problemas

    def _salvar_silencioso(self) -> None:
        self._recolher()
        conf.salvar(self.cfg)

    def _salvar(self) -> None:
        problemas = self._recolher()
        conf.salvar(self.cfg)
        self._redesenhar()
        if problemas:
            messagebox.showwarning(
                "2pc_1Kit", "Gravado, mas ainda falta:\n\n- "
                            + "\n- ".join(problemas))
        else:
            self._avisar(f"gravado em {conf.caminho_config()}")

    # -- motor --------------------------------------------------------------

    def _iniciar(self) -> None:
        problemas = self._recolher()
        if problemas:
            messagebox.showwarning("2pc_1Kit",
                                   "Corrija antes de iniciar:\n\n- "
                                   + "\n- ".join(problemas))
            return
        conf.salvar(self.cfg)
        try:
            self.motor.iniciar(self.cfg)
        except Exception as exc:
            messagebox.showerror("2pc_1Kit", f"Nao consegui iniciar:\n{exc}")
            return
        self._atualizar_estado()

    def _parar(self) -> None:
        self.motor.parar()
        self._atualizar_estado()

    def _agendar_estado(self) -> None:
        self._na_interface(self._atualizar_estado)

    def _agendar_troca(self, de: str, para: str) -> None:
        """Chamado de dentro do callback do hook: so' agenda, nao desenha."""
        self._na_interface(lambda: self._mostrar_troca(de, para))

    def _mostrar_troca(self, de: str, para: str) -> None:
        if not self.var_avisar.get():
            return
        eu = self.cfg.get("este_pc", "")
        voltou = para == eu
        texto = "teclado e mouse aqui" if voltou else f"teclado e mouse -> {para}"
        self.tarja.mostrar(texto, voltou)
        threading.Thread(target=aviso.bipar, args=(not voltou,),
                         name="bip", daemon=True).start()

    def _avisar(self, texto: str, segundos: float = 6.0) -> None:
        """Mensagem transitoria que o laco de estado nao pode apagar na hora."""
        self._aviso_ate = time.monotonic() + segundos
        self.var_estado.set(texto)

    def _atualizar_estado(self) -> None:
        ativo = self.motor.ativo()
        self.botao_iniciar.configure(state="disabled" if ativo else "normal")
        self.botao_parar.configure(state="normal" if ativo else "disabled")
        if time.monotonic() >= self._aviso_ate:
            self.var_estado.set(self.motor.resumo())

    def _laco_estado(self) -> None:
        """Um unico temporizador; chamar _atualizar_estado nao cria outro."""
        self._atualizar_estado()
        # O servidor pode ter mandado o layout: redesenha a lista e o mapa.
        papel = self.motor.papel
        if papel is not None and getattr(papel, "config_mudou", False):
            papel.config_mudou = False
            self._carregar_na_tela()
            self._avisar("layout recebido do servidor", 10)
        self.after(1500, self._laco_estado)

    # -- log ----------------------------------------------------------------

    def _ligar_log(self) -> None:
        manipulador = ManipuladorDeLog(self.fila_log)
        manipulador.setFormatter(logging.Formatter(
            "%(asctime)s %(name)-10s %(message)s", datefmt="%H:%M:%S"))
        logging.getLogger().addHandler(manipulador)

    def _consumir_log(self) -> None:
        linhas = []
        while True:
            try:
                linhas.append(self.fila_log.get_nowait())
            except queue.Empty:
                break
        if linhas:
            self.texto_log.configure(state="normal")
            self.texto_log.insert("end", "\n".join(linhas) + "\n")
            if int(self.texto_log.index("end-1c").split(".")[0]) > 500:
                self.texto_log.delete("1.0", "200.0")
            self.texto_log.see("end")
            self.texto_log.configure(state="disabled")
        self.after(200, self._consumir_log)

    # -- copiar e compartilhar o registro -----------------------------------

    def _copiar_selecao(self, _evento=None) -> str:
        try:
            trecho = self.texto_log.get("sel.first", "sel.last")
        except tk.TclError:
            return "break"  # nada selecionado
        self.clipboard_clear()
        self.clipboard_append(trecho)
        self._avisar(f"{len(trecho.splitlines())} linha(s) copiada(s)")
        return "break"

    def _selecionar_tudo(self, _evento=None) -> str:
        self.texto_log.tag_add("sel", "1.0", "end-1c")
        return "break"

    def _copiar_registro(self) -> None:
        trecho = self.texto_log.get("1.0", "end-1c")
        if not trecho.strip():
            self._avisar("o registro esta' vazio")
            return
        self.clipboard_clear()
        self.clipboard_append(trecho)
        self._avisar(f"registro copiado ({len(trecho.splitlines())} linhas) -- "
                     "cole onde quiser")

    def _limpar_registro(self) -> None:
        self.texto_log.configure(state="normal")
        self.texto_log.delete("1.0", "end")
        self.texto_log.configure(state="disabled")

    def _abrir_pasta(self) -> None:
        import os
        try:
            os.startfile(conf.pasta_de_saida())
        except OSError as exc:
            self._avisar(f"nao consegui abrir a pasta: {exc}")

    def _gerar_relatorio(self) -> None:
        self._recolher()
        self._avisar("gerando relatorio (testando as conexoes)...", 60)

        def trabalhar() -> None:
            try:
                caminho = relatorio.gerar(self.cfg, self.farol)
            except Exception as exc:
                logging.getLogger("relatorio").error("falhou", exc_info=True)
                self._na_interface(lambda: self._avisar(f"nao consegui gerar o "
                                                        f"relatorio: {exc}", 20))
                return
            self._na_interface(lambda: self._relatorio_pronto(caminho))

        threading.Thread(target=trabalhar, name="relatorio", daemon=True).start()

    def _relatorio_pronto(self, caminho) -> None:
        self.clipboard_clear()
        self.clipboard_append(str(caminho))
        self._avisar(f"relatorio em {caminho} (caminho copiado)", 30)
        if messagebox.askyesno(
                "2pc_1Kit",
                f"Relatorio gerado:\n\n{caminho}\n\n"
                f"O caminho ja' foi copiado. O arquivo nao contem a chave "
                f"compartilhada.\n\nAbrir a pasta agora?"):
            self._abrir_pasta()

    # -- bandeja e fechamento ----------------------------------------------

    def _ao_fechar(self) -> None:
        if self.var_bandeja.get() and self._recolher_para_bandeja():
            return
        self.motor.parar()
        self.destroy()

    def _recolher_para_bandeja(self) -> bool:
        try:
            import bandeja
        except ImportError:
            return False
        if self._icone_bandeja is None:
            self._icone_bandeja = bandeja.criar(self._restaurar, self._sair_de_vez,
                                                self.motor)
            if self._icone_bandeja is None:
                return False
        self.withdraw()
        return True

    def _restaurar(self) -> None:
        self.after(0, self.deiconify)

    def _sair_de_vez(self) -> None:
        self.after(0, self._encerrar)

    def _encerrar(self) -> None:
        if self._icone_bandeja is not None:
            self._icone_bandeja.stop()
        self.motor.parar()
        self.destroy()


def abrir(cfg: dict, farol: descoberta.Farol | None = None) -> None:
    Janela(cfg, farol).mainloop()
