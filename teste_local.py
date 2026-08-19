"""Auto-teste que roda num PC so': o que da' para verificar sem os outros PCs.

    python teste_local.py

Cobre: ida e volta de imagem no clipboard, vizinhanca no layout, instalacao dos
hooks, injecao de mouse e o roteamento do cursor entre varios PCs. Nao mexe no
mouse de verdade na parte do roteamento (`mover_cursor` e' substituido); a parte
de injecao move o cursor e o devolve ao lugar.
"""

from __future__ import annotations

import base64
import ctypes
import inspect
import io
import pathlib
import sys
import time
import threading

from PIL import Image

import alvo as alv
import arquivos
import borda
import cliente
import clipboard_win as cw
import configuracao as conf
import diagnostico
import entrada_win as ew
import layout as lay
import tema as tm

falhas: list[str] = []


def cliente_falso(enviados: list, injetado: list) -> "cliente.Cliente":
    """Um Cliente sem rede, sem hooks e sem mexer no mouse de verdade."""
    cli = cliente.Cliente.__new__(cliente.Cliente)
    cli.x0, cli.y0, cli.largura, cli.altura = 0, 0, 2560, 1080
    cli.monitores = [(0, 0, 2560, 1080)]
    cli.alvo = alv.Alvo(0, 0, 2560, 1080, cli.monitores)
    cli.injetor = type("I", (), {
        "mover_para": lambda s, x, y: injetado.append((x, y)),
        "soltar_modificadores": lambda s: None})()
    cli.remoto = cli.aguardando = False
    cli._esperando_desde = 0.0
    cli.conectado = True
    cli.ao_mudar = lambda: None
    cli.ao_trocar = lambda de, para: None
    cli.eu = "esq"
    cli.layout = lay.Layout([lay.PC("SRV", "1", 2, 1, True),
                             lay.PC("esq", "2", 1, 1)])
    cli.fila = None
    cli.enfileirar = lambda ev: enviados.append(ev)
    cli.comando = cliente.ComandoLocal(cli)
    cli.comando.ancora = (1280, 540)
    return cli


def checar(nome: str, condicao: bool, extra: str = "") -> None:
    print(f"  {nome:46} {'OK' if condicao else 'FALHOU'} {extra}")
    if not condicao:
        falhas.append(nome)


# -- clipboard --------------------------------------------------------------


def teste_clipboard() -> None:
    print("clipboard (ida e volta de imagem)")
    original = Image.new("RGB", (320, 200))
    for x in range(320):
        for y in range(0, 200, 7):
            original.putpixel((x, y), (x % 256, y % 256, (x + y) % 256))
    buf = io.BytesIO()
    original.save(buf, "PNG")
    msg = {"t": "clip", "fmt": "imagem",
           "dados": base64.b64encode(buf.getvalue()).decode()}

    guardado = cw.ler()  # preservar o que o usuario tinha copiado
    cw.escrever(msg)
    volta = cw.ler()
    checar("formato preservado", volta is not None and volta["fmt"] == "imagem")
    if volta:
        img = Image.open(io.BytesIO(base64.b64decode(volta["dados"])))
        checar("dimensoes preservadas", img.size == original.size, str(img.size))
        checar("pixels identicos",
               list(img.get_flattened_data()) == list(original.get_flattened_data()))
    if guardado:
        cw.escrever(guardado)


# -- layout -----------------------------------------------------------------


def _layout_de_teste() -> lay.Layout:
    """A (0,1) -- B (1,1) -- C (2,1), e D em cima do B (1,0)."""
    return lay.Layout([
        lay.PC("A", "10.0.0.1", 0, 1),
        lay.PC("B", "10.0.0.2", 1, 1, servidor=True),
        lay.PC("C", "10.0.0.3", 2, 1),
        lay.PC("D", "10.0.0.4", 1, 0),
    ])


def teste_layout() -> None:
    print("layout (vizinhanca)")
    lo = _layout_de_teste()
    checar("vizinho a' direita de B e' C",
           getattr(lo.vizinho("B", "direita"), "nome", None) == "C")
    checar("vizinho a' esquerda de B e' A",
           getattr(lo.vizinho("B", "esquerda"), "nome", None) == "A")
    checar("vizinho acima de B e' D",
           getattr(lo.vizinho("B", "cima"), "nome", None) == "D")
    checar("B nao tem vizinho abaixo", lo.vizinho("B", "baixo") is None)
    checar("C nao tem vizinho a' direita", lo.vizinho("C", "direita") is None)
    checar("com buraco na grade, A ainda alcanca C pela direita",
           getattr(lay.Layout([lay.PC("A", "x", 0, 0, True),
                               lay.PC("C", "y", 4, 0)]).vizinho("A", "direita"),
                   "nome", None) == "C")
    checar("layout valido nao acusa problema", lo.validar() == [], str(lo.validar()))

    ruim = lay.Layout([lay.PC("A", "", 0, 0, True), lay.PC("B", "", 5, 5)])
    problemas = ruim.validar()
    checar("acusa cliente sem IP", any("sem IP" in p for p in problemas))
    checar("acusa PC sem fronteira", any("fronteira" in p for p in problemas))

    x, y = lay.ponto_de_entrada("esquerda", 0.5, 0, 0, 1000, 500, margem=4)
    checar("entrada pela esquerda no meio da altura", (x, y) == (4, 249.5),
           f"({x},{y})")
    checar("saida detectada pela direita",
           lay.direcao_de_saida(1000, 200, 0, 0, 1000, 500) == "direita")
    checar("ponto dentro nao e' saida",
           lay.direcao_de_saida(500, 200, 0, 0, 1000, 500) is None)


# -- Win32 ------------------------------------------------------------------


def teste_injecao() -> None:
    print("injecao de mouse (SendInput)")
    x0, y0, largura, altura = ew.geometria_virtual()
    injetor = ew.Injetor()
    origem = ew.posicao_cursor()
    if not ew.user32.SetCursorPos(*origem):
        print("  PULADO: este desktop nao aceita injecao de input (tela "
              "bloqueada, sessao remota ou desktop nao interativo).")
        return
    for ax, ay in ((300, 300), (x0 + largura - 1, y0 + altura - 1)):
        injetor.mover_para(ax, ay)
        time.sleep(0.1)
        obtido = ew.posicao_cursor()
        checar(f"mover_para({ax},{ay})",
               abs(obtido[0] - ax) <= 1 and abs(obtido[1] - ay) <= 1, str(obtido))
    injetor.mover_para(*origem)


def teste_shift_direito() -> None:
    """Regressao: o Shift direito nao funcionava no PC comandado.

    O hook de baixo nivel entrega o Shift direito com LLKHF_EXTENDED ligado, e o
    injetor repassava o flag adiante. So' que `E0 36` NAO e' o Shift direito: e'
    o "fake shift" que o Windows fabrica em volta das teclas do teclado
    numerico. O outro PC recebia um evento que o Windows descarta em silencio.
    O Shift esquerdo escapava porque o scancode dele (0x2A) chega sem o flag.

    Nao injeta nada: inspeciona o INPUT montado. Assim vale tambem em desktop
    que nao aceita injecao -- que e' justamente onde o bug passou despercebido.
    """
    print("Shift direito nao pode virar E0 36")
    injetor = ew.Injetor()
    enviados: list = []
    injetor._enviar = lambda *entradas: enviados.extend(entradas)

    def flags(vk: int, scan: int, ext: bool, pressionar: bool = True) -> int:
        enviados.clear()
        injetor.tecla(vk, scan, ext, pressionar)
        return enviados[0].ki.dwFlags

    checar("Shift direito perde a estendida (o hook diz True)",
           not flags(0xA1, 0x36, True) & ew.KEYEVENTF_EXTENDEDKEY)
    checar("Shift direito continua indo por scancode",
           bool(flags(0xA1, 0x36, True) & ew.KEYEVENTF_SCANCODE))
    checar("keyup do Shift direito tambem perde a estendida",
           not flags(0xA1, 0x36, True, False) & ew.KEYEVENTF_EXTENDEDKEY
           and bool(flags(0xA1, 0x36, True, False) & ew.KEYEVENTF_KEYUP))
    # Ctrl e Alt direitos sao o contraste: neles o prefixo E0 E' a tecla certa,
    # entao uma "correcao" que zerasse a estendida para todo mundo os quebraria.
    checar("Ctrl direito mantem a estendida (E0 1D e' a tecla)",
           bool(flags(0xA3, 0x1D, True) & ew.KEYEVENTF_EXTENDEDKEY))
    checar("Alt direito mantem a estendida (E0 38 e' a tecla)",
           bool(flags(0xA5, 0x38, True) & ew.KEYEVENTF_EXTENDEDKEY))
    checar("Shift esquerdo segue sem estendida",
           not flags(0xA0, 0x2A, False) & ew.KEYEVENTF_EXTENDEDKEY)


