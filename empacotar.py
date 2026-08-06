"""Gera o 2pc_1Kit.exe (arquivo unico, com pedido de Administrador).

    python empacotar.py

O resultado fica em dist\\2pc_1Kit.exe. E' so' copiar esse arquivo para cada PC
-- a configuracao nao vai junto, cada maquina guarda a dela em
%APPDATA%\\2pc_1Kit\\config.json.
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess
import sys

RAIZ = pathlib.Path(__file__).resolve().parent
ICONE = RAIZ / "icone.ico"


def gerar_icone() -> pathlib.Path:
    """Desenha o icone do programa (um teclado) em varios tamanhos."""
    from PIL import Image, ImageDraw

    def desenhar(lado: int) -> Image.Image:
        e = lado / 64  # escala a partir do desenho de 64px
        img = Image.new("RGBA", (lado, lado), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle((4 * e, 16 * e, 60 * e, 48 * e), radius=max(2, 7 * e),
                            fill=(47, 107, 63, 255))
        for coluna in range(5):
            for linha in range(2):
                x = (11 + coluna * 9) * e
                y = (23 + linha * 10) * e
                d.rectangle((x, y, x + 6 * e, y + 6 * e), fill=(255, 255, 255, 235))
        return img

    base = desenhar(256)
    base.save(ICONE, sizes=[(256, 256), (64, 64), (48, 48), (32, 32), (16, 16)])
    return ICONE


def destino() -> pathlib.Path:
    """dist\\, ou dist-novo\\ se o .exe estiver aberto.

    O programa roda elevado, entao um build normal nao consegue nem sobrescrever
    nem encerrar a instancia aberta -- melhor gerar ao lado do que falhar.
    """
    dist = RAIZ / "dist"
    exe = dist / "2pc_1Kit.exe"
    if exe.exists():
        try:
            with open(exe, "r+b"):
                pass
        except OSError:
            alternativo = RAIZ / "dist-novo"
            print(f"AVISO: {exe} esta' em uso -- o programa esta' aberto.")
            print(f"       Gerando em {alternativo}; feche o programa e substitua.\n")
            return alternativo
    return dist


def main() -> int:
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("PyInstaller nao instalado. Rode:  pip install pyinstaller pystray")
        return 1

    gerar_icone()
    saida = destino()
    shutil.rmtree(RAIZ / "build", ignore_errors=True)
    shutil.rmtree(saida, ignore_errors=True)

    comando = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--onefile",              # um .exe so'
        "--windowed",             # sem janela preta de console
        "--uac-admin",            # pede Administrador: sem isso o Windows
                                  # bloqueia input sobre janelas elevadas
        "--name", "2pc_1Kit",
        "--icon", str(ICONE),
        "--distpath", str(saida),
        "--hidden-import", "pystray._win32",
        "--exclude-module", "pytest",
        str(RAIZ / "app.py"),
    ]
    print(" ".join(comando), "\n")
    resultado = subprocess.run(comando, cwd=RAIZ)
    if resultado.returncode != 0:
        return resultado.returncode

    exe = saida / "2pc_1Kit.exe"
    print(f"\nPronto: {exe}  ({exe.stat().st_size / 1e6:.1f} MB)")
    print("Copie esse arquivo para cada PC e abra. Na primeira vez ele cria a")
    print("configuracao; use a mesma chave em todas as maquinas.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
