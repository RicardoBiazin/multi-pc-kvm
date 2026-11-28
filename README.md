# Multi PC - KVM

Um teclado e um mouse para vários PCs Windows na mesma rede. O ponteiro
atravessa a borda do monitor e passa a controlar o PC do lado; a área de
transferência (texto e imagens) fica compartilhada entre todos.

> **Chamava-se `2pc_1Kit` até a v1.2.** Ao atualizar, a configuração gravada em
> `%APPDATA%\2pc_1Kit\` é copiada sozinha para a pasta nova — inclusive a chave
> compartilhada, sem a qual os PCs não se reconhecem. Nada a fazer à mão.

Python puro sobre a API do Windows (`ctypes`), sem dependência de framework:
`pywin32`, `cryptography` e `pillow` para clipboard e cifra, `tkinter` para a
janela. Licença MIT.

| Atalho | O que faz |
|---|---|
| **Ctrl+Alt+1…9** | leva teclado e mouse direto para o N-ésimo PC da lista |
| **Ctrl+Alt+Shift+Esc** | pânico: devolve tudo ao PC onde foi apertado |
| **Ctrl+Alt+M** | liga/desliga a troca de PC pela borda (até reiniciar; o menu da bandeja grava) |
| **Ctrl+Alt+K** | modo *vários PCs*: o que você digita e clica vale em todos ao mesmo tempo |
| **Ctrl+Alt+L**, duas vezes | bloqueia a sessão de **todos** os PCs |
| **Ctrl+Alt+R** | atualiza as conexões (fecha e reconecta; no servidor, todos os clientes) |

Os quatro últimos funcionam em qualquer PC que tenha teclado ligado. Em *Opções da
troca de PC* dá para **trocar a letra** de cada um (`Ctrl+Alt+<letra>`; uma letra de A a
Z, sem repetir; vazia = sem atalho) ou desligar todos. Em teclados com AltGr,
`Ctrl+Alt+letra` pode digitar um caractere: é para isso que a letra é trocável.

## Tema claro e escuro

A janela abre no tema do Windows e tem um botão no rodapé para alternar. A
escolha fica gravada no `config.json`, no campo `tema`: `sistema` (padrão)
segue o Windows, `claro` e `escuro` mandam nele. Para voltar a seguir o
sistema, apague esse campo.

## Os dois PCs têm teclado e mouse?

Funciona nos dois sentidos (desde a v1.2). Qualquer PC do layout pode comandar
os outros: encoste o cursor **daquele** PC na borda do lado do PC que quer
controlar, e o teclado e o mouse dele passam a mandar nos dois.

- Quem comanda é sempre **um** por vez. Mexer no teclado ou no mouse do outro PC
  traz o comando de volta para ele na hora — não precisa de atalho.
- Enquanto este PC estiver **sendo comandado**, o mouse local dele continua
  solto: mexer nele não rouba o cursor de quem está do outro lado.
- **Ctrl+Alt+Shift+Esc** devolve o comando ao PC onde foi apertado, em qualquer
  situação. É a saída se algo travar.
- Se a rede cair no meio, o PC que estava comandando libera o próprio teclado
  sozinho — nunca fica bloqueado esperando.

Um cliente sem teclado e mouse próprios não muda em nada: ele simplesmente nunca
pede o comando.

**Para retomar o comando**, mexa no teclado ou no mouse deste PC. Tecla e clique
valem na hora. Movimento de mouse precisa de um **gesto** (~40 px somados), e
não de um toque: cada PC tem o seu mouse parado na mesa, e sensor óptico gera
deslocamento sozinho — antes disso um único pixel bastava, e quem estava usando
o outro PC perdia o comando no mesmo segundo em que o pegava.

## Instalação

Baixe o `MultiPC-KVM.exe` em [Releases](../../releases), ou compile do código com
`python empacotar.py`.

O mesmo `.exe` serve para todos os PCs — quem é servidor e quem é cliente sai do
layout que você monta na janela.

1. Copie o `MultiPC-KVM.exe` para cada PC e **abra em todos** (ele pede
   Administrador). Enquanto o programa está aberto ele se anuncia na rede, então
   deixe todos abertos enquanto configura.
2. Clique em **Liberar no Firewall** nos dois PCs (cria as regras de entrada
   para 24810/TCP e 24811-24812/UDP). Se aparecer a faixa laranja avisando que a rede
   é **Pública**, resolva isso primeiro: nesse modo o Windows bloqueia tudo o
   que vem de fora, mesmo com a regra criada. Vá em *Configurações > Rede e
   Internet*, clique na rede e mude para **Rede particular**, nos dois PCs.
3. Em cada PC, escolha o papel no alto da janela:
   **servidor** (o que tem o teclado e o mouse) ou **cliente**.
4. **No servidor**, clique **Gerar** e depois **Copiar** a chave. Os PCs que
   aparecerem em *Encontrados na rede* você adiciona com um duplo clique — nome
   e IP vêm preenchidos. Arraste cada um no mapa até a posição real do monitor
   dele; quem está ao lado de quem é o que define por qual borda o cursor
   atravessa. **Salvar**.
5. **Nos clientes**, basta a **mesma chave** e o IP do servidor: a lista de PCs
   e o mapa vêm prontos do servidor na primeira conexão. Se o nome deste PC não
   estiver igual ao do layout, o servidor o reconhece **pelo IP** e manda o nome
   certo.
6. **Iniciar** em todos. O servidor pode subir por último; os clientes ficam
   tentando reconectar sozinhos a cada 3 s.

O **servidor é a fonte única** do layout. Monte a lista e o mapa só nele; os
clientes adotam e gravam o que ele mandar.

Na coluna *Chave* do painel de rede: **confere** quer dizer que aquele PC está
com a mesma chave que você; **outra** avisa antes de tentar conectar e falhar.
A chave em si não trafega no anúncio — só os 8 primeiros dígitos do hash dela.

## Iniciar com o Windows (e na tela de bloqueio)

Marque **Iniciar com o Windows** na janela de configuração. Isso registra uma
tarefa agendada (`MultiPCKVM`, disparada no boot, rodando como **SYSTEM**) e
grava o `config.json` **ao lado do executável** — é preciso, porque SYSTEM não
lê o seu `%APPDATA%`. Desmarcar remove a tarefa. Instalar e remover pedem
Administrador; o `.exe` já roda elevado.

Com ela no ar, o programa:

- sobe **no boot**, antes de qualquer login;
- funciona na **tela de bloqueio**, no Ctrl+Alt+Del e sobre o prompt de UAC;
- volta sozinho se cair, e acompanha a troca de usuário.

Não é firula de arquitetura — é o único jeito de chegar lá:

| | `HKCU\...\Run` | Tarefa no boot como SYSTEM |
|---|---|---|
| Roda antes do login | não | **sim** |
| Volta sozinho depois de cair | não | **sim** — gatilhos de boot **e** de logon |
| Roda pedindo elevação | **não** — o Windows descarta em silêncio, porque não há como mostrar UAC no logon | sim, é SYSTEM: não há prompt a mostrar |
| Alcança o desktop `Winlogon` (tela de bloqueio) | não | **sim** |

### Por que tarefa agendada e não um serviço

A conta e o gatilho seriam os mesmos, mas o `.exe` é `--onefile`: nesse modo o
bootloader do PyInstaller extrai o pacote e roda o Python num processo **filho**.
O SCM vigia o processo que ele criou — o pai —, que nunca chama
`StartServiceCtrlDispatcher`, e derruba o serviço em 30 s com o **erro 1053**.
Uma tarefa agendada não exige que o processo se apresente a ninguém, então o
modelo de dois processos deixa de importar. A alternativa seria abandonar o
`--onefile` e passar a distribuir uma pasta em vez de um arquivo — e copiar
**um** `.exe` para cada PC é o jeito de instalar este programa.

**Um motor por máquina.** A janela e o início automático não podem rodar juntos:
dois jogos de hooks disputam o mesmo teclado e a mesma porta, o teclado trava e o
outro PC entra e sai numa reconexão sem fim. Ligar o início automático **para o
motor da janela** antes de registrar a tarefa, abrir a janela com a tarefa no ar
não inicia nada (ela vira só tela de configuração), e um mutex
`Global\MultiPCKVM-motor` barra qualquer caminho que escape dos dois primeiros.

**A janela não cobre a tela de bloqueio, e nunca vai cobrir.** A caixa de senha
vive no desktop `Winlogon`; nenhum processo de usuário injeta ali, elevado ou
não. Se o teclado parar de atravessar exatamente quando você clica no campo de
senha e voltar ao sair dele, é isso: o início automático não está no ar naquela
máquina, e quem responde é a janela. O log diz a causa (`o teclado e o mouse não
passam: a tela em foco é o desktop seguro...`).

**A janela oferece ligar o início automático.** Ao abrir num PC sem ele, a janela
pergunta uma vez (e "Não perguntar de novo" guarda a resposta no `config.json`),
explicando que sem ele o teclado e o mouse não passam na tela de senha. "Ligar
agora" faz o mesmo que marcar *Iniciar com o Windows*; não precisa rodar o `.bat`.

**Quanto demora na tela de bloqueio.** A cada bloqueio e desbloqueio nasce um agente novo, no desktop novo, e o mouse so' atravessa depois que ele conecta: de poucos segundos ate' ~10 s medidos (27/11/2026), a maior parte desembrulhando o `.exe`, que e' `--onefile`. O diagnostico de partida roda ao lado, fora do caminho. Tentou-se fazer o agente reaproveitar a pasta extraida pelo supervisor (v2.6.1), mas na v2.7.0 ele morreu no primeiro import com o agente de verdade; ficou **desligado** (`servico.REAPROVEITAR_EXTRACAO`) ate' alguem prova-lo assim. Se o agente morrer, `erro-fatal-agente.txt` (ao lado do `.exe`) traz o traceback, e o processo sai com codigo 1 em vez de ficar pendurado numa caixa de erro que ninguem ve'.

### O supervisor e o agente

O supervisor não captura nem injeta nada: ele vive na sessão 0, isolada do
teclado e da tela desde o Vista. Ele lança o **agente** na sessão do console,
como SYSTEM, no desktop que está recebendo o teclado, e o relança a cada troca:

```
supervisor (sessão 0, SYSTEM)
  └── agente (sessão do console, SYSTEM, desktop Default)    ← área de trabalho
  └── agente (sessão do console, SYSTEM, desktop Winlogon)   ← tela de bloqueio
