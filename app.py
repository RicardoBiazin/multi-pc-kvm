"""Multi PC - KVM -- um teclado e um mouse para varios PCs Windows na mesma rede.

Sem argumentos abre a janela de configuracao. O mesmo executavel serve para
todos os PCs: quem e' servidor e quem e' cliente sai do layout, e cada maquina
so' precisa saber qual da lista e' ela ("Este PC e'").

    MultiPC-KVM.exe                 janela de configuracao
    MultiPC-KVM.exe --sem-janela    sobe direto pelo config gravado
    MultiPC-KVM.exe --sem-captura   servidor so' com area de transferencia (teste)

Os dois modos abaixo sao chamados pelo Windows, nao por gente (ver servico.py):

    MultiPC-KVM.exe --servico       o supervisor do inicio automatico (sessao 0)
    MultiPC-KVM.exe --agente        o motor num desktop, lancado pelo supervisor

Atalho de panico: Ctrl+Alt+Shift+Esc devolve teclado e mouse ao servidor.
"""

from __future__ import annotations

import argparse
import logging
import sys
import threading

import configuracao as conf
import entrada_win as ew


def configurar_log(nome_do_pc: str, verboso: bool, parte: str = "") -> None:
    manipuladores: list[logging.Handler] = [
        logging.FileHandler(conf.caminho_log(nome_do_pc, parte), encoding="utf-8")
    ]
    if sys.stdout is not None:  # no .exe sem console, stdout nao existe
        manipuladores.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(
        level=logging.DEBUG if verboso else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)-10s %(message)s",
        datefmt="%H:%M:%S",
        handlers=manipuladores,
    )
    logging.getLogger("PIL").setLevel(logging.WARNING)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--sem-janela", action="store_true",
                   help="roda pelo config gravado, sem abrir janela "
                        "(o log vai so' para o arquivo)")
    p.add_argument("--sem-captura", action="store_true",
                   help="servidor sem hooks: so' area de transferencia (teste)")
    p.add_argument("--servico", action="store_true",
                   help=argparse.SUPPRESS)  # a tarefa chama; ver servico.py
    p.add_argument("--agente", action="store_true",
                   help=argparse.SUPPRESS)  # o supervisor chama; ver servico.py
    p.add_argument("--desktop", default="",
                   help=argparse.SUPPRESS)  # em que desktop nos puseram
    p.add_argument("--relatorio", action="store_true",
                   help="gera o relatorio de diagnostico e sai")
    p.add_argument("-v", "--verboso", action="store_true")
    args = p.parse_args()

    # A config vem antes do log: e' dela que sai o nome do PC no arquivo.
    if args.servico:
        # Antes de qualquer outra coisa: nada aqui embaixo -- DPI, monitores,
        # placas de rede -- diz respeito a quem vive na sessao 0.
        configurar_log("", args.verboso, "servico")
        import servico
        return servico.rodar_como_servico()

    cfg = conf.carregar()
    configurar_log(cfg.get("este_pc", ""), args.verboso,
                   "agente" if args.agente else "")
    ew.ativar_dpi()  # antes de qualquer leitura de coordenada

    cfg["capturar"] = not args.sem_captura
    log = logging.getLogger("app")
    x0, y0, largura, altura = ew.geometria_virtual()
    telas = ew.monitores()
    log.info("%s v%s (por %s) | este PC: '%s' | desktop virtual %dx%d em "
             "(%d,%d) | %d monitor(es): %s", conf.APP, conf.VERSAO, conf.AUTOR,
             cfg.get("este_pc"), largura, altura, x0, y0, len(telas),
             ", ".join(f"{w}x{h} em ({x},{y})" + ("*" if p else "")
                       for x, y, w, h, p in telas))
    area = sum(w * h for _, _, w, h, _ in telas)
    if len(telas) > 1 and area < largura * altura:
        log.info("%.0f%% do retangulo do desktop nao esta' em tela nenhuma; o "
                 "cursor sera' puxado para a tela mais proxima nesses pedacos",
                 100 * (1 - area / (largura * altura)))
    log.info("configuracao: %s", conf.caminho_config())
    log.info("log e relatorios: %s", conf.pasta_de_saida())
    if conf.migrar_inicio_automatico():
        log.info("inicio automatico apontava para a versao anterior "
                 "(2pc_1Kit.exe); passou a apontar para este executavel")

    import servico
    # So' a janela faz essa limpeza: o agente roda como SYSTEM e nao tem nada
    # que consultar o Agendador de Tarefas por COM a cada nascimento.
    if not args.agente and servico.instalado() and conf.inicio_automatico():
        # Sobra da epoca do registro. Nunca chegou a rodar (o Windows descarta
        # entrada de Run que pede UAC), mas se um dia rodasse seriam dois
        # motores brigando pelos mesmos hooks depois do login.
        conf.definir_inicio_automatico(False)
        log.info("apaguei a entrada antiga de inicio automatico no registro: "
                 "quem sobe o programa agora e' a tarefa agendada")

    import descoberta as _desc
    import diagnostico
    # O hash da chave e' o jeito de conferir se os dois PCs tem a mesma sem
    # mostrar a chave: se estes 8 digitos diferem, o handshake vai falhar.
    log.info("chave (hash): %s", _desc.impressao_da_chave(cfg.get("chave", ""))
             or "NAO DEFINIDA")
    log.info("administrador: %s | placas de rede: %s",
             "sim" if diagnostico.elevado() else "NAO",
             diagnostico.resumo_das_placas())
    conflitos = diagnostico.programas_conflitantes()
    if conflitos:
        log.error("CONFLITO: %s esta' rodando e faz a mesma coisa que este "
                  "programa. Os dois disputam os hooks de mouse e teclado e "
                  "nenhum funciona direito -- feche um dos dois.",
                  ", ".join(conflitos))
    publicas = diagnostico.redes_publicas()
    if publicas:
        log.warning("rede '%s' marcada como Publica: o Windows bloqueia a busca "
                    "na rede e as conexoes de entrada nesse modo",
                    ", ".join(sorted(set(publicas))))

    if args.relatorio:
        import relatorio
        caminho = relatorio.gerar(cfg)
        log.info("relatorio gravado em %s", caminho)
        print(caminho)
        return 0

    # O farol fica no ar enquanto o programa estiver aberto, mesmo antes de
    # Iniciar: e' assim que os outros PCs conseguem enxergar este aqui.
    farol = None
    if cfg.get("descoberta", True):
        import descoberta
        farol = descoberta.Farol(descoberta.descritor(cfg))
        farol.start()

    if args.agente:
        import servico
        log.info("modo agente, desktop pedido: %s", args.desktop or "?")
        return servico.rodar_agente(cfg)

    if not args.sem_janela:
        import interface
        interface.abrir(cfg, farol)
        return 0

    import motor
    m = motor.Motor(cfg)
    try:
        m.iniciar(cfg)
    except ValueError as exc:
        log.error("configuracao incompleta: %s", exc)
        log.error("abra o programa sem --sem-janela para configurar")
        return 2
    try:
        while m.ativo():
            threading.Event().wait(0.5)
    except KeyboardInterrupt:
        log.info("encerrado pelo usuario")
    finally:
        m.parar()
    return 0


if __name__ == "__main__":
    sys.exit(main())
