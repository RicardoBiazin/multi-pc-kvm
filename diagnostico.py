"""Por que nao conectou: teste de alcance e liberacao no Firewall do Windows.

"Coloquei o IP e nao conectou" quase sempre e' uma destas quatro coisas, e cada
uma pede uma acao diferente:

* IP em outra sub-rede  -> os dois PCs nao se enxergam (Wi-Fi x cabo);
* tempo esgotado        -> Firewall barrando, ou o PC desligado;
* conexao recusada      -> chegou la', mas nao ha' servidor ouvindo naquela porta;
* conectou              -> a rede esta' boa; se ainda assim falha, e' a chave.
"""

from __future__ import annotations

import ctypes
import logging
import socket
import subprocess
import sys
from ctypes import wintypes

import redes

log = logging.getLogger("diagnostico")

import configuracao as conf

REGRA_TCP = f"{conf.APP} (TCP)"
REGRA_UDP = f"{conf.APP} (busca na rede UDP)"
# Regras criadas ate' a v1.2, quando o programa se chamava outro nome. Ficam
# listadas para o relatorio poder avisar que sobraram no firewall: elas nao
# atrapalham (apontam para o mesmo executavel), mas confundem quem for auditar.
REGRAS_ANTIGAS = (
    "2pc_1Kit (TCP)", "2pc_1Kit (busca na rede UDP)",
    # Esta nao foi criada por nos: e' a que o Windows cria quando pergunta
    # "permitir este programa?". Ela ficava presa ao caminho do executavel
    # anterior, e era ELA que sustentava a conexao em rede publica -- ao renomear
    # o programa, deixou de casar com qualquer arquivo e a conexao caiu. Sai
    # junto porque aponta para um .exe que nao existe mais.
    "2pc_1kit",
)


def testar(ip: str, porta: int, prazo: float = 3.0) -> tuple[bool, str]:
    """(deu certo, explicacao em portugues) para uma tentativa de TCP."""
    ip = (ip or "").strip()
    if not ip:
        return False, "sem IP para testar"

    placa = redes.alcanca(ip)
    if placa is None:
        minhas = ", ".join(p.rotulo() for p in redes.listar()) or "nenhuma"
        return False, (f"{ip} nao esta' em nenhuma das redes desta maquina "
                       f"({minhas}). Ligue os dois PCs na mesma rede, ou use o "
                       f"IP que o outro PC tem nessa rede.")

    try:
        with socket.create_connection((ip, porta), timeout=prazo):
            return True, (f"conectou em {ip}:{porta} pela placa "
                          f"{placa.nome or placa.tipo} -- a rede esta' boa")
    except socket.timeout:
        return False, (f"{ip}:{porta} nao respondeu em {prazo:.0f}s. Costuma ser "
                       f"o Firewall do Windows do outro PC: clique la' em "
                       f"'Liberar no Firewall'. Verifique tambem se ele esta' "
                       f"ligado e com o programa aberto.")
    except ConnectionRefusedError:
        return False, (f"{ip} respondeu mas recusou a porta {porta}: o programa "
                       f"esta' aberto la', mas aquele PC nao e' o servidor ou "
                       f"nao clicou em Iniciar.")
    except OSError as exc:
        return False, f"falha ao alcancar {ip}:{porta} -- {exc}"


