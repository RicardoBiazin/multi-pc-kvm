"""Servico do Windows que sobe o motor no boot e o mantem no desktop de entrada.

O que isto resolve, e que o `Run` do registro nunca resolveu:

  1. **Nao subia ao reiniciar.** O executavel pede elevacao (manifest
     `requireAdministrator`, ver MultiPC-KVM.spec). O Windows ignora em
     silencio entrada de `HKCU\\...\\Run` que pede elevacao -- nao ha' como
     mostrar o prompt de UAC no logon. Um servico roda como LocalSystem: nao
     ha' prompt nenhum a mostrar.
  2. **Nao funcionava na tela de bloqueio.** O `Run` so' dispara depois do
     login, e um processo de usuario nao alcanca o desktop seguro. O servico
     comeca no boot, antes de qualquer login.

O servico em si nao captura nem injeta nada -- ele vive na sessao 0, onde isso
seria impossivel (ver sessao_win). Ele e' um supervisor: lanca o **agente** na
sessao do console, como SYSTEM, no desktop de entrada, e o relanca toda vez que
o agente sai -- por troca de desktop (a tela bloqueou), por troca de usuario ou
por erro.

    servico (sessao 0, SYSTEM)
      |
      +-- agente (sessao do console, SYSTEM, desktop Default)   <- area de trabalho
      +-- agente (sessao do console, SYSTEM, desktop Winlogon)  <- tela de bloqueio

Instalar e remover exigem Administrador; quem chama e' a janela de
configuracao, que ja' roda elevada.
"""

from __future__ import annotations

import logging
import pathlib
import sys
import threading
import time

import win32service
import win32serviceutil

import configuracao as conf
import sessao_win

log = logging.getLogger("servico")

NOME = "MultiPCKVM"
EXIBICAO = f"{conf.APP} (inicio automatico)"
DESCRICAO = ("Sobe o Multi PC - KVM no boot e o mantem no desktop de entrada, "
             "para que teclado e mouse compartilhados funcionem antes do login "
             "e na tela de bloqueio.")

DESKTOP_PADRAO = "Default"
# Se o agente morrer antes disto, tratamos como falha e esperamos antes de
# tentar de novo: sem essa pausa, um config quebrado vira um lanca-e-morre em
# laco fechado, comendo CPU e enchendo o log.
VIDA_CURTA = 10.0
PAUSA_APOS_FALHA = 5.0
INTERVALO_DE_VIGIA = 0.3


# -- onde o agente avisa em que desktop quer nascer --------------------------


def _arquivo_do_desktop() -> pathlib.Path:
    """Bilhete do agente para o servico: o nome do desktop de entrada novo.

    Nao da' para mandar isso pelo codigo de saida (o nome e' texto, e alem de
    Default e Winlogon existe Screen-saver e o que mais o Windows criar), e o
    servico nao pode ler o desktop de entrada sozinho: da sessao 0 ele so'
    enxerga a propria sessao 0.
    """
    return conf.pasta_de_saida() / "servico-desktop.txt"


def _gravar_desktop(nome: str) -> None:
    try:
        _arquivo_do_desktop().write_text(nome, encoding="utf-8")
    except OSError:
        log.warning("nao consegui gravar o desktop alvo", exc_info=True)


def _ler_desktop() -> str:
    try:
        nome = _arquivo_do_desktop().read_text(encoding="utf-8").strip()
    except OSError:
        return DESKTOP_PADRAO
    return nome or DESKTOP_PADRAO


# -- instalacao --------------------------------------------------------------


def _binario() -> tuple[str, str]:
    """(executavel, linha de comando) que o Windows vai rodar como servico."""
    if getattr(sys, "frozen", False):
        return sys.executable, f'"{sys.executable}" --servico'
    script = conf.pasta_do_executavel() / "app.py"
    return sys.executable, f'"{sys.executable}" "{script}" --servico'


