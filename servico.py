"""Inicio automatico: sobe o motor no boot e o mantem no desktop de entrada.

O que isto resolve, e que o `Run` do registro nunca resolveu:

  1. **Nao subia ao reiniciar.** O executavel pede elevacao (manifest
     `requireAdministrator`, ver MultiPC-KVM.spec). O Windows ignora em
     silencio entrada de `HKCU\\...\\Run` que pede elevacao -- nao ha' como
     mostrar o prompt de UAC no logon.
  2. **Nao funcionava na tela de bloqueio.** O `Run` so' dispara depois do
     login, e um processo de usuario nao alcanca o desktop seguro.

Quem sobe o programa agora e' uma **tarefa agendada** disparada no boot, rodando
como SYSTEM (`S-1-5-18`). Nao ha' prompt de UAC a mostrar para SYSTEM, e ela
comeca antes de qualquer login.

Por que tarefa e nao um servico de verdade, ja' que a conta e o gatilho seriam
os mesmos: o `.exe` e' `--onefile`, e nesse modo o bootloader do PyInstaller
extrai o pacote e roda o Python num processo FILHO. O SCM vigia o processo que
ele criou -- o pai --, que nunca chama `StartServiceCtrlDispatcher`, e derruba o
servico em 30s com o erro 1053. Uma tarefa agendada nao exige que o processo se
apresente a ninguem, entao o modelo de dois processos deixa de importar. A
alternativa seria abandonar o `--onefile` e passar a distribuir uma pasta em vez
de um arquivo -- e copiar UM .exe para cada PC e' o jeito de instalar isto.

O supervisor nao captura nem injeta nada: ele vive na sessao 0, isolada do
teclado e da tela desde o Vista, onde um hook nunca dispara e um SendInput nao
chega a lugar nenhum. Ele lanca o **agente** na sessao do console, como SYSTEM,
no desktop que esta' recebendo o teclado, e o relanca toda vez que o agente sai
-- por troca de desktop (a tela bloqueou), por troca de usuario ou por erro.

    supervisor (sessao 0, SYSTEM)
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
from xml.sax.saxutils import escape as escapar_xml

import win32job

import configuracao as conf
import sessao_win

log = logging.getLogger("servico")

NOME = "MultiPCKVM"
DESCRICAO = ("Sobe o Multi PC - KVM no boot e o mantem no desktop de entrada, "
             "para que teclado e mouse compartilhados funcionem antes do login "
             "e na tela de bloqueio.")

# Estado da tarefa, do Agendador (TASK_STATE_RUNNING). O numero e' o mesmo em
# qualquer idioma do Windows -- ler "Em execucao" da saida do schtasks nao seria.
TAREFA_RODANDO = 4

DESKTOP_PADRAO = "Default"
# Se o agente morrer antes disto, tratamos como falha e esperamos antes de
# tentar de novo: sem essa pausa, um config quebrado vira um lanca-e-morre em
# laco fechado, comendo CPU e enchendo o log.
VIDA_CURTA = 10.0
PAUSA_APOS_FALHA = 5.0
INTERVALO_DE_VIGIA = 0.3


# -- onde o agente avisa em que desktop quer nascer --------------------------


def _arquivo_do_desktop() -> pathlib.Path:
    """Bilhete do agente para o supervisor: o nome do desktop de entrada novo.

    Nao da' para mandar isso pelo codigo de saida (o nome e' texto, e alem de
    Default e Winlogon existe Screen-saver e o que mais o Windows criar), e o
    supervisor nao pode ler o desktop de entrada sozinho: da sessao 0 ele so'
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


# -- a tarefa agendada -------------------------------------------------------


def _binario() -> tuple[str, str]:
    """(executavel, argumentos) que o Windows vai rodar no boot."""
    if getattr(sys, "frozen", False):
        return sys.executable, "--servico"
    script = conf.pasta_do_executavel() / "app.py"
    return sys.executable, f'"{script}" --servico'


def linha_do_agente(desktop: str) -> tuple[str, str]:
    """(executavel, linha de comando completa) do agente num dado desktop."""
    executavel, _ = _binario()
    if getattr(sys, "frozen", False):
        return executavel, f'"{executavel}" --agente --desktop "{desktop}"'
    script = conf.pasta_do_executavel() / "app.py"
    return executavel, (f'"{executavel}" "{script}" --agente '
                        f'--desktop "{desktop}"')


