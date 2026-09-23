"""Icone na bandeja do sistema. Opcional: se o pystray faltar, a janela apenas
continua se fechando normalmente."""

from __future__ import annotations

import logging
import threading

log = logging.getLogger("bandeja")

VERDE = (47, 107, 63)
CINZA = (110, 116, 126)


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
    if acoes and ao_sair is not None:
        itens.append(pystray.Menu.SEPARATOR)
        itens.append(pystray.MenuItem(rotulo_sair, lambda: ao_sair()))
    icone.menu = pystray.Menu(*itens)

    def atualizar() -> None:
        pausa = threading.Event()  # thread daemon: morre com o processo
        anterior = None
        while not pausa.wait(2):
            ativo = motor.ativo()
            if ativo != anterior:
                anterior = ativo
                icone.icon = _imagem(VERDE if ativo else CINZA)
            icone.title = f"{conf.APP} v{conf.VERSAO} -- {motor.resumo()}"

    threading.Thread(target=icone.run, name="bandeja", daemon=True).start()
    threading.Thread(target=atualizar, name="bandeja-estado", daemon=True).start()
    return icone
