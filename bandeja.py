"""Icone na bandeja do sistema. Opcional: se o pystray faltar, a janela apenas
continua se fechando normalmente."""

from __future__ import annotations

import logging
import threading

log = logging.getLogger("bandeja")

VERDE = (47, 107, 63)       # no ar e conectado
CINZA = (110, 116, 126)      # parado
AMARELO = (196, 146, 24)     # no ar, mas sem ninguem do outro lado
VERMELHO = (178, 54, 54)     # no ar com erro (recusado, sem servidor, injecao)


def _imagem(cor) -> "object":
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((4, 16, 60, 48), radius=7, fill=cor)
    for coluna in range(5):  # teclas
        for linha in range(2):
            x = 11 + coluna * 9
            y = 23 + linha * 10
            d.rectangle((x, y, x + 6, y + 6), fill=(255, 255, 255, 210))
    return img


# O Windows guarda a dica do icone num campo de 128 caracteres (szTip, com o
# terminador). Passar disso faz o pystray levantar ValueError ao redesenhar o
# icone -- visto no log do PC da esquerda: "string too long (130, maximum
# length 128)", quando o Explorer reiniciou. O icone some da bandeja.
LIMITE_DA_DICA = 127


def cor_do_estado(motor) -> tuple:
    """Verde conectado, amarelo esperando, vermelho com erro, cinza parado."""
    try:
        if not motor.ativo() or motor.papel is None:
            return CINZA
        s = motor.papel.situacao()
        if s.get("erro"):
            return VERMELHO
        return VERDE if s.get("conectados") else AMARELO
    except Exception:
        return CINZA


def _aviso_de_conexao(antes, depois) -> str:
    """Texto do balao quando a conexao muda; "" quando nao ha' o que dizer.

    So' o que importa ao usuario: conectou, ou estava conectado e deixou de estar.
    Parar e abrir o programa (cinza) nao e' queda.
    """
    if antes is None:
        return ""  # primeira leitura: nao e' uma mudanca
    if depois == VERDE:
        return "Conectado"
    if antes == VERDE and depois in (AMARELO, VERMELHO):
        return "Conexao perdida"
    return ""


def _avisos_ligados(motor) -> bool:
    papel = getattr(motor, "papel", None)
    politica = getattr(papel, "politica", None)
    return True if politica is None else bool(politica.avisos_na_bandeja)


def _troca_ligada(motor) -> bool:
    papel = getattr(motor, "papel", None)
    if papel is not None and getattr(papel, "politica", None) is not None:
        return papel.politica.troca_ligada
    return bool((getattr(motor, "cfg", None) or {}).get("opcoes", {})
                .get("troca_ligada", True))


def _alternar_troca(motor) -> None:
    """Liga/desliga a troca pela borda. Grava no config: o agente (outro
    processo) e a janela leem de la'."""
    import configuracao as conf
    novo = not _troca_ligada(motor)
    motor.opcoes({"troca_ligada": novo})
    try:
        conf.gravar_chave("opcoes", dict(motor.cfg.get("opcoes") or {}))
    except OSError:
        log.warning("nao consegui gravar as opcoes no config.json", exc_info=True)


def dica_do_icone(motor) -> str:
    """Dica do icone: versao e estado, cortada no limite do Windows."""
    import configuracao as conf
    texto = f"{conf.APP} v{conf.VERSAO} -- {motor.resumo()}"
    if len(texto) > LIMITE_DA_DICA:
        texto = texto[:LIMITE_DA_DICA - 3] + "..."
    return texto


def _alternar_audio(motor, nome: str) -> None:
    """Liga/desliga o som de `nome`. Grava no config: o agente (outro processo)
    e a janela leem de la'."""
    import configuracao as conf
    atuais = set(motor.cfg.get("ouvir") or [])
    atuais ^= {nome}
    motor.ouvir(sorted(atuais))
    try:
        conf.gravar_chave("ouvir", sorted(atuais))
    except OSError:
        log.warning("nao consegui gravar o que ouvir no config.json", exc_info=True)