```

O ícone da bandeja aparece também com o início automático: quem o criava era só
a janela, e com a tarefa no ar não há janela nenhuma. Clicar nele **abre a
janela de configuração**, e o "Sair" dele **para a tarefa** — encerrar só o
agente não adiantaria, o supervisor o relança em segundos. A tarefa continua
registrada: volta no próximo boot. Para desligar de vez, desmarque *Iniciar com
o Windows*.

Com o início automático ligado há **dois** ícones na bandeja — o do agente
(`início automático`) e o da janela (`janela`), identificados na dica de
ferramenta. Só o do agente encerra o compartilhamento; fechar a janela apenas
fecha a janela, e ela avisa isso. Ele some e volta a cada troca de
desktop, junto com o agente.

Cada troca de desktop custa uma partida nova do `.exe`, e um `--onefile`
extrai o pacote inteiro a cada partida — depois de desbloquear a tela há um
intervalo até o teclado voltar a atravessar. É o preço de manter um arquivo só.

Arquivo **copiado no Explorer** é lido por um processo do usuário logado, e não
pelo agente: o Explorer publica arquivo por OLE, com os formatos entregues sob
demanda por ele mesmo, e o agente — que é SYSTEM, outra conta — dali só enxerga
o marcador `DataObject`. Quando isso acontece o agente lança um PowerShell com o
token do usuário (`Get-Clipboard -Format FileDropList`), que lê a lista em
fração de segundo. Texto e imagem não passam por isso.

Arquivos colados de um PC para o outro são gravados no `%APPDATA%` de **quem
está logado**, e não no de quem está rodando: o agente é SYSTEM, e o `%APPDATA%`
de SYSTEM é `C:\Windows\system32\config\systemprofile\...`, pasta que o
usuário não consegue ler. Chegavam inteiros e colar dava acesso negado.

Os agentes ficam num *job object* com `KILL_ON_JOB_CLOSE`: parar a tarefa mata o
supervisor sem deixar rodar nenhum `finally` dele, e sem o job sobraria um
agente SYSTEM com os hooks instalados, vivo até o próximo boot e invisível para
quem só olha o Agendador de Tarefas.

Um processo por desktop porque hook e `SendInput` valem para **um** desktop só,
e não dá para arrastar um processo com hooks no ar de um para o outro. Quando a
tela bloqueia, o agente sai com o código 20 deixando por escrito o nome do
desktop novo (`servico-desktop.txt`, na pasta do programa) e o supervisor o faz
nascer lá. Quem lê o desktop de entrada é o agente, não o supervisor: da sessão
0 não se enxerga o desktop das outras sessões.

**Ao levar o `.exe` para o outro PC, não leve o `config.json` junto.** Ele
carrega a identidade (`este_pc`), e o arquivo ao lado do executável ganha do
`%APPDATA%` — o outro PC passa a se achar esta máquina e sobe como servidor
também. Ficam dois servidores, cada um recusando o handshake do outro, e nada
conecta. Desde a 2.1.6 o programa detecta isso pelo IP e assume a identidade
certa, registrando `IDENTIDADE:` no log; ainda assim, copie só o `.exe`.

O `config.json` ao lado do `.exe` contém a **chave compartilhada** e fica
legível para quem tem acesso àquela pasta — no `%APPDATA%` era só seu. Se isso
importar na sua máquina, ponha o `.exe` numa pasta com permissão restrita.

Para pôr no ar sem reiniciar — depois de trocar o `.exe`, por exemplo —, dê um
duplo clique em **`ligar-inicio-automatico.bat`** (vem no Release, ao lado do
`.exe`). Ele pede Administrador sozinho, confere se a tarefa existe, dispara e
mostra o estado.

Para conferir ou mexer pela linha de comando:

```
schtasks /query /tn MultiPCKVM /v /fo list
schtasks /end   /tn MultiPCKVM
schtasks /run   /tn MultiPCKVM
```

As travessias de cursor ficam em DEBUG: são várias por segundo e afogavam o
painel *Registro*. Rode com `--verboso` para trazê-las de volta. Quedas, recusas
de handshake e erros continuam no nível normal.

Cada papel escreve seu próprio log (`-servico`, `-agente`): três processos no
mesmo arquivo embaralhariam justamente o que se lê para entender uma falha de
início.

## Contagem de uso

Ao lado do *Registro* há o painel **Uso de hoje**: quantas teclas e quantos
cliques caíram em **cada PC**, e a média por minuto.

| | |
|---|---|
| **Contar** | desmarque para parar; o que já foi contado fica guardado |
| **Limpar contagem** | zera o dia |

Quem conta é o **servidor** — é o único ponto que sabe o destino de cada evento
(quando o cursor está noutro PC, para quem ele foi). Por isso a janela de um
cliente não mostra números, e diz isso em vez de exibir zeros.

Segurar uma tecla conta **um** toque, não vários: o Windows repete o `keydown`
enquanto ela fica presa, e sem essa guarda dois segundos de tecla presa viravam
dezenas de "digitações". Teclas diferentes ao mesmo tempo (Shift+C) contam as
duas, porque são toques distintos.

A média é por **minuto ativo**, não por minuto corrido: contar o dia inteiro
pelo relógio daria quase zero em qualquer jornada normal (3000 teclas das 8h às
18h viram "5 por minuto"). Contando só os minutos em que houve alguma coisa, o
número responde ao que se pergunta de verdade — o ritmo enquanto se trabalha.

A contagem é gravada em `uso.json`, na pasta do programa, e o fechamento de cada
dia vai para `uso-historico.jsonl`. Gravar em disco não é capricho: com o início
automático ligado, o agente morre e nasce a cada bloqueio de tela, e contagem só
na memória zeraria várias vezes por dia.

## Opções da troca de PC

O botão **Opções da troca de PC...** (e o menu do ícone da bandeja) controla
*quando* o cursor pode trocar de PC. Quem decide é o **servidor**, e ele repassa
as opções a todos os clientes; num cliente a janela mostra as opções como somente
leitura (exceto o cursor desenhado, que é de cada PC).

| Opção | O que faz |
|---|---|
| **Troca pela borda ligada** | desligada, encostar na borda não troca; só os atalhos `Ctrl+Alt+1…9` |
| **Bloquear os cantos** | encostar num canto (4% da borda) não troca: evita passar de PC ao buscar o botão de fechar |
| **Trocar só com Shift / Ctrl** | a borda só troca com a tecla apertada (o "Mouse Fácil" do PowerToys) |
| **Não trocar em tela cheia** | enquanto um programa cobre o monitor inteiro (jogo, Área de Trabalho Remota, apresentação) a borda não troca; **exceções** por nome de `.exe` |
| **Dar a volta** | passar da borda do último PC leva ao primeiro da fila |
| **Bloquear o protetor de tela** | os clientes não apagam a tela nem abrem o protetor enquanto conectados |
| **Compartilhar a área de transferência** | desligada, nada sai e nada é gravado (texto, imagem e arquivos) |
| **Avisos na bandeja** | balão quando a troca liga/desliga, o modo vários PCs muda, ou a conexão cai/volta |
| **Só aceitar PCs da mesma sub-rede** | o servidor recusa conexões vindas de fora das redes dele |
| **Validar o IP pelo DNS reverso** | o DNS reverso do IP tem de dizer o nome do PC (sem DNS reverso na rede, ligar isto recusa todo mundo) |
| **Desenhar o cursor** | num PC **sem mouse** o Windows não mostra o cursor; esta opção desenha uma setinha que acompanha a posição |

A cor do ícone na bandeja mostra o estado: **cinza** parado, **amarelo** no ar sem
ninguém do outro lado, **verde** conectado, **vermelho** com erro.

O **modo vários PCs** (`Ctrl+Alt+K`) só liga com o cursor e o teclado no próprio
servidor e pelo menos um PC conectado. Enquanto estiver ligado, a borda não troca
de PC (todos já recebem tudo); desligue com o mesmo atalho, ou use o pânico.

## Atualização automática

Cada PC se atualiza sozinho pelos **Releases do GitHub** (`releases/latest`, que
ignora pré-lançamentos). Em *Opções da troca de PC*, **Atualizações do programa**:

| Modo | O que faz |
|---|---|
| **só avisar** (padrão) | consulta a cada 6 h e mostra um balão; instale pelo botão **Verificar atualização** ou pelo menu do ícone |
| **instalar sozinho** | instala num momento calmo (ninguém comandando outro PC) |
| **não consultar** | nunca fala com o GitHub |

Como ela se protege:

1. **SHA-256.** O GitHub informa o hash de cada arquivo do Release; o download só vale
   se bater (e o tamanho). Sem hash, não atualiza sozinho.
2. **Conferência do pacote.** O `.exe` novo roda `--conferir-pacote` (importa tudo,
   inclusive as DLLs) antes de ocupar o lugar do atual.
3. **Troca por renomeação.** O `.exe` em uso vira `MultiPC-KVM.antigo.exe` e o novo
   ocupa o nome de sempre — a tarefa do início automático continua apontando certo.
4. **Ajudante.** Um processo à parte (`--pos-atualizacao`) para o antigo, abre o
   novo e espera ele **confirmar** que ficou de pé por ~25 s. Se não confirmar em
   2 minutos, devolve o `.exe` antigo e o abre de volta (o novo fica em
   `MultiPC-KVM.falhou.exe`, e `atualizacao-resultado.txt` conta o que houve).
   Com o início automático, o ajudante sobe por uma tarefa agendada própria
   (`MultiPC-KVM-atualizar`, apagada no fim): a principal mata tudo o que lançou.

A atualização reinicia o KVM por alguns segundos. Só o `.exe` se atualiza; do código
-fonte, use `git pull`. O primeiro `.exe` com este recurso (v2.7.0) ainda é trocado à mão.

## Segurança do `config.json`

O `config.json` guarda a **chave compartilhada**: quem a lê entra na rede do KVM e
passa a injetar teclas e cliques. Ao lado do `.exe` ele herdaria a permissão da
pasta (em `C:\Dev`, e no compartilhamento que aponta para ela, legível por outros
usuários). Desde a v2.7.0 o programa fecha o arquivo para **SYSTEM e
Administradores** ao gravá-lo e ao abrir (`config.json tinha permissão larga ...` no
log). Consequência: para ler ou editar o arquivo, abra o editor **como
Administrador**; um editor comum não o vê mais.

## Quando a conexão cai: fotografia da rede

Quando o cliente fecha a conexão por falta de sinal do servidor, ou o servidor
derruba um cliente que parou de responder, o log ganha um bloco **FOTOGRAFIA DA
REDE** com o estado naquele instante: placas e velocidade, rota padrão, o TCP do
KVM, ping ao outro PC e o que o Windows registrou nos últimos 3 minutos (o driver
do Wi-Fi, por exemplo). É para achar a causa de uma queda sem depender de testes
feitos depois, quando a rede já voltou. No máximo uma por minuto.

## Ouvir o áudio de outro PC

Na parte de baixo da janela, **Ouvir o áudio de:** tem uma caixa por PC (menos
este) e, havendo mais de um, **Todos**. Marque de quais PCs você quer ouvir o som
**neste** PC; o **Volume** ao lado vale para tudo o que se ouve aqui. O mesmo
menu existe no ícone da bandeja (**Ouvir o áudio de**), que é o jeito de mudar
quando só o início automático está no ar. A escolha fica no `config.json`.

O PC de origem **continua tocando nos próprios alto-falantes**: o programa copia
o que ele toca (WASAPI *loopback*), não o desvia.

- **Rede:** o som vai por UDP direto da origem para quem ouve, na porta **24812**
  (o botão *Liberar no Firewall* abre essa porta junto com a 24811). Não passa
  pelo canal de teclado e mouse, então um som atrasado nunca trava o cursor.
- **Cifrado:** cada pacote vai com AES-GCM, chave derivada da chave
  compartilhada. Pacote de outra chave, adulterado ou repetido é descartado.
- **Só quando pedem:** a origem só captura enquanto alguém ouve; sem ouvinte,
  não gasta CPU nem abre o dispositivo.
- **Todos:** cada origem toca numa saída própria, e o mixer do Windows soma.
- **Custo:** PCM sem compressão, cerca de 1,5 Mbit/s por origem. Em rede local
  é folgado; por Wi-Fi fraco pode picotar.
- **Atraso:** na ordem de 60 a 100 ms. Bom para vídeo, música e chamada; ruim
  para jogo.

**Limites:** o formato (taxa e canais) é o do PC de origem. Som surround (5.1,
7.1) vira estéreo. Se os dois PCs usam taxas diferentes (44,1 e 48 kHz) a saída
de quem ouve pode se recusar a abrir; o motivo aparece no Registro
(*"nao consegui abrir a saida"*). Enquanto a origem está muda não chega pacote
nenhum: é normal, o loopback do Windows não entrega silêncio.

## PC com mais de um monitor

Cada PC ocupa **uma** célula no mapa, mesmo tendo vários monitores: a travessia
acontece nas beiradas do conjunto todo, não entre os monitores de um mesmo PC —
entre eles o cursor passa normalmente, como já faz no Windows.

O cuidado que isso exige: com monitores de tamanhos diferentes ou desalinhados,
o retângulo que envolve todas as telas tem **pedaços que não existem em tela
nenhuma**. Exemplo, uma tela 1920×1080 em (0,0) e outra 1280×1024 em (1920,300):

```
 0                1920           3200
 ┌──────────────────┬──────────────┐  0
 │                  │ ▓▓▓▓ buraco  │
 │       tela A     ├──────────────┤  300
 │    1920x1080     │    tela B    │
 ├──────────────────┤  1280x1024   │  1080
 │ ▓▓▓ buraco ▓▓▓▓▓ │              │
 └──────────────────┴──────────────┘  1324