def linha_do_agente(desktop: str) -> tuple[str, str]:
    executavel, _ = _binario()
    if getattr(sys, "frozen", False):
        return executavel, f'"{executavel}" --agente --desktop "{desktop}"'
    script = conf.pasta_do_executavel() / "app.py"
    return executavel, (f'"{executavel}" "{script}" --agente '
                        f'--desktop "{desktop}"')


def _gerente(acesso=win32service.SC_MANAGER_CONNECT):
    return win32service.OpenSCManager(None, None, acesso)


def instalado() -> bool:
    try:
        gerente = _gerente()
    except Exception:
        return False
    try:
        servico = win32service.OpenService(gerente, NOME,
                                           win32service.SERVICE_QUERY_STATUS)
        win32service.CloseServiceHandle(servico)
        return True
    except Exception:
        return False
    finally:
        win32service.CloseServiceHandle(gerente)


def rodando() -> bool:
    try:
        estado = win32serviceutil.QueryServiceStatus(NOME)[1]
    except Exception:
        return False
    return estado == win32service.SERVICE_RUNNING


def instalar() -> None:
    """Cria (ou atualiza) o servico e o deixa rodando. Exige Administrador."""
    _executavel, comando = _binario()
    gerente = _gerente(win32service.SC_MANAGER_ALL_ACCESS)
    try:
        if instalado():
            servico = win32service.OpenService(gerente, NOME,
                                               win32service.SERVICE_ALL_ACCESS)
            win32service.ChangeServiceConfig(
                servico, win32service.SERVICE_WIN32_OWN_PROCESS,
                win32service.SERVICE_AUTO_START,
                win32service.SERVICE_ERROR_NORMAL, comando, None, 0, None,
                None, None, EXIBICAO)
        else:
            servico = win32service.CreateService(
                gerente, NOME, EXIBICAO, win32service.SERVICE_ALL_ACCESS,
                win32service.SERVICE_WIN32_OWN_PROCESS,
                win32service.SERVICE_AUTO_START,
                win32service.SERVICE_ERROR_NORMAL, comando, None, 0, None,
                None, None)
        try:
            win32service.ChangeServiceConfig2(
                servico, win32service.SERVICE_CONFIG_DESCRIPTION, DESCRICAO)
            # Se o servico cair, o Windows o levanta de novo em 5s.
            win32service.ChangeServiceConfig2(
                servico, win32service.SERVICE_CONFIG_FAILURE_ACTIONS,
                {"ResetPeriod": 86400, "RebootMsg": None, "Command": None,
                 "Actions": [(win32service.SC_ACTION_RESTART, 5000),
                             (win32service.SC_ACTION_RESTART, 5000),
                             (win32service.SC_ACTION_RESTART, 30000)]})
        except Exception:
            log.warning("descricao/recuperacao do servico nao aplicadas",
                        exc_info=True)
        win32service.CloseServiceHandle(servico)
    finally:
        win32service.CloseServiceHandle(gerente)
    log.info("servico %s instalado: %s", NOME, comando)
    if not rodando():
        win32serviceutil.StartService(NOME)


def remover() -> None:
    """Para e apaga o servico. Exige Administrador."""
    if rodando():
        try:
            win32serviceutil.StopService(NOME)
            for _ in range(30):
                if not rodando():
                    break
                time.sleep(0.5)
        except Exception:
            log.warning("o servico nao parou no pedido", exc_info=True)
    win32serviceutil.RemoveService(NOME)
    log.info("servico %s removido", NOME)


# -- o servico (sessao 0) ----------------------------------------------------