def teste_espera_do_sair() -> None:
    """Regressao: o cliente podia ficar esperando para sempre a resposta do 'sair'.

    Ao sair por uma aresta o cliente para de injetar e so' volta com 'entrar' ou
    'soltar'. Se nenhum dos dois chega, todo movimento e' descartado dali em
    diante, em silencio: mouse parado, teclado funcionando -- porque o teclado
    nao passa por este estado.

    Sao duas defesas independentes, e o teste cobre as duas:
      1. o SERVIDOR passa a responder 'soltar' mesmo quando ignora o aviso;
      2. o CLIENTE desiste depois de ESPERA_MAXIMA e volta a injetar.
    """
    print("espera da resposta ao 'sair'")

    # -- 1. o servidor nao pode ignorar em silencio --------------------------
    enviados: list[tuple[str, dict]] = []
    ctl = borda.Controle(_layout_de_teste(), "B",
                         lambda destino, msg: enviados.append((destino, msg)))
    ctl.conectados.update({"A", "C"})
    ctl.atual = "C"  # o cursor esta' em C...
    ctl.saiu_do_cliente("A", "direita", 0.5)  # ...e quem avisa saida e' A
    checar("aviso de quem nao tem o cursor e' respondido com soltar",
           enviados == [("A", {"t": "soltar"})], str(enviados))
    checar("e o cursor nao se mexe por causa disso", ctl.atual == "C")

    # -- 2. o cliente desiste sozinho ----------------------------------------
    mandados: list[dict] = []
    injetado: list[tuple] = []
    cli = cliente_falso([], injetado)
    conn = type("C", (), {"enviar": lambda s, m: mandados.append(m)})()
    cli.remoto = True
    cli.alvo.entrar("direita", 0.5)

    # Empurra para fora da aresta esquerda ate' o cliente avisar a saida.
    for _ in range(3):
        cli._aplicar(conn, None, {"t": "mv", "dx": -3000, "dy": 0})
    checar("saida avisada ao servidor",
           any(m.get("t") == "sair" for m in mandados), str(mandados))
    checar("e o cliente passou a esperar", cli.aguardando)

    injetado.clear()
    cli._aplicar(conn, None, {"t": "mv", "dx": 10, "dy": 0})
    checar("enquanto espera, o movimento e' descartado", injetado == [])

    # Sem dormir 5s: envelhece o relogio da espera.
    cli._esperando_desde -= cliente.ESPERA_MAXIMA + 0.1
    cli._aplicar(conn, None, {"t": "mv", "dx": 10, "dy": 0})
    checar("passado o prazo, volta a injetar", injetado != [], str(injetado))
    checar("e sai do estado de espera", not cli.aguardando)


def teste_renomeacao_e_tema() -> None:
    """A renomeacao da v1.3 nao pode custar a configuracao de ninguem.

    O programa passou de '2pc_1Kit' para 'Multi PC - KVM', e a pasta de dados
    acompanhou. Sem migracao, atualizar equivaleria a apagar o config.json do
    ponto de vista do usuario -- e dentro dele esta' a CHAVE COMPARTILHADA, cuja
    perda faz o handshake falhar sem explicacao obvia nos dois PCs.
    """
    print("renomeacao (v1.3) e paletas de tema")
    import os
    import tempfile

    checar("nome de arquivo nao tem espaco",
           " " not in conf.APP_ARQUIVO, conf.APP_ARQUIVO)
    checar("nome de exibicao e' o novo", conf.APP == "Multi PC - KVM", conf.APP)

    with tempfile.TemporaryDirectory() as tmp:
        antigo_appdata = os.environ.get("APPDATA")
        os.environ["APPDATA"] = tmp
        try:
            antiga = pathlib.Path(tmp) / conf.APP_ANTIGO
            antiga.mkdir(parents=True)
            (antiga / "config.json").write_text(
                '{"chave": "segredo-de-antes", "porta": 24810}', encoding="utf-8")

            nova = conf.pasta_de_dados()
            trazido = (nova / "config.json")
            checar("a pasta nova nasce com o config antigo dentro",
                   trazido.is_file())
            checar("e a chave compartilhada sobrevive",
                   "segredo-de-antes" in trazido.read_text(encoding="utf-8"))
            checar("o config antigo NAO e' movido (volta atras continua possivel)",
                   (antiga / "config.json").is_file())

            # Segunda execucao: o que ja' existe manda, senao a migracao
            # sobrescreveria alteracoes feitas depois de atualizar.
            trazido.write_text('{"chave": "mudei-depois"}', encoding="utf-8")
            conf.pasta_de_dados()
            checar("execucao seguinte nao sobrescreve o que o usuario mudou",
                   "mudei-depois" in trazido.read_text(encoding="utf-8"))
        finally:
            if antigo_appdata is None:
                os.environ.pop("APPDATA", None)
            else:
                os.environ["APPDATA"] = antigo_appdata

    # Paleta incompleta so' apareceria ao abrir a janela no tema errado.
    faltando = set(tm.PALETAS[tm.CLARO]) ^ set(tm.PALETAS[tm.ESCURO])
    checar("as duas paletas tem exatamente as mesmas cores", not faltando,
           str(sorted(faltando)))
    checar("preferencia explicita manda", tm.resolver(tm.ESCURO) == tm.ESCURO)
    checar("'sistema' resolve para uma paleta de verdade",
           tm.resolver(tm.SISTEMA) in (tm.CLARO, tm.ESCURO))


def _da_para_renomear(caminho: pathlib.Path) -> bool:
    """No Windows, renomear falha enquanto alguem mantem o arquivo aberto."""
    provisorio = caminho.with_suffix(caminho.suffix + ".livre")
    try:
        caminho.rename(provisorio)
        provisorio.rename(caminho)
        return True
    except OSError:
        return False


