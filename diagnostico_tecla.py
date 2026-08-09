"""Isola o caminho de uma tecla: o que o hook CAPTURA e o que a injecao PRODUZ.

Existe porque `teste_injecao.py` so' exercita o Shift ESQUERDO (scancode 0x2A):
o direito (0x36) nunca passou por teste nenhum, nem na captura nem na injecao.
Quando uma tecla "nao funciona no outro PC", o defeito esta' em um dos dois
lados, e olhar o codigo nao distingue qual -- os dois parecem certos.

    python diagnostico_tecla.py captura    # no PC do teclado
    python diagnostico_tecla.py injecao    # no PC que recebe

A parte de INJECAO nao precisa de editor de texto aberto: em vez de olhar o que
foi digitado, ela pergunta ao proprio Windows, com GetAsyncKeyState, qual tecla
ele entendeu. Se o Shift direito for injetado e o Windows registrar o ESQUERDO
como pressionado (ou nenhum), o defeito e' da injecao. Se registrar o direito,
a injecao esta' boa e o problema esta' na captura ou no transporte.
"""

from __future__ import annotations

import ctypes
import sys
import time

import entrada_win as ew

user32 = ctypes.WinDLL("user32", use_last_error=True)

# (nome, vk, scancode, estendida) -- os modificadores laterais mais o Esc, que
# serve de referencia por ser uma tecla sem lado.
TECLAS = (
    ("Shift esquerdo", 0xA0, 0x2A, False),
    ("Shift direito ", 0xA1, 0x36, False),
    ("Ctrl esquerdo ", 0xA2, 0x1D, False),
    ("Ctrl direito  ", 0xA3, 0x1D, True),
    ("Alt esquerdo  ", 0xA4, 0x38, False),
    ("Alt direito   ", 0xA5, 0x38, True),
)


def captura() -> None:
    """Mostra vk/scancode/estendida de cada tecla fisica, sem engolir nada."""
    print("Aperte e solte as teclas -- comece pelos DOIS Shifts.")
    print("Ctrl+C encerra.\n")
    print(f"{'evento':7} {'vk':>6} {'scan':>6} {'ext':>5}  interpretacao")
    print("-" * 60)

    conhecidas = {(vk, sc): nome.strip() for nome, vk, sc, _ in TECLAS}

    def ao_evento(ev: dict) -> bool:
        if ev["t"] != "key":
            return False  # nao e' tecla: deixa passar sem imprimir
        nome = conhecidas.get((ev["vk"], ev["sc"]), "")
        marca = "" if nome else "  <- nao esta' na tabela de modificadores"
        print(f"{'DOWN' if ev['down'] else 'UP':7} "
              f"{hex(ev['vk']):>6} {hex(ev['sc']):>6} "
              f"{str(ev['ext']):>5}  {nome}{marca}")
        return False  # NUNCA engolir: este script nao pode travar o teclado

    cap = ew.Captura(ao_evento, lambda dx, dy: None)
    cap.start()
    try:
        while True:
            time.sleep(0.2)
    except KeyboardInterrupt:
        print("\nencerrando...")
    finally:
        cap.parar()


def injecao() -> None:
    """Injeta cada modificador e pergunta ao Windows qual tecla ele entendeu."""
    print("Injetando cada modificador por 120 ms e lendo GetAsyncKeyState.")
    print("Nao mexa no teclado durante o teste.\n")
    print(f"{'tecla':16} {'injetado':>18}  {'Windows entendeu':<28} resultado")
    print("-" * 88)

    injetor = ew.Injetor()
    falhas = []

    for nome, vk, scan, estendida in TECLAS:
        injetor.tecla(vk, scan, estendida, True)
        time.sleep(0.12)
        # Le TODOS os laterais, nao so' o esperado: e' assim que aparece o caso
        # em que o Windows aceitou o evento mas trocou o lado.
        presos = [n.strip() for n, outro_vk, _, _ in TECLAS
                  if user32.GetAsyncKeyState(outro_vk) & 0x8000]
        injetor.tecla(vk, scan, estendida, False)
        time.sleep(0.05)

        esperado = nome.strip()
        # AltGr nao e' uma tecla: em ABNT2 e nos layouts latinos o Windows
        # sintetiza Ctrl esquerdo junto com o Alt direito, de proposito. Exigir
        # lista exata aqui reprovaria o comportamento correto.
        acompanhantes = {"Ctrl esquerdo"} if esperado == "Alt direito" else set()
        ok = esperado in presos and set(presos) - {esperado} <= acompanhantes
        if not ok:
            falhas.append((esperado, presos))
        print(f"{esperado:16} {f'scan={hex(scan)} ext={estendida}':>18}  "
              f"{(', '.join(presos) or '(nenhuma)'):<28} "
              f"{'ok' if ok else 'FALHOU'}")

    # Rede de seguranca: se alguma coisa ficou presa, o PC fica inutilizavel.
    injetor.soltar_modificadores()

    print()
    if not falhas:
        print("Todos os modificadores foram injetados e reconhecidos pelo lado")
        print("certo. O defeito NAO esta' na injecao -- rode o modo `captura`")
        print("no PC do teclado e compare os scancodes.")
        return
    for esperado, presos in falhas:
        print(f"FALHOU: injetei {esperado}, o Windows registrou "
              f"{', '.join(presos) or 'nenhuma tecla'}.")
    print("\nO defeito esta' na injecao (entrada_win.Injetor.tecla).")


def main() -> int:
    modo = sys.argv[1] if len(sys.argv) > 1 else ""
    if modo == "captura":
        captura()
    elif modo == "injecao":
        injecao()
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