```

O Windows não deixa o cursor entrar nesses pedaços. Se o programa calculasse uma
posição ali, o cursor real seria preso em outro lugar e a posição que ele guarda
passaria a **divergir da real** — o ponteiro apareceria deslocado e as bordas
disparariam na hora errada. Então toda posição calculada é puxada para a tela
mais próxima antes de ser usada, na chegada e a cada movimento.

O pulo por `Ctrl+Alt+N` cai no **centro do monitor principal**, não no centro do
retângulo — que pode ser um buraco, ou a divisa exata entre duas telas.

O relatório lista os monitores e avisa quanto do retângulo está vazio. Uma
consequência a saber: empurrando o cursor para o lado onde há um buraco, ele
para na beirada da tela atual em vez de atravessar para o outro PC — a travessia
vale nas beiradas do retângulo. Mova até a borda real da tela.

## Máquina com Wi-Fi e cabo

Se o PC tem mais de uma placa, o campo **IP** vira uma lista com todas, dizendo
qual é qual (`192.168.1.10 (Ethernet — cabo)`, `192.168.2.20 (Wi-Fi)`). Escolha
a placa que está na **mesma rede do outro PC** — Wi-Fi e cabo costumam estar em
sub-redes diferentes que não se enxergam.

A busca na rede transmite por **todas** as placas ativas, então o outro PC
aparece esteja ele de que lado estiver. Placas virtuais (VPN, loopback,
Hyper-V) e endereços `169.254.x.x` são ignorados.

O botão **Testar conexão** tenta um TCP no PC selecionado e diz o que houve:
outra sub-rede, Firewall barrando, ninguém ouvindo naquela porta, ou tudo certo.

A configuração fica em `%APPDATA%\MultiPC-KVM\config.json`. Se você puser um
`config.json` ao lado do `.exe`, ele ganha — é o jeito de levar a configuração
pronta num pendrive (lembre de trocar o `este_pc` em cada máquina).

## O mapa de posições

Cada PC ocupa uma célula. O vizinho numa direção é o PC mais próximo daquele
lado, na mesma linha ou coluna — buracos no meio não atrapalham. Funciona nas
quatro direções, então dá para pôr um PC acima ou abaixo do outro.

```
   ┌─────────┐  ┌─────────┐  ┌─────────┐
   │  CASA   │  │  SALA   │  │ QUARTO  │      cursor sai pela direita da SALA
   │         │  │(teclado)│  │         │      e aparece no QUARTO; continuando
   └─────────┘  └─────────┘  └─────────┘      para a direita, fica no QUARTO