def teste_transferencia_de_arquivos() -> None:
    """Copiar arquivo num PC e colar no outro.

    O clipboard do Windows guarda CAMINHOS (CF_HDROP), nao arquivos; mandar a
    lista pela rede nao serviria de nada do outro lado. O conteudo vai em blocos,
    e o que este teste protege e' a remontagem: um arquivo maior que um bloco tem
    de chegar byte a byte igual, na ordem certa e sem pedaco perdido.
    """
    print("transferencia de arquivos pelo clipboard")
    import hashlib
    import os
    import tempfile

    bloco_real = arquivos.BLOCO
    appdata_real = os.environ.get("APPDATA")
    with tempfile.TemporaryDirectory() as base:
        try:
            # Pasta de recebidos isolada, e bloco pequeno para varios blocos sem
            # gastar segundos gerando megabytes.
            os.environ["APPDATA"] = base
            arquivos.BLOCO = 64 * 1024

            origem = pathlib.Path(base) / "origem"
            origem.mkdir()
            pequeno = origem / "plano.txt"
            pequeno.write_text("acento: cao, agua\n", encoding="utf-8")
            grande = origem / "com espaco.bin"
            grande.write_bytes(os.urandom(200 * 1024))  # ~4 blocos
            sha = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                   for p in (pequeno, grande)}

            pronto = arquivos.preparar([str(pequeno), str(grande)])
            checar("aceita a selecao de dois arquivos", pronto is not None)
            lista, total = pronto

            recepcao = arquivos.Recepcao()
            prontos, blocos = None, 0
            for msg in arquivos.mensagens(lista, total, "teste1"):
                blocos += msg["e"] == "bloco"
                resultado = recepcao.aplicar(msg)
                if resultado:
                    prontos = resultado
            checar("foi em varios blocos", blocos >= 4, f"{blocos} blocos")
            checar("a transferencia completou", prontos is not None)
            checar("chegaram os dois arquivos", len(prontos or []) == 2)
            checar("conteudo identico byte a byte",
                   all(hashlib.sha256(c.read_bytes()).hexdigest() == sha[c.name]
                       for c in prontos or []))

            # Sem isto, colar de um lado devolveria tudo para o outro, para
            # sempre: os recebidos ficam no clipboard e o poller os veria.
            checar("nao devolve o que acabou de chegar",
                   arquivos.preparar([str(c) for c in prontos]) is None)
            checar("recusa pasta", arquivos.preparar([str(origem)]) is None)

            # O nome vem pela rede. Cifra e autenticacao protegem contra
            # estranho, nao contra bug do outro lado.
            checar("nome com ..\\ nao escapa da pasta",
                   arquivos._nome_seguro(r"..\..\Windows\algo.exe") == "algo.exe")
            checar("nome vazio ainda gera algo gravavel",
                   bool(arquivos._nome_seguro("")))

            # Transferencia interrompida nao pode deixar arquivo pela metade
            # passando por completo.
            r2 = arquivos.Recepcao()
            gerador = arquivos.mensagens(lista, total, "teste2")
            r2.aplicar(next(gerador))          # inicio
            r2.aplicar(next(gerador))          # um bloco
            r2.aplicar({"t": "arq", "e": "aborta", "id": "teste2", "motivo": "x"})
            checar("abortar limpa a pasta da transferencia",
                   r2.destino is None or not r2.destino.exists())
            # Abandonar o generator no meio deixa o arquivo de ORIGEM aberto, e
            # ninguem consegue mover nem apagar o arquivo do usuario enquanto
            # isso. Quem envia usa contextlib.closing por este motivo.
            gerador.close()
            checar("fechar o fluxo libera o arquivo de origem",
                   _da_para_renomear(grande), str(grande))
        finally:
            arquivos.BLOCO = bloco_real
            if appdata_real is None:
                os.environ.pop("APPDATA", None)
            else:
                os.environ["APPDATA"] = appdata_real


def teste_regra_de_firewall() -> None:
    """Regressao: a regra ficava valendo para nada e o painel dizia que existia.

    Ate' a v1.3 a regra era "qualquer programa, perfis privado e de dominio". O
    cabo direto entre dois PCs quase sempre entra como "Rede nao identificada",
    que o Windows trata como PUBLICA -- e naquele perfil a regra existe, aparece
    no painel e nao vale para nada. O que segurava a conexao de pe' era outra
    regra, criada pelo aviso do Windows e presa ao caminho do executavel; ao
    renomear o programa ela deixou de casar, e a conexao caiu com timeout, sem
    uma linha dizendo que era o firewall.

    Nao mexe no firewall: intercepta a chamada ao netsh e confere os argumentos.
    """
    print("regra de firewall")
    chamadas: list[tuple] = []
    netsh_real, frozen_real = diagnostico._netsh, getattr(sys, "frozen", False)
    diagnostico._netsh = lambda *a: (chamadas.append(a), (0, ""))[1]
    try:
        # -- empacotado: presa ao executavel, todos os perfis
        sys.frozen = True
        chamadas.clear()
        ok, texto = diagnostico.liberar(24810, 24811)
        adicoes = [a for a in chamadas if a[0] == "add"]
        checar("liberou", ok, texto)
        checar("criou as duas regras (TCP e UDP)", len(adicoes) == 2)
        checar("presa ao executavel",
               all(any(p.startswith("program=") for p in a) for a in adicoes))
        checar("vale em todos os perfis",
               all("profile=any" in a for a in adicoes))
        checar("e as regras do nome antigo sao removidas",
               all(any(f"name={n}" in a for a in chamadas if a[0] == "delete")
                   for n in diagnostico.REGRAS_ANTIGAS))

        # -- do codigo-fonte: nao pode prender no python.exe nem abrir publico
        del sys.frozen
        chamadas.clear()
        diagnostico.liberar(24810, 24811)
        adicoes = [a for a in chamadas if a[0] == "add"]
        checar("do fonte, NAO prende regra em programa nenhum",
               not any(p.startswith("program=") for a in adicoes for p in a))
        checar("do fonte, nao abre em rede publica",
               all("profile=private,domain" in a for a in adicoes))
    finally:
        diagnostico._netsh = netsh_real
        if frozen_real:
            sys.frozen = frozen_real
        elif hasattr(sys, "frozen"):
            del sys.frozen


def teste_vigia_do_teclado() -> None:
    """Regressao: o gancho do teclado morria e ninguem percebia.

    O vigia compara o Raw Input com `_visto_hook`, e quem marca `_visto_hook` e'
    so' o callback do MOUSE. Derrubado apenas o gancho do teclado -- basta um
    callback passar de ~300 ms, e o RDP torna isso facil --, o mouse seguia
    marcando, o atraso ficava em zero e o vigia nunca disparava: o mouse
    atravessava e o teclado nao, ate' reiniciar o programa.

    Testa a decisao (`_decidir`), nao o laco: sem thread e sem espera real.
    """
    print("vigia mantem o gancho do teclado vivo")
    cap = ew.Captura.__new__(ew.Captura)  # sem __init__: nada aqui instala hook
    cap._visto_raw = cap._visto_hook = cap._visto_teclado = 0.0

    agora = 1000.0
    checar("tudo em silencio e' normal (ninguem mexendo)",
           cap._decidir(agora, agora) is None)

    # Mouse saudavel: Raw Input e gancho do mouse em dia. Antes, isto bastava
    # para o vigia nunca mais reinstalar -- e o teclado ficava morto para sempre.
    cap._visto_raw = cap._visto_hook = agora
    checar("mouse saudavel nao dispara aviso",
           cap._decidir(agora, agora) is None)
    decisao = cap._decidir(agora + ew.INTERVALO_RENOVACAO, agora)
    checar("mas passa a renovar por tempo, mesmo sem defeito detectado",
           decisao is not None and decisao[0] == "renovacao periodica")
    checar("e a renovacao de rotina nao vira aviso no log",
           decisao is not None and decisao[1] is False)

    # Gancho do mouse atrasado em relacao ao Raw Input: o caso anormal.
    cap._visto_hook = agora - (ew.TOLERANCIA_VIGIA + 1)
    cap._visto_raw = agora
    decisao = cap._decidir(agora, agora)
    checar("mouse atras do Raw Input dispara reinstalacao",
           decisao is not None and "gancho do mouse" in decisao[0])
    checar("e essa vai como aviso", decisao is not None and decisao[1] is True)

    # Comandando outro PC: a renovacao tem de ficar curta, senao um programa
    # aberto depois (o cliente de Area de Trabalho Remota em tela cheia) fica na
    # frente na fila dos ganchos e o teclado para de atravessar ate' a proxima
    # renovacao -- que, no intervalo longo, e' um minuto inteiro.
    cap._visto_raw = cap._visto_hook = agora
    cap.em_disputa = lambda: True
    decisao = cap._decidir(agora + ew.INTERVALO_DISPUTA, agora)
    checar("comandando, renova no intervalo da disputa",
           decisao is not None and "rapida" in decisao[0])
    # A cadencia rapida foi RECUADA em 11/08: com 2s o servidor caiu tres vezes
    # em tres minutos, contra 7 horas limpas antes. Enquanto a corrupcao de heap
    # nao for localizada, agitar o subsistema dos ganchos nao compensa.
    checar("a cadencia da disputa nao e' mais agressiva que a normal",
           ew.INTERVALO_DISPUTA >= ew.INTERVALO_RENOVACAO,
           f"{ew.INTERVALO_DISPUTA}s vs {ew.INTERVALO_RENOVACAO}s")
    cap.em_disputa = lambda: False
    checar("em casa, o intervalo nao encurta",
           cap._decidir(agora + ew.INTERVALO_DISPUTA - 1, agora) is None)
    cap.em_disputa = lambda: 1 / 0  # dono com defeito nao pode derrubar o vigia
    checar("erro no sinal do dono nao quebra a decisao",
           cap._decidir(agora + ew.INTERVALO_RENOVACAO, agora) is not None)
    cap.em_disputa = lambda: False

    # A marcacao do teclado tem de valer tambem para evento nosso (com MARCA),
    # senao "ha' quanto tempo o gancho nao e' chamado" mede a coisa errada.
    fonte = inspect.getsource(ew.Captura._teclado)
    antes_da_marca = fonte.split("dwExtraInfo == MARCA")[0]
    checar("o gancho do teclado marca antes do filtro da MARCA",
           "_visto_teclado" in antes_da_marca)


