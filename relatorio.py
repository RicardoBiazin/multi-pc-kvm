"""Gera um arquivo unico com tudo o que serve para diagnosticar um problema.

O objetivo e' o usuario poder mandar **um** arquivo em vez de descrever o que
esta' vendo. Reune configuracao, placas de rede, perfis do Windows, regras de
Firewall, PCs achados, testes de conexao e o fim do log -- com os erros
destacados logo no comeco.

A **chave compartilhada nunca entra no relatorio** (so' os 8 digitos do hash
dela, que ja' e' o que trafega na rede). O arquivo leva nomes de PC, IPs
privados e nomes de placas: e' o necessario para diagnosticar rede.
"""

from __future__ import annotations

import datetime
import logging
import pathlib
import platform
import sys

import configuracao as conf
import descoberta
import diagnostico
import entrada_win
import layout as lay
import redes

log = logging.getLogger("relatorio")

LINHAS_DE_LOG = 400
PADROES_DE_ERRO = ("ERROR", "WARNING", "CRITICAL", "Traceback", "Exception",
                   "FALHOU", "falha", "recusad", "nao consegui")


def _cabecalho() -> list[str]:
    agora = datetime.datetime.now().strftime("%d/%m/%Y %H:%M:%S")
    return [
        "=" * 78,
        f"{conf.APP} v{conf.VERSAO} -- relatorio de diagnostico",
        f"desenvolvido por {conf.AUTOR}",
        f"gerado em {agora}",
        "=" * 78,
        "",
        "Este arquivo NAO contem a chave compartilhada.",
        "Contem: nomes de PC, IPs privados, nomes de placas de rede e o log.",
        "",
    ]


def _secao(titulo: str) -> list[str]:
    return ["", "-" * 78, titulo.upper(), "-" * 78]


def _sistema() -> list[str]:
    linhas = _secao("sistema") + [
        f"Windows      : {platform.platform()}",
        f"Maquina      : {platform.node()}",
        f"Python       : {sys.version.split()[0]}",
        f"Empacotado   : {'sim (.exe)' if getattr(sys, 'frozen', False) else 'nao'}",
        f"Administrador: {'sim' if diagnostico.elevado() else 'NAO'}",
        f"Executavel   : {sys.executable}",
    ]

    linhas += _secao("monitores")
    telas = [m[:4] for m in entrada_win.monitores()]
    x0, y0, largura, altura = entrada_win.geometria_virtual()
    linhas.append(f"  Retangulo do desktop: {largura}x{altura} em ({x0},{y0})")
    for x, y, w, h, principal in entrada_win.monitores():
        linhas.append(f"    {w}x{h} em ({x},{y})"
                      + ("   <- principal" if principal else ""))
    area_das_telas = sum(w * h for _, _, w, h in telas)
    if len(telas) > 1 and area_das_telas < largura * altura:
        vazio = 100 * (1 - area_das_telas / (largura * altura))
        linhas.append(f"  {vazio:.0f}% do retangulo NAO esta' em tela nenhuma "
                      f"(monitores desalinhados ou de tamanhos diferentes).")
        linhas.append("  O cursor e' puxado para a tela mais proxima nesses "
                      "pedacos; a travessia de borda so' vale nas beiradas do "
                      "retangulo.")

    linhas += _secao("teclado e mouse")
    conflitos = diagnostico.programas_conflitantes()
    if conflitos:
        linhas.append(f"  CONFLITO: {', '.join(conflitos)} esta' rodando.")
        linhas.append("  Faz a mesma coisa que este programa e disputa os mesmos")
        linhas.append("  hooks; com ele no ar a injecao de mouse nao tem efeito.")
    else:
        linhas.append("  nenhum outro programa de teclado/mouse compartilhado")

    try:
        ok, detalhe = entrada_win.injecao_funciona()
        linhas.append(f"  Injecao de mouse: {'OK' if ok else 'FALHOU'} -- {detalhe}")
    except Exception as exc:
        linhas.append(f"  Injecao de mouse: nao consegui testar ({exc})")
    return linhas


def _configuracao(cfg: dict) -> list[str]:
    linhas = _secao("configuracao") + [
        f"Arquivo      : {conf.caminho_config()}",
        f"Este PC e'   : {cfg.get('este_pc')}",
        f"Porta TCP    : {cfg.get('porta')}",
        f"Porta UDP    : {descoberta.PORTA} (busca na rede)",
        f"Chave        : {descoberta.impressao_da_chave(cfg.get('chave', ''))}"
        f"  (so' o hash -- confira se e' igual no outro PC)",
        f"Busca na rede: {'ligada' if cfg.get('descoberta', True) else 'desligada'}",
        "",
        "PCs no layout (coluna,linha = posicao no mapa):",
    ]
    for pc in cfg.get("pcs", []):
        papel = "servidor" if pc.get("servidor") else "cliente"
        marca = "  <== este PC" if pc.get("nome") == cfg.get("este_pc") else ""
        linhas.append(f"  {pc.get('nome',''):<20} {pc.get('ip',''):<16} "
                      f"{papel:<9} celula ({pc.get('coluna')},{pc.get('linha')})"
                      f"{marca}")

    problemas = lay.Layout.de_config(cfg).validar()
    linhas.append("")
    if problemas:
        linhas.append("PROBLEMAS NO LAYOUT:")
        linhas += [f"  - {p}" for p in problemas]
    else:
        linhas.append("Layout: sem problemas.")
    return linhas