def escutando(porta: int) -> bool:
    """Se alguem ja' ocupa a porta TCP nesta maquina (o proprio servidor, por ex.)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("0.0.0.0", porta))
        return False
    except OSError:
        return True


# -- Firewall ---------------------------------------------------------------


def _netsh(*argumentos: str) -> tuple[int, str]:
    try:
        r = subprocess.run(["netsh", "advfirewall", "firewall", *argumentos],
                           capture_output=True, text=True, timeout=20,
                           creationflags=0x08000000)  # CREATE_NO_WINDOW
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)


# Programas que fazem a MESMA coisa que este e disputam os mesmos hooks de
# mouse e teclado. Com um deles no ar, a nossa injecao nao tem efeito e o
# SetCursorPos passa a devolver 0 -- sem sintoma nenhum no log.
CONFLITANTES = {
    "mousewithoutborders.exe": "Mouse Without Borders (PowerToys)",
    "mousewithoutbordershelper.exe": "Mouse Without Borders (PowerToys)",
    "mousewithoutborderssvc.exe": "Mouse Without Borders (PowerToys)",
    "synergy.exe": "Synergy",
    "synergyc.exe": "Synergy",
    "synergys.exe": "Synergy",
    "synergy-core.exe": "Synergy",
    "deskflow.exe": "Deskflow",
    "deskflow-server.exe": "Deskflow",
    "deskflow-client.exe": "Deskflow",
    "input-leap.exe": "Input Leap",
    "input-leaps.exe": "Input Leap",
    "input-leapc.exe": "Input Leap",
    "barrier.exe": "Barrier",
    "barrierc.exe": "Barrier",
    "barriers.exe": "Barrier",
    "sharemouse.exe": "ShareMouse",
    "multiplicity.exe": "Multiplicity",
    "inputdirector.exe": "Input Director",
}

TH32CS_SNAPPROCESS = 0x00000002


class _PROCESSENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
        ("szExeFile", ctypes.c_char * 260),
    ]


def processos() -> list[str]:
    """Nomes dos executaveis em execucao, em minusculas."""
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    instantaneo = k.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if instantaneo in (0, -1, None) or instantaneo == wintypes.HANDLE(-1).value:
        return []
    entrada = _PROCESSENTRY32()
    entrada.dwSize = ctypes.sizeof(_PROCESSENTRY32)
    nomes = []
    try:
        if k.Process32First(instantaneo, ctypes.byref(entrada)):
            while True:
                nomes.append(entrada.szExeFile.decode("latin-1").lower())
                if not k.Process32Next(instantaneo, ctypes.byref(entrada)):
                    break
    finally:
        k.CloseHandle(instantaneo)
    return nomes


def programas_conflitantes() -> list[str]:
    """Programas de teclado/mouse compartilhado rodando agora."""
    ativos = set(processos())
    achados = {nome_amigavel for exe, nome_amigavel in CONFLITANTES.items()
               if exe in ativos}
    return sorted(achados)


def elevado() -> bool:
    """Se o processo esta' rodando como Administrador."""
    try:
        return bool(ctypes.WinDLL("shell32").IsUserAnAdmin())
    except (OSError, AttributeError):
        return False


def perfis_de_rede() -> list[tuple[str, str]]:
    """[(nome da rede, 'Private'/'Public'/'DomainAuthenticated')].

    Importa porque uma rede marcada como **Publica** faz o Windows barrar tudo o
    que vem de fora, e as regras que criamos (privada/dominio) nem se aplicam.
    """
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "Get-NetConnectionProfile | ForEach-Object "
             "{ $_.Name + '|' + $_.NetworkCategory }"],
            capture_output=True, text=True, timeout=20,
            creationflags=0x08000000)
    except (OSError, subprocess.SubprocessError):
        return []
    perfis = []
    for linha in (r.stdout or "").splitlines():
        if "|" in linha:
            nome, _, categoria = linha.strip().partition("|")
            perfis.append((nome, categoria))
    return perfis


def redes_publicas() -> list[str]:
    return [nome for nome, categoria in perfis_de_rede() if categoria == "Public"]


def regras_existem() -> bool:
    """True so' se a regra existe E aponta para o executavel de agora.

    Conferir o nome nao basta: uma regra criada para o executavel anterior
    continua existindo e aparecendo no painel depois de uma renomeacao ou de
    mover a pasta -- e nao vale mais para nada. Foi assim que a conexao entre os
    dois PCs quebrou na v1.3, sem nenhum aviso: o painel dizia que estava
    liberado. `verbose` e' o que traz a linha do programa na saida.
    """
    codigo, saida = _netsh("show", "rule", f"name={REGRA_TCP}", "verbose")
    if codigo != 0:
        return False
    exe = f"{conf.APP_ARQUIVO}.exe".lower()
    return exe in saida.lower()