def teste_raw_input_apos_religar() -> None:
    """Regressao: religar o servidor matava o mouse e deixava so' o teclado.

    A classe de janela do Raw Input vale para o processo inteiro e guarda o
    WNDPROC de quem a registrou. Parar e iniciar de novo cria uma Captura nova,
    o RegisterClass falha com ERROR_CLASS_ALREADY_EXISTS -- que e' tolerado --
    e a janela nascia apontando para o WNDPROC da instancia MORTA. O WM_INPUT
    ia para um objeto que nao comandava mais nada e o movimento sumia em
    silencio; o teclado sobrevivia porque os hooks sao reinstalados por
    instancia.

    Nao injeta nada e nao depende de mexer no mouse: pergunta ao Windows de quem
    e' o WNDPROC da janela, que e' a pergunta que separa os dois casos.
    """
    print("Raw Input sobrevive a religar o servidor")

    def endereco(proc) -> int:
        return ctypes.cast(proc, ctypes.c_void_p).value

    primeira = ew.Captura(lambda ev: False, lambda dx, dy: None)
    primeira.start()
    checar("1a captura instalou", primeira.pronta.wait(5))
    checar("1a captura registrou o Raw Input", primeira.raw_ativo)
    primeira.parar()

    segunda = ew.Captura(lambda ev: False, lambda dx, dy: None)
    segunda.start()
    checar("2a captura instalou", segunda.pronta.wait(5))
    checar("2a captura registrou o Raw Input", segunda.raw_ativo)
    # O nucleo: a janela da 2a instancia tem de despachar para a 2a instancia.
    dono = ew._ler_wndproc(segunda._hwnd, ew.GWLP_WNDPROC)
    checar("o WM_INPUT vai para a captura NOVA",
           dono == endereco(segunda._proc_janela))
    checar("e nao para a instancia morta",
           dono != endereco(primeira._proc_janela))
    segunda.parar()


def teste_hooks() -> None:
    print("hooks de baixo nivel")
    captura = ew.Captura(lambda ev: False)  # nunca bloqueia: seguro
    captura.start()
    checar("SetWindowsHookEx instalou", captura.pronta.wait(5))
    captura.parar()
    checar("hooks desinstalados ao parar", not captura.is_alive())


# -- roteamento do cursor ---------------------------------------------------


def teste_roteamento() -> None:
    print("roteamento do cursor entre PCs")
    x0, y0, largura, altura = ew.geometria_virtual()
    movidos: list = []
    borda.ew.mover_cursor = lambda x, y: movidos.append((x, y))
    enviados: list[tuple[str, dict]] = []
    ctl = borda.Controle(_layout_de_teste(), "B",
                         lambda destino, msg: enviados.append((destino, msg)))

    def na_borda(lado):
        pontos = {"direita": (x0 + largura - 1, 300), "esquerda": (x0, 300),
                  "cima": (600, y0), "baixo": (600, y0 + altura - 1)}
        return {"t": "mv", "pos": pontos[lado]}

    checar("sem ninguem conectado, a borda nao captura",
           ctl.tratar(na_borda("direita")) is False)
    ctl.conectados.update({"A", "C", "D"})

    checar("mouse no meio passa direto",
           ctl.tratar({"t": "mv", "pos": (600, 500)}) is False)
    checar("borda direita entrega o cursor a C",
           ctl.tratar(na_borda("direita")) is True)
    checar("mandou soltar modificadores locais",
           enviados[0] == (borda.LOCAL, {"t": "soltar_local"}))
    checar("C recebeu 'entrar' pela esquerda",
           enviados[1][0] == "C" and enviados[1][1]["de"] == "esquerda")
    checar("cursor registrado em C", ctl.atual == "C")

    # O movimento remoto vem do Raw Input, nao da posicao do hook.
    ctl.delta_bruto(17, -9)
    checar("delta bruto repassado ao PC atual",
           enviados[-1] == ("C", {"t": "mv", "dx": 17, "dy": -9}), str(enviados[-1]))
    antes = len(enviados)
    ctl.tratar({"t": "mv", "pos": (ctl.ancora[0] + 500, ctl.ancora[1])})
    checar("posicao do hook nao gera movimento em modo remoto",
           len(enviados) == antes)
    checar("tecla vai para C",
           ctl.tratar({"t": "key", "vk": 65, "sc": 30, "ext": False,
                       "down": True}) is True and enviados[-1][0] == "C")

    # C nao tem vizinho a' direita: o cursor tem de ficar em C
    enviados.clear()
    ctl.saiu_do_cliente("C", "direita", 0.4)
    checar("sem vizinho, o cursor fica onde estava", ctl.atual == "C")
    checar("e C e' reposicionado na propria borda",
           enviados[-1] == ("C", {"t": "entrar", "de": "direita", "rel": 0.4}))

    # C -> esquerda volta para B (o servidor)
    enviados.clear()
    ctl.saiu_do_cliente("C", "esquerda", 0.25)
    checar("voltou para o servidor", ctl.atual == "B")
    checar("C foi avisado de que perdeu o cursor",
           ("C", {"t": "soltar"}) in enviados)
    esperado = lay.ponto_de_entrada("direita", 0.25, x0, y0, largura, altura,
                                    borda.MARGEM)
    checar("cursor reposicionado na borda direita do servidor",
           movidos[-1] == (int(esperado[0]), int(esperado[1])), str(movidos[-1]))

    # outra aresta: a de cima leva a D (depois da trava anti-reentrada)
    time.sleep(borda.TRAVA_APOS_RETORNO + 0.05)
    ctl.tratar(na_borda("cima"))
    checar("borda de cima entrega o cursor a D", ctl.atual == "D")
    enviados.clear()
    ctl.saiu_do_cliente("D", "baixo", 0.5)
    checar("D -> baixo devolve ao servidor", ctl.atual == "B")

    # cliente que cai enquanto tem o cursor
    time.sleep(borda.TRAVA_APOS_RETORNO + 0.05)
    ctl.tratar(na_borda("direita"))
    checar("cursor em C de novo", ctl.atual == "C")
    ctl.cliente_caiu("C")
    checar("queda de C devolve o cursor ao servidor", ctl.atual == "B")
    checar("C saiu da lista de conectados", "C" not in ctl.conectados)

    # trava e panico
    checar("trava impede reentrar na hora", ctl.tratar(na_borda("cima")) is False)
    time.sleep(borda.TRAVA_APOS_RETORNO + 0.05)
    checar("reentra depois da trava expirar", ctl.tratar(na_borda("cima")) is True)
    borda.ew.modificador_pressionado = lambda vk: True  # simula Ctrl+Alt+Shift
    ctl.tratar({"t": "key", "vk": ew.VK_ESCAPE, "sc": 1, "ext": False, "down": True})
    checar("atalho de panico devolve o controle", ctl.atual == "B")