def _rede() -> list[str]:
    linhas = _secao("placas de rede desta maquina")
    placas = redes.listar()
    if not placas:
        linhas.append("  NENHUMA placa utilizavel encontrada.")
    for p in placas:
        linhas.append(f"  {p.ip:<16} /{p.prefixo:<3} {p.tipo:<8} "
                      f"broadcast {p.broadcast:<16} {p.nome}")

    descartadas = [p for p in redes.listar(so_reais=False) if p not in placas]
    if descartadas:
        linhas += ["", "  Descartadas (virtuais ou sem IP valido):"]
        for p in descartadas:
            motivo = "virtual" if p.virtual else "APIPA/sem DHCP"
            linhas.append(f"    {p.ip:<16} /{p.prefixo:<3} {motivo:<15} {p.nome}")

    linhas += _secao("perfis de rede do windows")
    perfis = diagnostico.perfis_de_rede()
    if not perfis:
        linhas.append("  nao consegui consultar (Get-NetConnectionProfile)")
    for nome, categoria in perfis:
        alerta = ("   <== PUBLICA: o Windows bloqueia a busca e as conexoes de "
                  "entrada" if categoria == "Public" else "")
        linhas.append(f"  {nome:<34} {categoria}{alerta}")

    linhas += _secao("firewall")
    linhas.append(f"  Regras do 2pc_1Kit criadas: "
                  f"{'sim' if diagnostico.regras_existem() else 'NAO'}")
    return linhas


def _achados(farol) -> list[str]:
    linhas = _secao("pcs encontrados na rede")
    if farol is None:
        linhas.append("  nao disponivel: o farol so' roda com o programa aberto")
        linhas.append("  (gerando pela janela, esta secao vem preenchida)")
        return linhas
    lista = farol.lista()
    if not lista:
        linhas.append("  NENHUM. Se o outro PC esta' aberto, verifique: mesma")
        linhas.append("  sub-rede, rede marcada como Particular, e Firewall liberado.")
    for d in lista:
        versao = d.get("versao", "?")
        alerta = "  <- versao diferente da deste PC" if versao != conf.VERSAO else ""
        linhas.append(f"  {d['nome']:<20} {d['ip']:<16} {d['papel']:<9} "
                      f"v{versao:<6} chave {d['chave']}  outros IPs: "
                      f"{', '.join(d.get('ips', [])) or '-'}{alerta}")
    return linhas


def _testes(cfg: dict) -> list[str]:
    linhas = _secao("testes de conexao")
    eu = cfg.get("este_pc")
    alvos = [p for p in cfg.get("pcs", []) if p.get("nome") != eu]
    if not alvos:
        linhas.append("  nenhum outro PC no layout para testar")
    for pc in alvos:
        ok, mensagem = diagnostico.testar(pc.get("ip", ""),
                                          int(cfg.get("porta", 24810)), prazo=2.5)
        linhas.append(f"  {pc.get('nome')} ({pc.get('ip')}): "
                      f"{'OK' if ok else 'FALHOU'}")
        linhas.append(f"      {mensagem}")
    porta = int(cfg.get("porta", 24810))
    linhas += ["", f"  Porta {porta} ocupada nesta maquina: "
                   f"{'sim (servidor no ar)' if diagnostico.escutando(porta) else 'nao'}"]
    return linhas


def _log() -> tuple[list[str], list[str]]:
    """(fim do log, linhas que parecem erro)."""
    caminho = conf.caminho_log()
    if not caminho.exists():
        return ["  (sem arquivo de log ainda)"], []
    try:
        linhas = caminho.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        return [f"  (nao consegui ler {caminho}: {exc})"], []
    erros = [l for l in linhas if any(p in l for p in PADROES_DE_ERRO)]
    return linhas[-LINHAS_DE_LOG:], erros[-60:]


def gerar(cfg: dict, farol=None, destino: pathlib.Path | None = None) -> pathlib.Path:
    """Monta o relatorio e grava. Devolve o caminho do arquivo."""
    fim_do_log, erros = _log()

    partes = _cabecalho()
    partes += _secao("erros e avisos encontrados")
    if erros:
        partes.append(f"  ({len(erros)} linha(s); o log completo esta' no fim)")
        partes += [f"  {l}" for l in erros]
    else:
        partes.append("  nenhum erro registrado no log")

    partes += _sistema()
    partes += _configuracao(cfg)
    partes += _rede()
    partes += _achados(farol)
    partes += _testes(cfg)
    partes += _secao(f"log (ultimas {LINHAS_DE_LOG} linhas)")
    partes += fim_do_log
    partes += ["", "=" * 78, "fim do relatorio", "=" * 78]

    if destino is None:
        # O nome ja' diz de quem e' o relatorio: com dois arquivos na mao, saber
        # qual e' o servidor e qual e' o cliente e' a primeira coisa necessaria.
        eu = cfg.get("este_pc", "sem-nome")
        sou_servidor = any(p.get("nome") == eu and p.get("servidor")
                           for p in cfg.get("pcs", []))
        papel = "servidor" if sou_servidor else "cliente"
        marca = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        destino = (conf.pasta_de_saida()
                   / f"{papel}-{conf.nome_de_arquivo(eu)}-{marca}.txt")
    destino.write_text("\n".join(partes), encoding="utf-8")
    log.info("relatorio gravado em %s", destino)
    return destino
