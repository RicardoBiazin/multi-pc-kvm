@echo off
rem ============================================================================
rem  Multi PC - KVM : poe a tarefa de inicio automatico no ar AGORA.
rem
rem  Equivale a  schtasks /run /tn MultiPCKVM  -- mas pedindo elevacao sozinho.
rem  A tarefa roda como SYSTEM e so' Administrador pode dispara-la; chamada de
rem  um prompt comum, ela responde "Acesso negado" e nada acontece.
rem
rem  Use depois de trocar o MultiPC-KVM.exe, ou quando o compartilhamento nao
rem  estiver no ar. Nao precisa reiniciar a maquina.
rem
rem  As consultas vao pelo PowerShell (Get-ScheduledTask), e nao pelo texto do
rem  schtasks: o estado vem como valor (Ready/Running/Disabled) em vez de frase
rem  traduzida, e "nao existe" deixa de se confundir com "acesso negado".
rem ============================================================================

setlocal
set TAREFA=MultiPCKVM

rem -- Ja' estamos elevados? "openfiles" so' responde como Administrador.
openfiles >nul 2>&1
if errorlevel 1 (
    echo Pedindo elevacao...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs" >nul 2>&1
    if errorlevel 1 (
        echo.
        echo NAO consegui pedir elevacao. Abra o Prompt de Comando como
        echo Administrador e rode:   schtasks /run /tn %TAREFA%
        echo.
        pause
    )
    exit /b
)

echo.
echo === Multi PC - KVM : ligando o inicio automatico ===
echo.

rem -- A tarefa existe?
powershell -NoProfile -Command ^
  "if (Get-ScheduledTask -TaskName '%TAREFA%' -ErrorAction SilentlyContinue) { exit 0 } else { exit 1 }"
if errorlevel 1 (
    echo A tarefa "%TAREFA%" nao esta' registrada nesta maquina.
    echo.
    echo Abra o MultiPC-KVM.exe e marque "Iniciar com o Windows".
    echo Isso registra a tarefa e grava a configuracao ao lado do executavel,
    echo que e' de onde o inicio automatico le' -- ele roda como SYSTEM e nao
    echo enxerga o %%APPDATA%% do seu usuario.
    echo.
    pause
    exit /b 1
)

powershell -NoProfile -Command "Start-ScheduledTask -TaskName '%TAREFA%'"
if errorlevel 1 (
    echo.
    echo Nao consegui disparar a tarefa. A mensagem acima diz por que.
    echo.
    pause
    exit /b 1
)

rem -- Dar tempo de o supervisor subir e lancar o agente.
timeout /t 3 /nobreak >nul

echo Estado da tarefa:
powershell -NoProfile -Command ^
  "$t = Get-ScheduledTask -TaskName '%TAREFA%'; Write-Host ('  ' + $t.TaskName + ' -> ' + $t.State)"
echo.
powershell -NoProfile -Command ^
  "$n = @(Get-Process -Name 'MultiPC-KVM' -ErrorAction SilentlyContinue).Count; Write-Host ('  processos no ar: ' + $n)"

echo.
echo Running = no ar. Se ficar em Ready, a tarefa disparou e saiu -- veja o
echo multipc-kvm-servico.log na pasta do executavel.
echo.
echo FECHE a janela do programa, se houver alguma aberta: com a tarefa rodando,
echo quem compartilha teclado e mouse e' o agente, noutro processo.
echo.
pause