def teste_watchdog_devolve_comando() -> None:
    """Cliente que TRAVA (nao fecha o TCP) tem de devolver o comando ao servidor.

    O `_receber_cliente` so' limpa quando o `receber` levanta; um cliente travado
    deixa o `receber` pendurado para sempre. O watchdog (`_derrubar`, chamado por
    `_vigiar` quando o pong seca) cobre esse buraco. Aqui exercitamos o
    `_derrubar` isolado: com o cursor num cliente, derruba-lo devolve o controle.
    """
    print("watchdog: cliente travado devolve o comando ao servidor")
    import servidor as srvmod

    borda.ew.mover_cursor = lambda x, y: None  # nao mexe no cursor real
    enviados: list = []
    ctl = borda.Controle(_layout_de_teste(), "B",
                         lambda destino, msg: enviados.append((destino, msg)))
    ctl.conectados.update({"A", "C", "D"})

    # Servidor sem __init__: evita instalar hooks/ler cfg; so' o que _derrubar usa.
    srv = object.__new__(srvmod.Servidor)
    srv._lock = threading.Lock()
    srv.clientes = {}
    srv.ultimo_pong = {}
    srv.ao_mudar = lambda: None
    srv.controle = ctl

    # Leva o cursor para C (encosta na borda direita) e trava o C.
    x0, y0, largura, altura = ew.geometria_virtual()
    ctl.tratar({"t": "mv", "pos": (x0 + largura - 1, 300)})
    checar("cursor foi para C", ctl.atual == "C")

    class FakeConn:
        def __init__(self): self.fechado = False
        def fechar(self): self.fechado = True

    conn = FakeConn()
    srv.clientes["C"] = conn
    srv.ultimo_pong["C"] = 0.0  # muito antigo -> travado para o watchdog

    srv._derrubar("C", conn, "parou de responder (teste)")
    checar("comando voltou ao servidor", ctl.atual == "B")
    checar("C saiu da lista de clientes", "C" not in srv.clientes)
    checar("socket do C foi fechado", conn.fechado is True)

    # Idempotente: derrubar de novo (ja' saiu) nao re-age nem explode.
    conn2 = FakeConn()
    srv._derrubar("C", conn2, "de novo")
    checar("derrubar repetido e' inofensivo", conn2.fechado is False)


def teste_quique() -> None:
    """Regressao: o cursor entra a MARGEM px da borda e um tremor de mao no
    sentido de volta devolvia o controle na hora -- o cursor mal aparecia."""
    print("quique na borda de entrada")
    enviados: list[dict] = []
    injetado: list[tuple] = []

    class ConnFalsa:
        def enviar(self, msg): enviados.append(msg)

    cli = cliente_falso(enviados, injetado)

    conn = ConnFalsa()
    cli._aplicar(conn, None, {"t": "entrar", "de": "direita", "rel": 0.5})
    checar("entrou perto da borda direita",
           abs(injetado[-1][0] - (2560 - 1 - alv.MARGEM)) < 1)

    enviados.clear()
    for _ in range(12):  # tremores no sentido da borda por onde entrou
        cli._aplicar(conn, None, {"t": "mv", "dx": 8, "dy": 0})
    checar("tremor de volta nao devolve o controle", enviados == [], str(enviados))

    cli._aplicar(conn, None, {"t": "mv", "dx": -alv.FOLGA_DE_ENTRADA - 10,
                              "dy": 0})
    checar("afastar-se da borda libera a volta", cli.alvo.pode_voltar is True)
    cli._aplicar(conn, None, {"t": "mv", "dx": 300, "dy": 0})
    checar("depois disso, voltar devolve o controle",
           len(enviados) == 1 and enviados[0]["dir"] == "direita", str(enviados))

    # Quem atravessa e quer voltar na hora, sem andar para dentro, nao pode
    # ficar presa: passado TRAVA_DE_ENTRADA a saida libera do mesmo jeito.
    enviados.clear()
    cli.remoto = cli.aguardando = False
    cli._aplicar(conn, None, {"t": "entrar", "de": "direita", "rel": 0.5})
    cli.alvo.entrou_em -= alv.TRAVA_DE_ENTRADA + 0.05  # simula o tempo passando
    enviados.clear()
    cli._aplicar(conn, None, {"t": "mv", "dx": 20, "dy": 0})
    checar("passada a trava de tempo, volta sem precisar andar para dentro",
           len(enviados) == 1 and enviados[0]["dir"] == "direita", str(enviados))

    # a aresta oposta nunca fica travada
    enviados.clear()
    injetado.clear()
    cli2 = cliente_falso(enviados, injetado)
    cli2._aplicar(conn, None, {"t": "entrar", "de": "direita", "rel": 0.5})
    enviados.clear()
    cli2._aplicar(conn, None, {"t": "mv", "dx": -5000, "dy": 0})
    checar("sair pela aresta oposta passa direto",
           len(enviados) == 1 and enviados[0]["dir"] == "esquerda", str(enviados))


def teste_movimento_bruto() -> None:
    """Regressao: o movimento remoto nao pode sair da posicao do hook.

    O hook so' informa posicao absoluta, ja' presa aos limites da tela; derivar
    delta de uma ancora exigia que o SetCursorPos tivesse sido aplicado, e
    chamado de dentro do callback ele nao e' confiavel -- o resultado eram
    deltas de meia tela atirando o cursor de borda em borda.
    """
    print("movimento por Raw Input")
    borda.ew.mover_cursor = lambda x, y: None
    enviados: list[tuple] = []
    lo = lay.Layout([lay.PC("SRV", "2", 2, 1, True), lay.PC("esq", "1", 1, 1)])
    ctl = borda.Controle(lo, "SRV", lambda d, m: enviados.append((d, m)))
    ctl.conectados.add("esq")

    ctl.delta_bruto(10, 10)
    checar("em modo local, delta bruto e' ignorado", enviados == [], str(enviados))

    ctl.tratar({"t": "mv", "pos": (ctl.x0, 300)})  # encosta na borda esquerda
    checar("cursor entregue ao vizinho da esquerda", ctl.atual == "esq")
    enviados.clear()

    # a posicao do hook fica presa na borda; antes isto virava dx = -meia tela
    for _ in range(3):
        ctl.tratar({"t": "mv", "pos": (ctl.x0, 300)})
    checar("posicao presa na borda nao gera movimento", enviados == [],
           str(enviados))

    ctl.delta_bruto(-12, 3)
    checar("delta relativo vai como veio",
           enviados == [("esq", {"t": "mv", "dx": -12, "dy": 3})], str(enviados))

    # encostar na borda continua nao movendo, mas o delta relativo continua indo
    enviados.clear()
    ctl.delta_bruto(-8, 0)
    ctl.delta_bruto(-8, 0)
    checar("movimento continuo mesmo com o cursor local imovel",
           enviados == [("esq", {"t": "mv", "dx": -8, "dy": 0})] * 2, str(enviados))