```

Um PC que não faz fronteira com nenhum outro fica inalcançável — a janela avisa
antes de deixar iniciar.

## Empacotar

```
python empacotar.py
```

Gera `dist\MultiPC-KVM.exe` (arquivo único, ~35 MB, com pedido de Administrador
embutido). Precisa de `pip install pyinstaller pystray`.

Se o programa estiver aberto na hora de reconstruir, o `.exe` fica travado — e
como ele roda elevado, nem dá para encerrá-lo de um terminal comum. Nesse caso o
empacotador avisa e gera em `dist-novo\` em vez de falhar; feche o programa e
substitua o arquivo.

Sem elevação o Windows bloqueia captura e injeção sobre janelas elevadas
(Gerenciador de Tarefas, instaladores) — daí o pedido de Administrador.

## Como funciona

| Arquivo | O que faz |
|---|---|
| `app.py` | Ponto de entrada: abre a janela, ou `--sem-janela` para subir direto |
| `interface.py` | Janela tkinter: papel, lista de PCs, mapa arrastável, rede, log |
| `descoberta.py` | Farol UDP: anuncia este PC e acha os outros na LAN |
| `audio.py` | Áudio entre PCs: captura WASAPI, UDP cifrado (AES-GCM), saída por origem |
| `politica.py` | Regras da troca de PC (cantos, Shift/Ctrl, tela cheia, ligar/desligar) e atalhos de ação |
| `cursor_virtual.py` | Setinha desenhada por janela transparente, para PC sem mouse |
| `atualizacao.py` | Atualização pelos Releases: SHA-256, troca por renomeação, ajudante com desfazer |
| `redes.py` | Placas via `GetAdaptersAddresses`: IP, Wi-Fi/cabo, máscara, broadcast |
| `diagnostico.py` | Teste de conexão e regras de Firewall |
| `relatorio.py` | Relatório de diagnóstico em arquivo, para compartilhar |
| `motor.py` | Liga/desliga o papel certo numa thread |
| `configuracao.py` | config.json, IP local, início automático com o Windows |
| `layout.py` | Posições, vizinhança nas 4 direções, geometria de entrada/saída |
| `borda.py` | Roteamento do cursor e de quem comanda (roda dentro do hook) |
| `alvo.py` | Lado que **recebe** o cursor: posição virtual, trava e saída |
| `entrada_win.py` | Win32 via ctypes: hooks `WH_MOUSE_LL`/`WH_KEYBOARD_LL` e `SendInput` |
| `protocolo.py` | TCP com frames de tamanho fixo, handshake HMAC e cifra Fernet |
| `clipboard_win.py` | Clipboard: texto `CF_UNICODETEXT`, imagem `CF_DIB` ↔ PNG |
| `servidor.py`, `cliente.py` | Os dois papéis |
| `bandeja.py` | Ícone na bandeja (opcional, via pystray) |
| `aviso.py` | Tarja na tela e bip ao trocar de PC |

Em modo remoto os hooks **engolem** os eventos, então nada chega às aplicações
locais. O **movimento** do cursor remoto vem do **Raw Input** (`WM_INPUT`), que
entrega deslocamento relativo; o hook não serve para isso porque só informa
posição absoluta, já presa aos limites da tela — encostado numa borda ela para
de variar. Quem decide o roteamento é sempre o servidor: o cliente só avisa "saí
por tal aresta, nesta altura" e espera a resposta.

A aresta por onde o cursor acabou de entrar fica travada até ele se afastar
40 px dela ou passar 0,6 s — sem isso, um tremor de mão o devolvia antes de dar
para ver.

O servidor guarda duas coisas separadas: de quem é o **cursor** (`atual`) e de
quem é o **teclado e o mouse** (`comandante`). Até a v1.1 o comandante era sempre
o servidor. Agora um cliente pode pedir o comando, e aí os papéis se invertem —
o servidor passa a usar o mesmo `alvo.Alvo` do cliente e recebe o input pela
rede. O roteamento continua todo no servidor, que é o que impede os dois lados de
divergirem. Não há risco de laço entre a injeção e a captura: o hook descarta
tudo o que vem com `LLMHF_INJECTED`, que inclui o nosso próprio `SetCursorPos`.

Um **vigia** compara o Raw Input (que chega por mensagem de janela) com os hooks:
se o Raw Input está recebendo movimento e os hooks não, o Windows os descartou em
silêncio — o que ele faz quando o callback passa de ~300 ms — e o vigia
reinstala. Sem isso o programa fica aberto sem funcionar e sem dizer por quê.

## Quando não funcionar

**`'X' desconectou: [WinError 10038] ... não é um soquete`** logo depois de
`parou de responder` — a segunda linha é *consequência* da primeira, não uma
falha de rede: o watchdog fecha o socket de propósito, e fechar é como se acorda
um `receber` pendurado. Desde a 2.2.6 a queda é relatada pela causa
(`derrubado por nós: ...`) e o watchdog informa o silêncio **medido**, não o
limite. O que investigar é por que o outro PC ficou mudo.

**`FileNotFoundError ... _MEIxxxxxase_library.zip`** — o pacote do `.exe`
(que o `--onefile` extrai no `%TEMP%`) foi apagado com o programa rodando, por
alguma limpeza de temporários. **Feche e abra**: ele se extrai de novo. Desde a
2.2.5 o programa detecta isso ao abrir e escreve `PACOTE INCOMPLETO` no log em
vez de quebrar mais adiante.


No painel *Registro*, o botão **Gerar relatório para compartilhar** cria um
`.txt` **na pasta onde o programa está** com tudo o que serve para achar o
problema:
erros do log destacados no topo, configuração, placas de rede, perfis do
Windows, regras de Firewall, PCs encontrados e um teste de conexão com cada PC
do layout. O caminho já vai para a área de transferência.

**O relatório não contém a chave compartilhada** — só os 8 dígitos do hash dela,
que é o que permite comparar os dois PCs sem expor nada.

Também dá para gerar sem abrir a janela:

```
MultiPC-KVM.exe --relatorio
```

Para copiar só o log: **Copiar registro**, ou selecione com o mouse e Ctrl+C
(Ctrl+A seleciona tudo).

### Outro programa de teclado/mouse compartilhado no ar

**A causa mais traiçoeira.** Mouse Without Borders (PowerToys), Synergy,
Deskflow, Input Leap, Barrier, ShareMouse, Multiplicity e Input Director fazem a
mesma coisa que este programa e prendem os mesmos hooks. Com um deles rodando, o
`SendInput` **devolve sucesso e o Windows descarta o evento**: o cliente aceita o
controle, injeta, e nada aparece na tela.

O programa detecta isso ao iniciar e escreve `CONFLITO: ...` no log, além de
testar a injeção de verdade (move o cursor 4 px e confere se andou). Feche o
outro programa — os dois juntos não funcionam.

### Nome diferente

O nome de cada PC tem de ser **exatamente igual nos dois lados**. Se o cliente
se chama `esq` e no layout do servidor ele está como `PC-esq`, o servidor
recusa. Adicionar pelo painel *Encontrados na rede* evita isso, porque o nome
vem pronto. O cliente agora mostra o motivo da recusa e a lista de nomes que o
servidor conhece.

### Chave diferente

É o erro mais comum e o mais silencioso: a rede funciona, a conexão chega, e o
handshake é recusado. O log de ambos os lados agora diz isso com todas as
letras, e o hash da chave aparece no início do log de cada PC — se os oito
dígitos diferem, é isso.

## Testes

```
python teste_local.py     # clipboard, layout, hooks, injeção, roteamento
python teste_injecao.py   # digitação e mouse no PC cliente (precisa dos 2 PCs)
```

`teste_local.py` pula a parte de injeção se a sessão estiver bloqueada ou não
for um desktop interativo — o Windows recusa `SetCursorPos`/`SendInput` nessas
condições.

## Limitações conhecidas

- Windows ↔ Windows apenas.
- Na tela de bloqueio vale o mesmo layout da área de trabalho, mas a área de
  transferência não atravessa: o desktop `Winlogon` tem a sua, separada.
- Um servidor; os demais são clientes. Qualquer um deles pode **comandar**, mas
  o roteamento passa sempre pelo servidor: com ele fechado, ninguém comanda.
- A **porta TCP** tem de ser a mesma em todos os PCs. Se divergir, os PCs se
  enxergam na busca da rede mas nunca conectam; o painel *Encontrados na rede* e
  o relatório avisam quando isso acontece.
- A busca na rede é por broadcast UDP: só enxerga PCs na **mesma sub-rede**.
  Entre VLANs ou por VPN, adicione os PCs à mão pelo IP.
- Arquivos e pastas atravessam pela área de transferência (copiar num PC, colar no
  outro), até **500 MB** por colagem. Não há arrastar e soltar entre os PCs.
- Imagens acima de 8 MB (já em PNG) são descartadas.
- O cursor desenhado (PC sem mouse) é uma janela transparente: não aparece sobre
  telas seguras (UAC, Ctrl+Alt+Del) nem sobre alguns jogos em tela cheia exclusiva.
- Em modo remoto o cursor do servidor fica parado e visível no meio do monitor
  principal.
- Eventos de mouse *injetados* por outros programas não atravessam a borda
  (o hook os descarta, junto com os nossos próprios).

## Onde ficam os arquivos

| O quê | Onde |
|---|---|
| Log | pasta do programa — `multipc-kvm-<nome do PC>.log`; o supervisor e o agente escrevem em `multipc-kvm-servico.log` e `multipc-kvm-<nome>-agente.log` |
| Relatórios | pasta do programa — `<papel>-<nome do PC>-<data>.txt` |
| Configuração | `%APPDATA%\MultiPC-KVM\config.json` — ou ao lado do `.exe`, que tem prioridade e é o que o início automático lê |

Log e relatórios ficam **ao lado do executável**, que é onde se procura. Se essa
pasta não aceitar escrita (`.exe` numa pasta protegida, pendrive travado), os
dois caem para `%APPDATA%\MultiPC-KVM\` em vez de impedir o programa de abrir — a
primeira linha do log diz qual pasta está em uso. O botão **Abrir pasta**, no
painel *Registro*, abre a pasta certa nos dois casos.

A configuração continua em `%APPDATA%` para sobreviver à troca do executável;
um `config.json` colocado ao lado do `.exe` tem prioridade, para levar tudo
pronto num pendrive — e é onde **Iniciar com o Windows** grava a configuração,
já que o supervisor roda como SYSTEM e não lê o `%APPDATA%` do usuário.
