"""Sessao, desktop e criacao de processo entre sessoes -- o que o servico
precisa para por o motor no desktop certo.

Por que este modulo existe: um servico do Windows roda na **sessao 0**, que e'
isolada desde o Vista. De la' ele nao ve teclado, mouse nem tela de ninguem --
um hook instalado ali nunca dispara, e um SendInput nao chega a lugar nenhum.

Para o programa funcionar ANTES do login e na TELA DE BLOQUEIO, quem captura e
injeta precisa ser um processo:

  * rodando na sessao do console (a do monitor fisico), e
  * como SYSTEM, porque o desktop seguro `Winlogon` -- o da tela de bloqueio,
    do Ctrl+Alt+Del e do prompt de UAC -- so' aceita SYSTEM na sua DACL, e
  * anexado ao desktop de ENTRADA, o que estiver recebendo o teclado agora.

Hook e SendInput valem para UM desktop so'. Quando a tela bloqueia, o desktop
de entrada passa de `Default` para `Winlogon` e o processo antigo fica falando
sozinho com um desktop que ninguem mais ve. Nao da' para arrastar um processo
com hooks no ar de um desktop para o outro (SetThreadDesktop falha com janela
ou hook ja' criado), entao a estrategia e' relancar: um processo por desktop,
morrendo e nascendo a cada troca. E' tambem o que o Synergy faz.

Quem le' o desktop de entrada tem de estar na sessao interativa: da sessao 0 o
OpenInputDesktop enxerga apenas o desktop de entrada da propria sessao 0. Por
isso o servico nao vigia nada -- quem vigia e' o agente que ele lancou, que sai
com `SAIDA_TROCOU_DESKTOP` pedindo para nascer de novo no desktop novo.
"""

from __future__ import annotations

import base64
import logging
import os
import time

import ntsecuritycon
import win32api
import win32con
import win32event
import win32process
import win32profile
import win32security
import win32service
import win32ts

log = logging.getLogger("sessao")

# Codigo de saida do agente: "o desktop de entrada mudou, me relance nele".
SAIDA_TROCOU_DESKTOP = 20

SEM_SESSAO = 0xFFFFFFFF


def sessao_do_console() -> int | None:
    """Sessao ligada ao monitor/teclado fisicos, ou None se nao houver uma.

    Fica sem sessao entre o boot e o Winlogon aparecer, e durante a troca
    rapida de usuario. Nesses instantes nao ha' onde lancar o agente.
    """
    sessao = win32ts.WTSGetActiveConsoleSessionId()
    if sessao == SEM_SESSAO or sessao == 0:
        return None
    return int(sessao)


def _nome_do_desktop(handle) -> str:
    return win32service.GetUserObjectInformation(handle, win32service.UOI_NAME)


def desktop_de_entrada() -> str | None:
    """Nome do desktop que esta' recebendo teclado e mouse AGORA.

    `Default` na area de trabalho normal, `Winlogon` na tela de bloqueio, no
    Ctrl+Alt+Del e no prompt de UAC, `Screen-saver` na protecao de tela.
    Devolve None quando nem da' para abrir o desktop de entrada -- acontece por
    um instante no meio da troca, e a resposta certa ali e' esperar, nao
    relancar.
    """
    try:
        handle = win32service.OpenInputDesktop(0, False, win32con.MAXIMUM_ALLOWED)
    except Exception:  # pywintypes.error, e qualquer surpresa no meio da troca
        return None
    try:
        return _nome_do_desktop(handle)
    finally:
        handle.CloseDesktop()


def meu_desktop() -> str | None:
    try:
        return _nome_do_desktop(
            win32service.GetThreadDesktop(win32api.GetCurrentThreadId()))
    except Exception:
        return None