def teste_atalho_e_troca() -> None:
    """Atalho Ctrl+Alt+N e o aviso de troca de PC."""
    print("atalho de teclado e aviso de troca")
    borda.ew.mover_cursor = lambda x, y: None
    enviados: list[tuple] = []
    trocas: list[tuple] = []
    lo = lay.Layout([lay.PC("SRV", "1", 2, 1, True), lay.PC("esq", "2", 1, 1),
                     lay.PC("dir", "3", 3, 1)])
    ctl = borda.Controle(lo, "SRV", lambda d, m: enviados.append((d, m)))
    ctl.ao_trocar = lambda de, para: trocas.append((de, para))
    ctl.conectados.update({"esq", "dir"})

    pressionadas = {ew.VK_CONTROL, ew.VK_MENU}
    borda.ew.modificador_pressionado = lambda vk: vk in pressionadas

    def tecla(vk, down=True):
        return ctl.tratar({"t": "key", "vk": vk, "sc": 0, "ext": False,
                           "down": down})

    checar("Ctrl+Alt+2 leva ao 2o PC da lista", tecla(0x32) is True)
    checar("cursor foi para 'esq'", ctl.atual == "esq", ctl.atual)
    checar("entrada pelo centro, nao por borda",
           enviados[-1][1] == {"t": "entrar", "de": "centro", "rel": 0.5},
           str(enviados[-1]))
    checar("avisou a troca", trocas[-1] == ("SRV", "esq"), str(trocas))
    checar("o digito nao vaza no keyup", tecla(0x32, down=False) is True)

    checar("Ctrl+Alt+3 pula direto de um cliente para outro", tecla(0x33) is True)
    checar("cursor foi para 'dir'", ctl.atual == "dir", ctl.atual)
    checar("o PC anterior foi avisado de que perdeu o cursor",
           ("esq", {"t": "soltar"}) in enviados)

    checar("Ctrl+Alt+1 traz de volta ao servidor",
           tecla(0x31) is True and ctl.atual == "SRV")
    checar("indice sem PC correspondente e' ignorado",
           ctl._pc_do_atalho({"t": "key", "vk": 0x39, "down": True}) is None)

    pressionadas.add(ew.VK_SHIFT)
    checar("com Shift nao e' atalho de troca (e' o de panico)",
           ctl._pc_do_atalho({"t": "key", "vk": 0x32, "down": True}) is None)
    pressionadas.clear()
    checar("sem Ctrl+Alt o digito passa normalmente",
           ctl._pc_do_atalho({"t": "key", "vk": 0x32, "down": True}) is None)


def teste_layout_do_servidor() -> None:
    """O servidor e' a fonte unica: resolve o cliente por nome ou por IP."""
    print("layout vindo do servidor")
    lo = lay.Layout([lay.PC("SRV", "192.168.0.1", 2, 1, True),
                     lay.PC("PC-esq", "192.168.0.5", 1, 1)])
    checar("casa pelo nome",
           getattr(lo.por_nome("PC-esq"), "nome", None) == "PC-esq")
    checar("nome divergente casa pelo IP",
           getattr(lo.por_ip("192.168.0.5"), "nome", None) == "PC-esq")
    checar("o proprio servidor nao casa como cliente",
           lo.por_ip("192.168.0.1", ignorar="SRV") is None)
    checar("IP desconhecido nao casa", lo.por_ip("192.168.0.99") is None)

    ambiguo = lay.Layout([lay.PC("A", "10.0.0.1", 0, 0, True),
                          lay.PC("B", "10.0.0.9", 1, 0),
                          lay.PC("C", "10.0.0.9", 2, 0)])
    checar("IP repetido nao casa (ambiguo)", ambiguo.por_ip("10.0.0.9") is None)

    x, y = lay.ponto_de_entrada("centro", 0.5, 0, 0, 1000, 500)
    checar("entrada pelo centro fica no meio da tela", (x, y) == (500.0, 250.0),
           f"({x},{y})")


def teste_multi_monitor() -> None:
    """Buracos entre monitores: a posicao virtual nao pode parar neles.

    Arranjo usado: uma tela 1920x1080 em (0,0) e outra 1280x1024 em (1920,300),
    mais baixa e deslocada para baixo. O retangulo que envolve as duas vai de
    (0,0) a (3200,1324) e tem dois buracos: acima da segunda tela e abaixo da
    primeira.
    """
    print("varios monitores por PC")
    A = (0, 0, 1920, 1080)
    B = (1920, 300, 1280, 1024)
    telas = [A, B]
    x0, y0, largura, altura = 0, 0, 3200, 1324

    checar("ponto na tela A e' visivel", lay.dentro_de_algum(500, 500, telas))
    checar("ponto na tela B e' visivel", lay.dentro_de_algum(2500, 800, telas))
    checar("buraco acima de B nao e' visivel",
           not lay.dentro_de_algum(2500, 100, telas))
    checar("buraco abaixo de A nao e' visivel",
           not lay.dentro_de_algum(500, 1200, telas))

    checar("ponto ja' visivel nao e' mexido",
           lay.ponto_visivel(500, 500, telas) == (500, 500))
    px, py = lay.ponto_visivel(2500, 100, telas)
    checar("buraco acima de B cai na borda de cima de B", (px, py) == (2500, 300),
           f"({px},{py})")
    px, py = lay.ponto_visivel(500, 1200, telas)
    checar("buraco abaixo de A cai na borda de baixo de A",
           (px, py) == (500, 1079), f"({px},{py})")
    px, py = lay.ponto_visivel(3199, 50, telas)
    checar("canto superior direito do retangulo cai no ponto real mais proximo",
           lay.dentro_de_algum(px, py, telas), f"({px},{py})")

    # entrada por borda que cairia num buraco
    px, py = lay.ponto_de_entrada("direita", 0.0, x0, y0, largura, altura, 4, telas)
    checar("entrada pela direita no topo e' puxada para dentro de B",
           lay.dentro_de_algum(px, py, telas), f"({px},{py})")
    px, py = lay.ponto_de_entrada("baixo", 0.0, x0, y0, largura, altura, 4, telas)
    checar("entrada por baixo na esquerda e' puxada para dentro de A",
           lay.dentro_de_algum(px, py, telas), f"({px},{py})")
    for aresta in ("esquerda", "direita", "cima", "baixo"):
        for rel in (0.0, 0.25, 0.5, 0.75, 1.0):
            px, py = lay.ponto_de_entrada(aresta, rel, x0, y0, largura, altura,
                                          4, telas)
            if not lay.dentro_de_algum(px, py, telas):
                checar(f"entrada {aresta}/{rel} cai em tela", False, f"({px},{py})")
                break
        else:
            continue
        break
    else:
        checar("toda entrada, em qualquer aresta e altura, cai em tela", True)

    # Centro do atalho: monitor principal, e nao o centro do retangulo. Dois
    # arranjos mostram por que o centro do retangulo e' um lugar ruim.
    cx, cy = lay.centro_principal(telas, x0, y0, largura, altura)
    checar("centro do atalho fica no monitor principal", (cx, cy) == (960.0, 540.0),
           f"({cx},{cy})")
    checar("e esta' em tela", lay.dentro_de_algum(cx, cy, telas))

    # (a) duas telas iguais lado a lado: o centro do retangulo cai exatamente na
    #     divisa, onde um movimento minimo troca de monitor
    lado_a_lado = [(0, 0, 1920, 1080), (1920, 0, 1920, 1080)]
    checar("centro do retangulo cai na divisa entre as duas telas",
           3840 / 2 == lado_a_lado[1][0])
    checar("centro do atalho fica no meio do principal, longe da divisa",
           lay.centro_principal(lado_a_lado, 0, 0, 3840, 1080) == (960.0, 540.0))

    # (b) telas em diagonal: o centro do retangulo nao existe em tela nenhuma
    diagonal = [(0, 0, 1000, 1000), (2000, 2000, 1000, 1000)]
    checar("centro do retangulo cai num buraco",
           not lay.dentro_de_algum(1500, 1500, diagonal))
    ccx, ccy = lay.centro_principal(diagonal, 0, 0, 3000, 3000)
    checar("centro do atalho continua em tela", lay.dentro_de_algum(ccx, ccy, diagonal),
           f"({ccx},{ccy})")

    # um monitor so': nada muda
    unico = [(0, 0, 2560, 1080)]
    checar("com um monitor, ponto_visivel nao mexe em nada",
           lay.ponto_visivel(1234, 567, unico) == (1234, 567))
    checar("com um monitor, o centro e' o centro da tela",
           lay.centro_principal(unico, 0, 0, 2560, 1080) == (1280.0, 540.0))
    checar("sem lista de monitores, ponto_de_entrada segue como antes",
           lay.ponto_de_entrada("esquerda", 0.5, 0, 0, 1000, 500)
           == lay.ponto_de_entrada("esquerda", 0.5, 0, 0, 1000, 500, 4, None))