def xml_da_tarefa() -> str:
    """Definicao da tarefa.

    Os ajustes que nao sao obvios, e sem os quais isto nao serviria:

      * `UserId` S-1-5-18 e `LogonType` 5 -- SYSTEM, sem senha e sem sessao.
        SID em vez de "SYSTEM" porque o nome da conta e' traduzido.
      * `ExecutionTimeLimit` PT0S -- sem limite. O padrao mata a tarefa em 3
        dias, e este programa e' para ficar no ar.
      * `DisallowStartIfOnBatteries` false -- senao um notebook na bateria
        simplesmente nao subiria.
      * `MultipleInstancesPolicy` IgnoreNew -- o Run da instalacao nao pode
        criar um segundo supervisor ao lado do que ja' esta' no ar.
    """
    executavel, argumentos = _binario()
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Author>{escapar_xml(conf.AUTOR)}</Author>
    <Description>{escapar_xml(DESCRICAO)}</Description>
  </RegistrationInfo>
  <Triggers>
    <BootTrigger>
      <Enabled>true</Enabled>
    </BootTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>S-1-5-18</UserId>
      <RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>false</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>5</Priority>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>999</Count>
    </RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escapar_xml(executavel)}</Command>
      <Arguments>{escapar_xml(argumentos)}</Arguments>
    </Exec>
  </Actions>
</Task>
"""


def _agendador():
    import win32com.client
    agendador = win32com.client.Dispatch("Schedule.Service")
    agendador.Connect()
    return agendador.GetFolder("\\")


def _tarefa():
    """A tarefa registrada, ou None."""
    try:
        return _agendador().GetTask(NOME)
    except Exception:
        return None


def instalado() -> bool:
    return _tarefa() is not None


def rodando() -> bool:
    tarefa = _tarefa()
    try:
        return tarefa is not None and tarefa.State == TAREFA_RODANDO
    except Exception:
        return False


def instalar() -> None:
    """Registra (ou atualiza) a tarefa e a poe no ar. Exige Administrador."""
    pasta = _agendador()
    # 6 = TASK_CREATE_OR_UPDATE, 5 = TASK_LOGON_SERVICE_ACCOUNT.
    pasta.RegisterTask(NOME, xml_da_tarefa(), 6, None, None, 5)
    log.info("tarefa %s registrada: %s %s", NOME, *_binario())
    if not rodando():
        pasta.GetTask(NOME).Run(None)


def remover() -> None:
    """Para e apaga a tarefa. Exige Administrador."""
    tarefa = _tarefa()
    if tarefa is not None:
        try:
            tarefa.Stop(0)
            for _ in range(20):
                if not rodando():
                    break
                time.sleep(0.25)
        except Exception:
            log.warning("a tarefa nao parou no pedido", exc_info=True)
        _agendador().DeleteTask(NOME, 0)
    log.info("tarefa %s removida", NOME)


# -- o supervisor (sessao 0) -------------------------------------------------


def _prisao():
    """Job que leva os agentes junto quando o supervisor morre.

    Parar a tarefa MATA o supervisor -- nenhum `finally` dele roda. Sem isto,
    desligar o inicio automatico deixaria para tras um agente SYSTEM com os
    hooks instalados, vivo ate' o proximo boot e invisivel para quem so' olha o
    Agendador de Tarefas.
    """
    job = win32job.CreateJobObject(None, "")
    info = win32job.QueryInformationJobObject(
        job, win32job.JobObjectExtendedLimitInformation)
    info["BasicLimitInformation"]["LimitFlags"] |= \
        win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    win32job.SetInformationJobObject(
        job, win32job.JobObjectExtendedLimitInformation, info)
    return job


def supervisionar(parar: threading.Event) -> None:
    """Mantem um agente vivo na sessao do console, no desktop de entrada."""
    job = _prisao()
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
        try:
            win32job.AssignProcessToJobObject(job, processo)
        except Exception:
            log.warning("o agente ficou fora do job; se me matarem a forca ele "
                        "sobrevive", exc_info=True)

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


def rodar_como_servico() -> int:
    """O processo que a tarefa agendada roda no boot. So' sai se o matarem."""
    log.info("supervisor no ar (sessao do console: %s)",
             sessao_win.sessao_do_console() or "nenhuma ainda")
    supervisionar(threading.Event())
    return 0


# -- o agente (sessao do console, num desktop) -------------------------------


def rodar_agente(cfg: dict) -> int:
    """Sobe o motor e vigia o desktop de entrada. Devolve o codigo de saida.

    Sai com SAIDA_TROCOU_DESKTOP quando a tela bloqueia (ou desbloqueia): hook
    e SendInput valem para um desktop so', entao quem atende o desktop novo tem
    de ser um processo novo -- o supervisor o lanca.
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
        log.error("o inicio automatico le' o config.json ao lado do "
                  "executavel; abra a janela de configuracao e marque "
                  "'Iniciar com o Windows' de novo para grava-lo la'")
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
