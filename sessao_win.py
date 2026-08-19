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

import logging

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
                     linha_de_comando: str):
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
        log.info("agente %d lancado na sessao %d, desktop %s", pid, sessao,
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
