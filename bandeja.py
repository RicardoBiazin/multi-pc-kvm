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


def criar(ao_abrir, ao_sair, motor):
    """Devolve o icone (ja' rodando) ou None se o pystray nao estiver presente."""
    try:
        import pystray
    except ImportError:
        log.info("pystray nao instalado; bandeja indisponivel")
        return None

    icone = pystray.Icon("2pc_1Kit", _imagem(CINZA), "2pc_1Kit")
    icone.menu = pystray.Menu(
        pystray.MenuItem("Abrir", lambda: ao_abrir(), default=True),
        pystray.MenuItem(lambda _i: motor.resumo(), None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Sair", lambda: ao_sair()),
    )

    def atualizar() -> None:
        pausa = threading.Event()  # thread daemon: morre com o processo
        anterior = None
        while not pausa.wait(2):
            ativo = motor.ativo()
            if ativo != anterior:
                anterior = ativo
                icone.icon = _imagem(VERDE if ativo else CINZA)
            icone.title = f"2pc_1Kit -- {motor.resumo()}"

    threading.Thread(target=icone.run, name="bandeja", daemon=True).start()
    threading.Thread(target=atualizar, name="bandeja-estado", daemon=True).start()
    return icone