def teste_comando_do_cliente() -> None:
    """O cliente com teclado e mouse proprios pedindo e exercendo o comando."""
    print("comando vindo do cliente (lado cliente)")
    enviados: list[dict] = []
    injetado: list[tuple] = []
    cli = cliente_falso(enviados, injetado)
    cmd = cli.comando

    # 'esq' esta' na coluna 1 e SRV na 2: o vizinho da direita e' o servidor.
    checar("mouse no meio nao pede nada",
           cmd.tratar({"t": "mv", "pos": (600, 500)}) is False and enviados == [])
    engoliu = cmd.tratar({"t": "mv", "pos": (2559, 540)})
    checar("borda direita pede o comando",
           enviados and enviados[-1]["t"] == "assumir"
           and enviados[-1]["dir"] == "direita", str(enviados))
    checar("mas nao engole o evento antes de o servidor conceder",
           engoliu is False)

    enviados.clear()
    cmd.tratar({"t": "mv", "pos": (2559, 545)})
    checar("nao repete o pedido a cada tremida", enviados == [], str(enviados))

    # Sem o comando concedido, o teclado local continua sendo do proprio PC.
    tecla = {"t": "key", "vk": 65, "sc": 30, "ext": False, "down": True}
    checar("tecla local passa direto enquanto nao comandamos",
           cmd.tratar(tecla) is False)

    cmd.responder(True)
    checar("comando concedido", cmd.comandando is True)
    checar("cursor tirado da borda", injetado[-1] == (1280, 540), str(injetado))
    enviados.clear()
    checar("agora a tecla e' engolida", cmd.tratar(tecla) is True)
    checar("e vai para o servidor",
           enviados[-1] == {"t": "in", "ev": tecla}, str(enviados))
    enviados.clear()
    cmd.delta_bruto(13, -7)
    checar("movimento vai pelo Raw Input",
           enviados[-1] == {"t": "in", "ev": {"t": "mv", "dx": 13, "dy": -7}},
           str(enviados))
    checar("a posicao do hook e' engolida sem virar movimento",
           cmd.tratar({"t": "mv", "pos": (10, 10)}) is True)

    # Panico com o comando na mao: solta o bloqueio e avisa o servidor.
    enviados.clear()
    cliente.ew.modificador_pressionado = lambda vk: True
    cmd.tratar({"t": "key", "vk": ew.VK_ESCAPE, "sc": 1, "ext": False,
                "down": True})
    checar("panico larga o comando", cmd.comandando is False)
    checar("e avisa o servidor",
           {"t": "largar"} in enviados, str(enviados))
    cliente.ew.modificador_pressionado = ew.modificador_pressionado

    # O cursor voltando para ca': para de comandar sem pedir nada.
    cmd.comandando = True
    injetado.clear()
    cmd.devolver("direita", 0.5)
    checar("'devolver' encerra o comando", cmd.comandando is False)
    checar("e recoloca o cursor na borda direita",
           abs(injetado[-1][0] - (2560 - 1 - alv.MARGEM)) < 1, str(injetado))

    # Sendo comandado de fora, o mouse local fica solto e nao rouba o cursor.
    cli.remoto = True
    enviados.clear()
    cmd._liberado_em = 0.0
    cmd._pedido_em = 0.0
    checar("enquanto e' comandado, a borda local nao pede o comando",
           cmd.tratar({"t": "mv", "pos": (2559, 540)}) is False
           and enviados == [], str(enviados))


def teste_comando_no_servidor() -> None:
    """O servidor virando alvo do teclado de um cliente."""
    print("comando vindo do cliente (lado servidor)")
    x0, y0, largura, altura = ew.geometria_virtual()
    movidos: list = []
    borda.ew.mover_cursor = lambda x, y: movidos.append((x, y))
    enviados: list[tuple[str, dict]] = []
    ctl = borda.Controle(_layout_de_teste(), "B",
                         lambda destino, msg: enviados.append((destino, msg)))
    ctl.conectados.update({"A", "C", "D"})

    checar("no comeco quem comanda e' o servidor", ctl.comandante == "B")

    # A esta' a' esquerda de B: saindo pela direita, A pede o comando de B.
    ctl.pedir_comando("A", "direita", 0.5)
    checar("o comando passou para A", ctl.comandante == "A")
    checar("A foi avisado de que pode comandar",
           ("A", {"t": "comando", "ok": True}) in enviados, str(enviados))
    checar("o cursor ficou no servidor", ctl.atual == "B")
    checar("e o servidor virou alvo", ctl.sou_o_alvo is True)
    entrada = lay.ponto_de_entrada("esquerda", 0.5, x0, y0, largura, altura,
                                   alv.MARGEM)
    checar("cursor posto na borda esquerda do servidor",
           enviados[-1][0] == borda.LOCAL
           and abs(enviados[-1][1]["x"] - entrada[0]) < 1, str(enviados[-1]))

    # Input de A: o servidor injeta em si mesmo, nao manda para a rede.
    enviados.clear()
    ctl.do_comandante("A", {"t": "mv", "dx": 40, "dy": 0})
    checar("movimento de quem comanda vira injecao local",
           enviados[-1][0] == borda.LOCAL and enviados[-1][1]["t"] == "por_cursor",
           str(enviados[-1]))
    enviados.clear()
    tecla = {"t": "key", "vk": 66, "sc": 48, "ext": False, "down": True}
    ctl.do_comandante("A", tecla)
    checar("tecla de quem comanda tambem",
           enviados[-1] == (borda.LOCAL, {"t": "injetar", "ev": tecla}),
           str(enviados[-1]))
    enviados.clear()
    ctl.do_comandante("C", {"t": "mv", "dx": 500, "dy": 0})
    checar("input de quem NAO comanda e' ignorado", enviados == [], str(enviados))

    # Empurrando para a direita: C e' vizinho de B daquele lado.
    enviados.clear()
    ctl.do_comandante("A", {"t": "mv", "dx": largura + 500, "dy": 0})
    checar("o cursor atravessa para C", ctl.atual == "C")
    checar("C recebeu 'entrar', nao 'devolver'",
           any(d == "C" and m.get("t") == "entrar" for d, m in enviados),
           str(enviados))
    checar("A continua comandando", ctl.comandante == "A")
    enviados.clear()
    ctl.do_comandante("A", {"t": "mv", "dx": 5, "dy": 0})
    checar("agora o movimento e' repassado a C",
           enviados[-1] == ("C", {"t": "mv", "dx": 5, "dy": 0}), str(enviados))

    # De C para a esquerda -> B; de B para a esquerda -> A, que e' quem comanda.
    enviados.clear()
    ctl.saiu_do_cliente("C", "esquerda", 0.5)
    checar("voltou ao servidor como alvo",
           ctl.atual == "B" and ctl.sou_o_alvo is True)
    enviados.clear()
    ctl.do_comandante("A", {"t": "mv", "dx": -(largura + 500), "dy": 0})
    checar("o cursor volta para quem comanda", ctl.atual == "A")
    checar("e A recebe 'devolver', nao 'entrar'",
           any(d == "A" and m.get("t") == "devolver" for d, m in enviados),
           str(enviados))

    # Mexer no teclado/mouse do servidor traz o comando de volta.
    enviados.clear()
    checar("input fisico daqui nao e' engolido",
           ctl.tratar({"t": "mv", "pos": (600, 500)}) is False)
    checar("e o comando volta para o servidor", ctl.comandante == "B")
    checar("A foi avisado de que perdeu o comando",
           any(d == "A" and m.get("t") == "comando" and m.get("ok") is False
               for d, m in enviados), str(enviados))

    # Queda de quem comanda nao pode deixar o servidor esperando para sempre.
    ctl.pedir_comando("A", "direita", 0.5)
    checar("A comanda de novo", ctl.comandante == "A")
    ctl.cliente_caiu("A")
    checar("queda de quem comanda devolve o comando", ctl.comandante == "B")