def supervisionar(parar: threading.Event) -> None:
    """Mantem um agente vivo na sessao do console, no desktop de entrada."""
    desktop = _ler_desktop()
    while not parar.is_set():
        sessao = sessao_win.sessao_do_console()
        if sessao is None:  # entre o boot e o Winlogon, ou troca de usuario
            parar.wait(2)
            continue
        executavel, comando = linha_do_agente(desktop)
        try:
            processo = sessao_win.lancar_na_sessao(sessao, desktop, executavel,
                                                   comando)
        except Exception:
            log.error("nao consegui lancar o agente na sessao %d, desktop %s",
                      sessao, desktop, exc_info=True)
            desktop = DESKTOP_PADRAO  # o desktop anotado pode nem existir mais
            parar.wait(PAUSA_APOS_FALHA)
            continue

        nasceu = time.monotonic()
        codigo = None
        try:
            while not parar.is_set():
                codigo = sessao_win.esperar(processo, 1.0)
                if codigo is not None:
                    break
                if sessao_win.sessao_do_console() != sessao:
                    log.info("a sessao do console mudou; encerrando o agente")
                    sessao_win.encerrar(processo)
                    codigo = sessao_win.esperar(processo, 5.0)
                    break
            if parar.is_set():
                sessao_win.encerrar(processo)
                sessao_win.esperar(processo, 5.0)
                return
        finally:
            processo.Close()

        if codigo == sessao_win.SAIDA_TROCOU_DESKTOP:
            desktop = _ler_desktop()
            log.info("o desktop de entrada virou %s; relancando", desktop)
            continue
        log.info("o agente saiu com codigo %s", codigo)
        desktop = _ler_desktop()
        if time.monotonic() - nasceu < VIDA_CURTA:
            parar.wait(PAUSA_APOS_FALHA)


class Servico(win32serviceutil.ServiceFramework):
    _svc_name_ = NOME
    _svc_display_name_ = EXIBICAO
    _svc_description_ = DESCRICAO

    def __init__(self, args):
        super().__init__(args)
        self.parar = threading.Event()

    def SvcStop(self) -> None:
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        self.parar.set()

    def SvcDoRun(self) -> None:
        try:
            supervisionar(self.parar)
        except Exception:
            log.error("o supervisor parou com erro", exc_info=True)
            raise


def rodar_como_servico() -> int:
    """Entrega o processo ao SCM. So' faz sentido chamado pelo Windows."""
    import servicemanager
    servicemanager.Initialize()
    servicemanager.PrepareToHostSingle(Servico)
    servicemanager.StartServiceCtrlDispatcher()
    return 0


# -- o agente (sessao do console, num desktop) -------------------------------


def rodar_agente(cfg: dict) -> int:
    """Sobe o motor e vigia o desktop de entrada. Devolve o codigo de saida.

    Sai com SAIDA_TROCOU_DESKTOP quando a tela bloqueia (ou desbloqueia): hook
    e SendInput valem para um desktop so', entao quem atende o desktop novo tem
    de ser um processo novo -- o servico o lanca.
    """
    import motor

    meu = sessao_win.meu_desktop()
    log.info("agente no desktop %s (entrada: %s)", meu,
             sessao_win.desktop_de_entrada())

    def trocou() -> str | None:
        entrada = sessao_win.desktop_de_entrada()
        if entrada is None or meu is None:  # instante da troca: nao concluir nada
            return None
        return None if entrada.lower() == meu.lower() else entrada

    alvo = trocou()
    if alvo:  # nasceu no desktop errado -- a tela mudou entre o lance e o start
        _gravar_desktop(alvo)
        return sessao_win.SAIDA_TROCOU_DESKTOP

    m = motor.Motor(cfg)
    try:
        m.iniciar(cfg)
    except ValueError as exc:
        log.error("configuracao incompleta: %s", exc)
        log.error("o servico le' o config.json ao lado do executavel; abra a "
                  "janela de configuracao e marque 'Iniciar com o Windows' de "
                  "novo para grava-lo la'")
        return 2
    try:
        while True:
            time.sleep(INTERVALO_DE_VIGIA)
            alvo = trocou()
            if alvo:
                log.info("o desktop de entrada virou %s; saindo para nascer la'",
                         alvo)
                _gravar_desktop(alvo)
                return sessao_win.SAIDA_TROCOU_DESKTOP
            if not m.ativo():
                log.error("o motor parou sozinho")
                return 1
    finally:
        m.parar()
