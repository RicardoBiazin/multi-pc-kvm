"""Aviso de qual PC esta' com o controle: tarja na tela e bip.

Existe porque a duvida "onde foi o meu cursor?" e' o que mais atrapalha no uso.
A tarja aparece no alto da tela por um instante, sem roubar o foco, e funciona
mesmo com a janela do programa fechada na bandeja.
"""

from __future__ import annotations

import logging
import tkinter as tk

log = logging.getLogger("aviso")

DURACAO = 1200  # ms que a tarja fica na tela
FONTE = ("Segoe UI", 15, "bold")
COR_SAIU = ("#2f6b3f", "#ffffff")     # controle foi para outro PC
COR_VOLTOU = ("#c9752b", "#ffffff")   # controle voltou para este PC

TOM_SAIU = (880, 60)    # (Hz, ms)
TOM_VOLTOU = (554, 60)


def bipar(subindo: bool) -> None:
    """Bip curto. Tom alto ao entregar o controle, baixo ao receber de volta."""
    try:
        import winsound
        frequencia, duracao = TOM_SAIU if subindo else TOM_VOLTOU
        winsound.Beep(frequencia, duracao)
    except Exception:
        pass  # maquina sem alto-falante ou sem winsound: o aviso visual basta


class Tarja:
    """Janela sem borda que aparece no alto da tela e desaparece sozinha.

    Precisa ser criada e usada na thread do tkinter; quem chama de fora usa
    `Janela._na_interface`.
    """

    def __init__(self, raiz: tk.Misc):
        self.raiz = raiz
        self.janela: tk.Toplevel | None = None
        self._esconder_em: str | None = None

    def _criar(self) -> tk.Toplevel:
        janela = tk.Toplevel(self.raiz)
        janela.overrideredirect(True)          # sem barra de titulo
        janela.attributes("-topmost", True)
        janela.attributes("-alpha", 0.92)
        # Nao pode roubar o foco: o usuario esta' digitando no outro PC.
        janela.attributes("-disabled", True)
        self.rotulo = tk.Label(janela, text="", font=FONTE, padx=26, pady=10)
        self.rotulo.pack()
        return janela

    def mostrar(self, texto: str, voltou: bool) -> None:
        try:
            if self.janela is None or not self.janela.winfo_exists():
                self.janela = self._criar()
            fundo, frente = COR_VOLTOU if voltou else COR_SAIU
            self.rotulo.configure(text=texto, bg=fundo, fg=frente)
            self.janela.configure(bg=fundo)
            self.janela.update_idletasks()
            largura = self.janela.winfo_reqwidth()
            tela = self.janela.winfo_screenwidth()
            self.janela.geometry(f"+{(tela - largura) // 2}+60")
            self.janela.deiconify()
            self.janela.lift()
            if self._esconder_em is not None:
                self.raiz.after_cancel(self._esconder_em)
            self._esconder_em = self.raiz.after(DURACAO, self.esconder)
        except tk.TclError:
            self.janela = None  # janela principal fechando

    def esconder(self) -> None:
        self._esconder_em = None
        try:
            if self.janela is not None and self.janela.winfo_exists():
                self.janela.withdraw()
        except tk.TclError:
            self.janela = None