def appdata_do_usuario_do_console() -> "pathlib.Path | None":
    """`%APPDATA%` de quem esta' logado no console, visto daqui.

    Existe por causa de um jeito silencioso de nao funcionar: o agente roda
    como SYSTEM, e o `%APPDATA%` de SYSTEM e'
    `C:\\Windows\\system32\\config\\systemprofile\\AppData\\Roaming` -- pasta que
    o usuario logado nao consegue nem ler. Arquivos colados de um PC para o
    outro chegavam ali, o clipboard recebia esses caminhos, e colar no
    Explorador dava acesso negado sem que nada no log parecesse errado.

    Devolve None quando nao da' para saber (sem sessao no console, ou sem o
    privilegio SE_TCB que o WTSQueryUserToken exige -- e' o caso normal quando
    quem roda e' a janela do proprio usuario, que ja' tem o %APPDATA% certo).
    """
    import pathlib

    sessao = sessao_do_console()
    if sessao is None:
        return None
    try:
        token = win32ts.WTSQueryUserToken(sessao)
    except Exception:
        return None
    try:
        # Pelo bloco de ambiente, e nao montando "perfil\\AppData\\Roaming" na
        # mao: o AppData pode estar redirecionado por politica de dominio.
        ambiente = win32profile.CreateEnvironmentBlock(token, False)
        caminho = ambiente.get("APPDATA")
        if not caminho:
            caminho = str(pathlib.Path(
                win32profile.GetUserProfileDirectory(token))
                / "AppData" / "Roaming")
        return pathlib.Path(caminho)
    except Exception:
        log.warning("nao consegui achar o %%APPDATA%% do usuario do console",
                    exc_info=True)
        return None
    finally:
        token.Close()


def sou_system() -> bool:
    """Este processo roda como SYSTEM (o agente do inicio automatico)?"""
    try:
        token = win32security.OpenProcessToken(win32api.GetCurrentProcess(),
                                                win32con.TOKEN_QUERY)
        try:
            sid, _ = win32security.GetTokenInformation(token,
                                                       win32security.TokenUser)
        finally:
            token.Close()
        return win32security.ConvertSidToStringSid(sid) == "S-1-5-18"
    except Exception:
        return False


def comando_ler_arquivos(saida: str) -> str:
    """Linha de comando do PowerShell que grava em `saida` os arquivos copiados.

    PowerShell, e nao o nosso proprio .exe: ele nao pede elevacao (o nosso tem
    manifest requireAdministrator, que um token comum de usuario nao lanca),
    sobe em fracao de segundo em vez de extrair 40 MB de --onefile, e o
    Get-Clipboard le' pelo mesmo OLE que o Explorer usa para publicar.

    -EncodedCommand para nenhum caminho com espaco ou aspas precisar de
    escape na linha de comando.
    """
    alvo = saida.replace("'", "''")
    script = ("$f = Get-Clipboard -Format FileDropList "
              "-ErrorAction SilentlyContinue; "
              "if ($f) { $f.FullName | Set-Content -LiteralPath '" + alvo +
              "' -Encoding UTF8 }")
    codificado = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return ("powershell.exe -NoProfile -NonInteractive -Sta "
            "-WindowStyle Hidden -EncodedCommand " + codificado)


def ler_saida_de_arquivos(saida: str) -> "list[str] | None":
    """Le' (e apaga) o que o PowerShell gravou. None se nada veio."""
    try:
        with open(saida, encoding="utf-8-sig") as f:
            caminhos = [linha.strip() for linha in f if linha.strip()]
    except OSError:
        return None
    try:
        os.remove(saida)
    except OSError:
        pass
    return caminhos or None


