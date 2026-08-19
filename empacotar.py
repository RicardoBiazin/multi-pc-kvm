"""Gera o executavel (arquivo unico, com pedido de Administrador).

    python empacotar.py

O resultado fica em dist\\MultiPC-KVM.exe. E' so' copiar esse arquivo para cada
PC -- a configuracao nao vai junto, cada maquina guarda a dela em
%APPDATA%\\MultiPC-KVM\\config.json.
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess
import sys

import configuracao as conf

RAIZ = pathlib.Path(__file__).resolve().parent
ICONE = RAIZ / "icone.ico"
VERSAO_TXT = RAIZ / "versao.txt"
NOME_EXE = f"{conf.APP_ARQUIVO}.exe"

# Copia do projeto no outro PC (\\192.168.10.2\DEV-Direito). E' de la' que a
# outra maquina roda o programa, entao o .exe tem de ir junto a cada build: com
# versoes diferentes nos dois lados o protocolo quebra, e sem barulho nenhum.
# A PASTA nao foi renomeada junto com o programa na v1.3: ela e' o
# compartilhamento que vira o Y:, e renomea-la exigiria refazer o
# compartilhamento no outro PC a mao.
ESPELHOS = (pathlib.Path("Y:/2pc_1Kit/dist"),)


def gerar_versao() -> pathlib.Path:
    """Recurso de versao do Windows: alimenta as Propriedades do .exe."""
    partes = conf.VERSAO.split(".")
    numero = ", ".join((partes + ["0", "0", "0"])[:4])
    VERSAO_TXT.write_text(f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers=({numero}), prodvers=({numero})),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', {conf.AUTOR!r}),
      StringStruct('FileDescription',
                   'Um teclado e um mouse para varios PCs na mesma rede'),
      StringStruct('FileVersion', {conf.VERSAO!r}),
      StringStruct('ProductName', {conf.APP!r}),
      StringStruct('ProductVersion', {conf.VERSAO!r}),
      StringStruct('LegalCopyright', 'Copyright (c) 2026 {conf.AUTOR} -- MIT'),
    ])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])]),
  ],
)
""", encoding="utf-8")
    return VERSAO_TXT


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
    exe = dist / NOME_EXE
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
    gerar_versao()
    saida = destino()
    shutil.rmtree(RAIZ / "build", ignore_errors=True)
    # So' o .exe, e nao a pasta inteira: o programa grava o log e os relatorios
    # de diagnostico ao lado dele, e um rmtree aqui apagava justamente o que o
    # usuario guardou para descobrir por que algo nao funcionou.
    saida.mkdir(parents=True, exist_ok=True)
    (saida / NOME_EXE).unlink(missing_ok=True)

    comando = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--onefile",              # um .exe so'
        "--windowed",             # sem janela preta de console
        "--uac-admin",            # pede Administrador: sem isso o Windows
                                  # bloqueia input sobre janelas elevadas
        "--name", conf.APP_ARQUIVO,
        "--icon", str(ICONE),
        "--version-file", str(VERSAO_TXT),
        "--distpath", str(saida),
        "--hidden-import", "pystray._win32",
        # O inicio automatico fala com o Agendador de Tarefas por COM. Nada
        # disso aparece num import no topo de arquivo, entao o PyInstaller nao
        # acha sozinho e o checkbox quebraria so' no PC de destino.
        "--hidden-import", "win32com.client",
        "--hidden-import", "pythoncom",
        "--hidden-import", "win32timezone",
        "--exclude-module", "pytest",
        str(RAIZ / "app.py"),
    ]
    print(" ".join(comando), "\n")
    resultado = subprocess.run(comando, cwd=RAIZ)
    if resultado.returncode != 0:
        return resultado.returncode

    exe = saida / NOME_EXE
    print(f"\nPronto: {exe}  ({exe.stat().st_size / 1e6:.1f} MB)")
    espelhar(exe)
    print("\nNa primeira vez o programa cria a configuracao; use a mesma chave")
    print("e a mesma PORTA em todas as maquinas.")
    return 0


def espelhar(exe: pathlib.Path) -> None:
    """Leva o .exe para as copias do projeto nos outros PCs."""
    for pasta in ESPELHOS:
        alvo = pasta / exe.name
        try:
            pasta.mkdir(parents=True, exist_ok=True)
            shutil.copy2(exe, alvo)
            print(f"Copiado tambem para: {alvo}")
        except PermissionError:
            print(f"NAO consegui copiar para {alvo}: o arquivo esta' em uso.")
            print(f"   Feche o {conf.APP} naquele PC e rode de novo, ou copie "
                  "a mao.")
        except OSError as erro:
            print(f"NAO consegui copiar para {alvo}: {erro}")


if __name__ == "__main__":
    sys.exit(main())