def liberar(porta_tcp: int, porta_udp: int) -> tuple[bool, str]:
    """Cria as regras de entrada. Precisa de Administrador -- o .exe ja' roda assim.

    As regras sao presas ao EXECUTAVEL e valem em TODOS os perfis de rede.
    Ate' a v1.3 eram o contrario -- qualquer programa, so' nos perfis privado e
    de dominio -- e isso falhava calado no caso mais comum deste programa: o
    cabo direto entre dois PCs quase sempre entra como "Rede nao identificada",
    que o Windows classifica como PUBLICA. Nesse perfil a regra existe, aparece
    no painel e nao vale para nada; o sintoma e' timeout na conexao, sem uma
    linha dizendo que foi o firewall.

    A troca aperta um eixo e afrouxa outro. Aperta: so' este executavel passa,
    em vez de qualquer programa que ocupe a porta. Afrouxa: passa a valer em rede
    publica tambem. O que sustenta isso e' o resto do desenho -- o servidor
    escuta apenas no IP configurado, nao em 0.0.0.0, e o handshake exige a chave
    compartilhada. Sem a chave, alcancar a porta nao leva a nada.
    """
    # Rodando do codigo-fonte nao existe executavel para prender a regra: o
    # `sys.executable` seria o python.exe, e liberar o python inteiro em rede
    # publica seria pior que o problema. Nesse caso ficamos na regra antiga --
    # por porta, so' nos perfis privado e de dominio -- e o texto de retorno diz
    # que foi isso, para ninguem concluir que esta' igual ao .exe.
    exe = sys.executable if getattr(sys, "frozen", False) else ""
    # As antigas saem junto: ficaram sem efeito quando o programa foi renomeado
    # (apontavam para o executavel anterior) e so' confundem quem audita.
    for nome in (REGRA_TCP, REGRA_UDP, *REGRAS_ANTIGAS):
        _netsh("delete", "rule", f"name={nome}")

    erros = []
    for nome, protocolo, porta in ((REGRA_TCP, "TCP", porta_tcp),
                                   (REGRA_UDP, "UDP", porta_udp)):
        argumentos = ["add", "rule", f"name={nome}", "dir=in", "action=allow",
                      f"protocol={protocolo}", f"localport={porta}",
                      "enable=yes"]
        argumentos += ([f"program={exe}", "profile=any"] if exe
                       else ["profile=private,domain"])
        codigo, saida = _netsh(*argumentos)
        if codigo != 0:
            erros.append(f"{protocolo}/{porta}: {saida.strip()[:120]}")

    if erros:
        return False, ("nao consegui criar as regras (precisa de Administrador): "
                       + "; ".join(erros))

    aviso = ""
    publicas = redes_publicas()
    if publicas:
        # Nao e' mais impedimento para a conexao, mas continua valendo dizer: em
        # rede publica o Windows desliga a descoberta de rede, e o usuario perde
        # a lista automatica de PCs (o IP digitado a mao segue funcionando).
        aviso = (f" A rede '{publicas[0]}' esta' marcada como Publica: a conexao "
                 f"funciona, mas a busca automatica de PCs nao. Para te-la, mude "
                 f"para Rede particular em Configuracoes > Rede.")
    alcance = (f"para {conf.APP_ARQUIVO}.exe, em todos os perfis de rede" if exe
               else "para as redes privada e de dominio (rodando do codigo-fonte, "
                    "sem executavel para prender a regra)")
    return True, (f"liberados TCP {porta_tcp} e UDP {porta_udp} "
                  f"{alcance}.{aviso}")


def resumo_das_placas() -> str:
    placas = redes.listar()
    if not placas:
        return "nenhuma placa de rede ativa"
    return " | ".join(p.rotulo() for p in placas)
