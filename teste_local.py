"""Auto-teste que roda num PC so': o que da' para verificar sem os outros PCs.

    python teste_local.py

Cobre: ida e volta de imagem no clipboard, vizinhanca no layout, instalacao dos
hooks, injecao de mouse e o roteamento do cursor entre varios PCs. Nao mexe no
mouse de verdade na parte do roteamento (`mover_cursor` e' substituido); a parte
de injecao move o cursor e o devolve ao lugar.
"""

from __future__ import annotations

import base64
import io
import sys
import time

from PIL import Image

import borda
import cliente
import clipboard_win as cw
import entrada_win as ew
import layout as lay

falhas: list[str] = []


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


def teste_quique() -> None:
    """Regressao: o cursor entra a MARGEM px da borda e um tremor de mao no
    sentido de volta devolvia o controle na hora -- o cursor mal aparecia."""
    print("quique na borda de entrada")
    enviados: list[dict] = []
    injetado: list[tuple] = []

    class ConnFalsa:
        def enviar(self, msg): enviados.append(msg)

    cli = cliente.Cliente.__new__(cliente.Cliente)
    cli.x0, cli.y0, cli.largura, cli.altura = 0, 0, 2560, 1080
    cli.monitores = [(0, 0, 2560, 1080)]
    cli.injetor = type("I", (), {
        "mover_para": lambda s, x, y: injetado.append((x, y)),
        "soltar_modificadores": lambda s: None})()
    cli.remoto = cli.aguardando = False
    cli.aresta_de_entrada = ""
    cli.pode_voltar = True
    cli.entrou_em = 0.0
    cli.vx = cli.vy = 0.0
    cli.ao_mudar = lambda: None
    cli.ao_trocar = lambda de, para: None
    cli.eu = "esq"
    cli.layout = lay.Layout([lay.PC("SRV", "1", 2, 1, True),
                             lay.PC("esq", "2", 1, 1)])

    conn = ConnFalsa()
    cli._aplicar(conn, None, {"t": "entrar", "de": "direita", "rel": 0.5})
    checar("entrou perto da borda direita",
           abs(injetado[-1][0] - (2560 - 1 - cliente.MARGEM)) < 1)

    enviados.clear()
    for _ in range(12):  # tremores no sentido da borda por onde entrou
        cli._aplicar(conn, None, {"t": "mv", "dx": 8, "dy": 0})
    checar("tremor de volta nao devolve o controle", enviados == [], str(enviados))

    cli._aplicar(conn, None, {"t": "mv", "dx": -cliente.FOLGA_DE_ENTRADA - 10,
                              "dy": 0})
    checar("afastar-se da borda libera a volta", cli.pode_voltar is True)
    cli._aplicar(conn, None, {"t": "mv", "dx": 300, "dy": 0})
    checar("depois disso, voltar devolve o controle",
           len(enviados) == 1 and enviados[0]["dir"] == "direita", str(enviados))

    # Quem atravessa e quer voltar na hora, sem andar para dentro, nao pode
    # ficar presa: passado TRAVA_DE_ENTRADA a saida libera do mesmo jeito.
    enviados.clear()
    cli.remoto = cli.aguardando = False
    cli._aplicar(conn, None, {"t": "entrar", "de": "direita", "rel": 0.5})
    cli.entrou_em -= cliente.TRAVA_DE_ENTRADA + 0.05  # simula o tempo passando
    enviados.clear()
    cli._aplicar(conn, None, {"t": "mv", "dx": 20, "dy": 0})
    checar("passada a trava de tempo, volta sem precisar andar para dentro",
           len(enviados) == 1 and enviados[0]["dir"] == "direita", str(enviados))

    # a aresta oposta nunca fica travada
    enviados.clear()
    injetado.clear()
    cli2 = cliente.Cliente.__new__(cliente.Cliente)
    cli2.__dict__.update(cli.__dict__)
    cli2.remoto = cli2.aguardando = False
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


def main() -> int:
    ew.ativar_dpi()
    x0, y0, largura, altura = ew.geometria_virtual()
    print(f"desktop virtual: {largura}x{altura} em ({x0},{y0})\n")
    teste_clipboard()
    teste_layout()
    teste_injecao()
    teste_hooks()
    teste_roteamento()
    teste_quique()
    teste_movimento_bruto()
    teste_atalho_e_troca()
    teste_layout_do_servidor()
    teste_multi_monitor()
    print()
    if falhas:
        print(f"{len(falhas)} FALHA(S): {', '.join(falhas)}")
        return 1
    print("tudo OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