def teste_inicio_automatico() -> None:
    """O inicio automatico tem de sobreviver ao boot e a tela de bloqueio.

    Dois enganos custaram isso antes, e os dois eram silenciosos:

      * a entrada de `HKCU\\...\\Run` chamava o executavel SEM `--sem-janela`:
        no melhor caso o logon abriria a janela de configuracao e pararia ali;
      * e nem chegava a abrir, porque o .exe pede elevacao e o Windows descarta
        entrada de Run que pede UAC -- nao ha' como mostrar o prompt no logon.

    O caminho de verdade e' o servico. Aqui checamos o que da' para checar sem
    reiniciar a maquina: as linhas de comando, a leitura do desktop de entrada
    e o bilhete que o agente deixa para o servico.
    """
    print("inicio automatico (servico e desktop de entrada)")
    import os
    import tempfile

    import servico as svc
    import sessao_win as sw

    checar("o comando do registro sobe sem janela",
           "--sem-janela" in conf._comando_de_inicio(),
           conf._comando_de_inicio())

    _exe, comando = svc._binario()
    checar("o supervisor e' chamado com --servico", "--servico" in comando)
    _exe, agente = svc.linha_do_agente("Winlogon")
    checar("o agente recebe o desktop pedido",
           "--agente" in agente and '--desktop "Winlogon"' in agente, agente)

    # A tarefa agendada. Cada ajuste aqui ja' foi um jeito de nao funcionar:
    # sem BootTrigger nao sobe antes do login; sem SYSTEM nao alcanca o desktop
    # seguro; com o ExecutionTimeLimit padrao o Windows mata tudo em 3 dias.
    import xml.etree.ElementTree as ET
    NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    arvore = ET.fromstring(svc.xml_da_tarefa().replace('encoding="UTF-16"',
                                                       'encoding="UTF-8"'))

    def campo(caminho: str) -> str:
        achado = arvore.find(f".//t:{caminho}", NS)
        return "" if achado is None else (achado.text or "")

    checar("a tarefa dispara no boot",
           arvore.find(".//t:BootTrigger", NS) is not None)
    checar("como SYSTEM, pelo SID (o nome da conta e' traduzido)",
           campo("UserId") == "S-1-5-18", campo("UserId"))
    checar("com privilegio maximo",
           campo("RunLevel") == "HighestAvailable")
    checar("sem limite de tempo de execucao",
           campo("ExecutionTimeLimit") == "PT0S", campo("ExecutionTimeLimit"))
    checar("e roda na bateria tambem",
           campo("DisallowStartIfOnBatteries") == "false")
    checar("nao empilha um segundo supervisor",
           campo("MultipleInstancesPolicy") == "IgnoreNew")
    checar("a acao e' o executavel com --servico",
           campo("Command") and "--servico" in campo("Arguments"),
           f"{campo('Command')} {campo('Arguments')}")

    # Ler o desktop de entrada e' o que decide relancar ou nao. Se voltasse um
    # nome diferente do desktop do proprio processo, o agente sairia em laco.
    entrada = sw.desktop_de_entrada()
    meu = sw.meu_desktop()
    checar("da' para ler o desktop de entrada", bool(entrada), str(entrada))
    checar("e ele bate com o desktop deste processo",
           bool(entrada) and bool(meu) and entrada.lower() == meu.lower(),
           f"{entrada} / {meu}")
    checar("a sessao do console nao e' a sessao 0",
           sw.sessao_do_console() not in (None, 0), str(sw.sessao_do_console()))
    checar("o codigo de troca de desktop nao colide com erro comum",
           sw.SAIDA_TROCOU_DESKTOP not in (0, 1, 2))

    # O bilhete: o servico nao consegue ler o desktop de entrada da sessao 0,
    # entao quem o le' e' o agente, que deixa o nome por escrito antes de sair.
    with tempfile.TemporaryDirectory() as tmp:
        salvo = conf._pasta_de_saida
        conf._pasta_de_saida = pathlib.Path(tmp)
        try:
            checar("sem bilhete, o servico usa o desktop padrao",
                   svc._ler_desktop() == svc.DESKTOP_PADRAO)
            svc._gravar_desktop("Winlogon")
            checar("com bilhete, ele nasce no desktop pedido",
                   svc._ler_desktop() == "Winlogon")
            svc._gravar_desktop("")
            checar("bilhete vazio nao vira desktop invalido",
                   svc._ler_desktop() == svc.DESKTOP_PADRAO)
        finally:
            conf._pasta_de_saida = salvo

    # A config tem de ir para o lado do .exe: o servico roda como SYSTEM e o
    # %APPDATA% dele nao e' o do usuario -- de la' o motor subiria sem chave.
    with tempfile.TemporaryDirectory() as tmp:
        salvo = conf.pasta_do_executavel
        conf.pasta_do_executavel = lambda: pathlib.Path(tmp)
        try:
            destino = conf.gravar_ao_lado_do_executavel(
                {"chave": "abc", "porta": 24810, "capturar": True})
            texto = destino.read_text(encoding="utf-8")
            checar("a config vai para a pasta do executavel",
                   destino == pathlib.Path(tmp) / "config.json")
            checar("com a chave que o servico precisa", '"chave"' in texto)
            checar("e sem o 'capturar', que e' so' da linha de comando",
                   "capturar" not in texto)
            checar("o caminho_config passa a preferir esse arquivo",
                   conf.caminho_config() == destino)
        finally:
            conf.pasta_do_executavel = salvo


def main() -> int:
    ew.ativar_dpi()
    x0, y0, largura, altura = ew.geometria_virtual()
    print(f"desktop virtual: {largura}x{altura} em ({x0},{y0})\n")
    teste_clipboard()
    teste_layout()
    teste_injecao()
    teste_shift_direito()
    teste_espera_do_sair()
    teste_renomeacao_e_tema()
    teste_transferencia_de_arquivos()
    teste_regra_de_firewall()
    teste_vigia_do_teclado()
    teste_raw_input_apos_religar()
    teste_hooks()
    teste_roteamento()
    teste_watchdog_devolve_comando()
    teste_quique()
    teste_movimento_bruto()
    teste_atalho_e_troca()
    teste_layout_do_servidor()
    teste_multi_monitor()
    teste_comando_do_cliente()
    teste_comando_no_servidor()
    teste_inicio_automatico()
    print()
    if falhas:
        print(f"{len(falhas)} FALHA(S): {', '.join(falhas)}")
        return 1
    print("tudo OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