def arquivos_do_clipboard_pelo_usuario(espera: float = 6.0) -> "list[str] | None":
    """Arquivos copiados no Explorer, lidos por um processo DO USUARIO.

    Por que existe: arquivo copiado no Explorer vai para o clipboard por OLE
    (OleSetClipboard), com os formatos entregues sob demanda PELO PROCESSO DO
    EXPLORER. O agente roda como SYSTEM -- outra conta -- e dali enxerga so' o
    marcador "DataObject"; o IDataObject responde, mas sem formato nenhum. Foi
    o que impediu, por semanas, copiar arquivo do PC da esquerda para o da
    direita (log: "o IDataObject respondeu, mas so' oferece: nada"), enquanto
    texto e imagem passavam. Reproduzido em 02/10/2026: com o MESMO objeto de
    dados do Explorer no clipboard, um processo de usuario ve' CF_HDROP e a
    lista de arquivos normalmente.

    Entao quem le' e' um PowerShell lancado com o token do usuario logado, na
    area de trabalho dele. Exige SE_TCB (WTSQueryUserToken): fora do agente
    devolve None, e quem chama segue como antes.
    """
    sessao = sessao_do_console()
    if sessao is None:
        return None
    try:
        token = win32ts.WTSQueryUserToken(sessao)
    except Exception:
        return None  # sem SE_TCB: nao somos o agente, nao ha' o que fazer
    try:
        primario = win32security.DuplicateTokenEx(
            token, win32security.SecurityImpersonation,
            win32con.MAXIMUM_ALLOWED, ntsecuritycon.TokenPrimary)
    finally:
        token.Close()
    try:
        ambiente = win32profile.CreateEnvironmentBlock(primario, False)
        temp = ambiente.get("TEMP") or ambiente.get("TMP")
        if not temp:
            return None
        saida = os.path.join(
            temp, f"multipc-kvm-clip-{os.getpid()}-{int(time.time() * 1000)}.txt")
        inicio = win32process.STARTUPINFO()
        inicio.lpDesktop = r"WinSta0\Default"
        processo, thread, _pid, _tid = win32process.CreateProcessAsUser(
            primario, None, comando_ler_arquivos(saida), None, None, False,
            win32con.CREATE_NO_WINDOW | win32con.CREATE_UNICODE_ENVIRONMENT,
            ambiente, None, inicio)
        thread.Close()
        try:
            if esperar(processo, espera) is None:
                encerrar(processo)
                log.info("o leitor de arquivos do clipboard nao respondeu em %.0fs",
                         espera)
        finally:
            processo.Close()
        return ler_saida_de_arquivos(saida)
    except Exception:
        log.info("nao consegui ler os arquivos do clipboard pelo usuario",
                 exc_info=True)
        return None
    finally:
        primario.Close()


def _token_do_system_para(sessao: int):
    """Copia primaria do proprio token de SYSTEM, apontada para outra sessao.

    Trocar o TokenSessionId exige o privilegio SE_TCB_NAME, que o servico tem
    por rodar como LocalSystem -- e e' justamente isso que permite atravessar o
    isolamento da sessao 0. O processo filho continua sendo SYSTEM (nao o
    usuario logado) porque so' SYSTEM entra no desktop Winlogon.
    """
    atual = win32security.OpenProcessToken(
        win32api.GetCurrentProcess(),
        win32con.TOKEN_DUPLICATE | win32con.TOKEN_QUERY)
    try:
        token = win32security.DuplicateTokenEx(
            atual, win32security.SecurityImpersonation, win32con.MAXIMUM_ALLOWED,
            ntsecuritycon.TokenPrimary)
    finally:
        atual.Close()
    win32security.SetTokenInformation(token, ntsecuritycon.TokenSessionId, sessao)
    return token


def lancar_na_sessao(sessao: int, desktop: str, executavel: str,
                     linha_de_comando: str, o_que: str = "agente"):
    """Sobe o executavel como SYSTEM, na `sessao`, anexado a `desktop`.

    Devolve o handle do processo (para esperar por ele) ou levanta a excecao do
    Win32. O chamador fecha o handle.
    """
    token = _token_do_system_para(sessao)
    try:
        ambiente = win32profile.CreateEnvironmentBlock(token, False)
        inicio = win32process.STARTUPINFO()
        inicio.lpDesktop = rf"WinSta0\{desktop}"
        processo, thread, pid, _tid = win32process.CreateProcessAsUser(
            token, executavel, linha_de_comando, None, None, False,
            win32con.CREATE_NO_WINDOW | win32con.CREATE_UNICODE_ENVIRONMENT,
            ambiente, None, inicio)
        thread.Close()
        # `o_que` existe porque esta funcao tambem lanca a JANELA, e dizer
        # "agente lancado" ali mandou o diagnostico para o lado errado: parecia
        # que o supervisor estava relancando o agente em laco, quando eram
        # janelas abrindo.
        log.info("%s %d lancado na sessao %d, desktop %s", o_que, pid, sessao,
                 desktop)
        return processo
    finally:
        token.Close()


def esperar(processo, segundos: float) -> int | None:
    """Espera o processo terminar. Devolve o codigo de saida, ou None no prazo."""
    resultado = win32event.WaitForSingleObject(processo.handle,
                                               int(segundos * 1000))
    if resultado != win32event.WAIT_OBJECT_0:
        return None
    return win32process.GetExitCodeProcess(processo.handle)


def encerrar(processo) -> None:
    try:
        win32process.TerminateProcess(processo.handle, 1)
    except Exception:
        pass  # ja' morreu: e' o estado que queriamos