def _itens_de_audio(pystray, motor) -> list:
    """Um item marcavel por PC (menos este), e 'Nenhum'. Refeito a cada abertura."""
    cfg = getattr(motor, "cfg", None) or {}
    eu = cfg.get("este_pc", "")
    outros = [p["nome"] for p in cfg.get("pcs", []) if p["nome"] != eu]
    if not outros:
        return [pystray.MenuItem("(nao ha' outros PCs)", None, enabled=False)]
    itens = [pystray.MenuItem(
        nome, lambda _i, n=nome: _alternar_audio(motor, n),
        checked=lambda _i, n=nome: n in (motor.cfg.get("ouvir") or []))
        for nome in outros]
    if len(outros) > 1:
        itens.append(pystray.Menu.SEPARATOR)
        itens.append(pystray.MenuItem("Todos", lambda _i: _ouvir_varios(motor, outros)))
    itens.append(pystray.MenuItem("Nenhum", lambda _i: _ouvir_varios(motor, [])))
    return itens


def _ouvir_varios(motor, nomes) -> None:
    import configuracao as conf
    motor.ouvir(list(nomes))
    try:
        conf.gravar_chave("ouvir", list(nomes))
    except OSError:
        log.warning("nao consegui gravar o que ouvir no config.json", exc_info=True)


def criar(ao_abrir, ao_sair, motor, acoes: bool = True, titulo: str = "",
          rotulo_abrir: str = "Abrir", rotulo_sair: str = "Sair"):
    """Devolve o icone (ja' rodando) ou None se o pystray nao estiver presente.

    Cada item so' aparece se houver o que chamar: passar `ao_sair=None` com
    `acoes=True` montava um "Sair" que executava `None()` e estourava calado.
    Foi o que fez o menu do agente ter um Sair que nao podia funcionar.

    `titulo` distingue os icones. Com o inicio automatico ligado ha' DOIS na
    bandeja -- o do agente e o da janela --, e ate' aqui os dois eram iguais:
    nao havia como saber em qual se estava clicando.
    """
    try:
        import pystray
    except ImportError:
        log.info("pystray nao instalado; bandeja indisponivel")
        return None

    import configuracao as conf
    legenda = f"{conf.APP} v{conf.VERSAO}"
    if titulo:
        legenda += f" -- {titulo}"
    icone = pystray.Icon(conf.APP, _imagem(CINZA),
                         f"{legenda}\npor {conf.AUTOR}")
    estado = pystray.MenuItem(lambda _i: motor.resumo(), None, enabled=False)
    itens = []
    if acoes and ao_abrir is not None:
        itens.append(pystray.MenuItem(rotulo_abrir, lambda: ao_abrir(),
                                      default=True))
    itens.append(estado)
    if acoes:
        itens.append(pystray.MenuItem(
            "Troca pela borda (Ctrl+Alt+M)", lambda _i: _alternar_troca(motor),
            checked=lambda _i: _troca_ligada(motor)))
        itens.append(pystray.MenuItem(
            "Atualizar conexoes (Ctrl+Alt+R)",
            lambda _i: motor.atualizar_conexoes()))
        itens.append(pystray.MenuItem("Ouvir o audio de", pystray.Menu(
            lambda: _itens_de_audio(pystray, motor))))
    if acoes and ao_sair is not None:
        itens.append(pystray.Menu.SEPARATOR)
        itens.append(pystray.MenuItem(rotulo_sair, lambda: ao_sair()))
    icone.menu = pystray.Menu(*itens)

    def avisar(texto: str) -> None:
        """Balao de aviso do Windows. Nunca pode derrubar quem avisa."""
        try:
            icone.notify(texto, conf.APP)
        except Exception:
            log.debug("nao consegui mostrar o aviso na bandeja", exc_info=True)

    if hasattr(motor, "aviso"):
        motor.aviso = avisar

    def atualizar() -> None:
        pausa = threading.Event()  # thread daemon: morre com o processo
        anterior = None
        while not pausa.wait(2):
            cor = cor_do_estado(motor)
            if cor != anterior:
                aviso_da_conexao = _aviso_de_conexao(anterior, cor)
                anterior = cor
                icone.icon = _imagem(cor)
                if aviso_da_conexao and _avisos_ligados(motor):
                    avisar(aviso_da_conexao)
            icone.title = dica_do_icone(motor)

    threading.Thread(target=icone.run, name="bandeja", daemon=True).start()
    threading.Thread(target=atualizar, name="bandeja-estado", daemon=True).start()
    return icone
