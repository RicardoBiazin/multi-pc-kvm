"""Paletas clara e escura da janela, e a leitura do tema do Windows.

Fica separado da `interface` porque nao e' assunto de layout: e' uma tabela de
cores mais uma pergunta ao sistema. Assim tambem da' para conferir o contraste
das duas paletas em teste, sem abrir janela nenhuma.

A janela nao guarda cor literal em lugar nenhum: pede por NOME (`fundo`,
`celula`, ...) e o nome resolve na paleta em vigor. E' o que permite trocar de
tema com a janela ja' montada -- ver `Janela._pintar`.
"""

from __future__ import annotations

SISTEMA, CLARO, ESCURO = "sistema", "claro", "escuro"

PALETAS: dict[str, dict[str, str]] = {
    CLARO: {
        "fundo": "#f4f5f7",
        "celula": "#e2e5ea",
        "celula_borda": "#cfd3da",
        "pc": "#4a7fd4",
        "pc_servidor": "#2f6b3f",
        "pc_eu": "#c9752b",
        "pc_borda": "#20242b",
        "texto_no_pc": "#ffffff",
        "rodape": "#8b9099",
        "aviso_fundo": "#fde7c8",
        "aviso_texto": "#7a4a10",
        "log_fundo": "#1e1f22",
        "log_texto": "#d6d8dc",
    },
    ESCURO: {
        "fundo": "#1b1d21",
        "celula": "#2a2d33",
        "celula_borda": "#3a3f47",
        # Os tres tons de PC ficam mais claros que no tema claro: sobre fundo
        # escuro, a mesma cor perde contraste com o texto branco de dentro.
        "pc": "#5b8fe0",
        "pc_servidor": "#3f8b56",
        "pc_eu": "#d98b3f",
        "pc_borda": "#0e1013",
        "texto_no_pc": "#ffffff",
        "rodape": "#7d838c",
        "aviso_fundo": "#4a3a1d",
        "aviso_texto": "#f0d9a8",
        # O registro ja' era escuro no tema claro; aqui ele so' acompanha o
        # fundo geral para nao virar o unico retangulo destoante da janela.
        "log_fundo": "#141619",
        "log_texto": "#d6d8dc",
    },
}


def do_windows() -> str:
    """CLARO ou ESCURO, conforme a preferencia de aplicativos do Windows.

    `AppsUseLightTheme` = 0 significa escuro. A chave nao existe em instalacoes
    antigas; nesse caso o padrao do Windows sempre foi claro.
    """
    try:
        import winreg
        chave = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
        with chave:
            valor, _ = winreg.QueryValueEx(chave, "AppsUseLightTheme")
        return CLARO if valor else ESCURO
    except OSError:
        return CLARO


def resolver(preferencia: str) -> str:
    """Traduz o que esta' no config para a paleta a usar de fato."""
    if preferencia in (CLARO, ESCURO):
        return preferencia
    return do_windows()


def cores(preferencia: str) -> dict[str, str]:
    return PALETAS[resolver(preferencia)]
