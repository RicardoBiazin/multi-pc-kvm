"""Atualizacao do proprio .exe pelos Releases do GitHub.

Ate' a v2.6 cada versao nova exigia fechar o programa nos dois PCs, trocar o
.exe a' mao e abrir de novo. Aqui cada PC se atualiza sozinho, e com cuidado:

1. **Descobre** a ultima versao em `releases/latest` (que ignora pre-release, e
   foi assim que a v2.4.0 quebrada deixou de ser oferecida).
2. **Baixa e confere**: o GitHub entrega o SHA-256 de cada arquivo do Release;
   o download so' vale se bater. Depois o .exe novo roda `--conferir-pacote`
   (importa tudo, inclusive as DLLs) -- a mesma prova que o build exige.
3. **Troca** renomeando: um .exe em uso nao se sobrescreve, mas se renomeia.
   `MultiPC-KVM.exe` vira `.antigo.exe` e o novo ocupa o nome certo -- assim a
   tarefa do inicio automatico e o atalho continuam apontando para o mesmo lugar.
4. **Reinicia por um ajudante** (`--pos-atualizacao`), que para o antigo, abre o
   novo e espera ele CONFIRMAR que ficou de pe' por alguns segundos. Se nao
   confirmar, o ajudante devolve o .exe antigo e o abre de volta.

O ajudante e' outro processo porque quem se atualiza morre no meio: a tarefa de
inicio automatico mata o supervisor e tudo o que ele lancou. Com o inicio
automatico ele sobe por uma tarefa agendada propria (fora do job da principal).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import pathlib
import subprocess
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass

import configuracao as conf

log = logging.getLogger("atualizacao")

REPOSITORIO = "RicardoBiazin/multi-pc-kvm"
URL_DA_API = f"https://api.github.com/repos/{REPOSITORIO}/releases/latest"
PREFIXO_DO_DOWNLOAD = f"https://github.com/{REPOSITORIO}/releases/download/"
NOME_DO_ASSET = f"{conf.APP_ARQUIVO}.exe"

ARQ_NOVO = f"{conf.APP_ARQUIVO}.novo.exe"
ARQ_ANTIGO = f"{conf.APP_ARQUIVO}.antigo.exe"
ARQ_FALHOU = f"{conf.APP_ARQUIVO}.falhou.exe"
ARQ_ESTADO = "atualizacao.json"
ARQ_CONFIRMACAO = "atualizacao-ok.txt"
ARQ_RESULTADO = "atualizacao-resultado.txt"
TAREFA_AUXILIAR = f"{conf.APP_ARQUIVO}-atualizar"
TAREFA_PRINCIPAL = conf.APP_ARQUIVO.replace("-", "")  # "MultiPCKVM", como em servico.py

INTERVALO_DE_CHECAGEM = 6 * 3600   # s entre consultas ao GitHub
ESPERA_INICIAL = 90                # s depois de abrir, antes da primeira consulta
CONFIRMAR_APOS = 25                # s de vida do novo para dizer "estou bem"
ESPERA_DA_CONFIRMACAO = 120        # s que o ajudante espera por ela
ESPERA_DO_ANTIGO = 20              # s ate' o antigo sair sozinho; depois e' morto

MODOS = ("avisar", "automatica", "desligada")


class ErroDeAtualizacao(Exception):
    """Algo impediu a atualizacao; o programa continua na versao de antes."""


@dataclass
class Release:
    versao: str            # "2.6.1", sem o "v"
    url: str
    tamanho: int
    sha256: str            # em minusculas; "" se o GitHub nao informou


# -- descobrir ---------------------------------------------------------------


def versao_em_tupla(texto: str) -> tuple[int, ...] | None:
    """"v2.6.1" -> (2, 6, 1). None se nao for numero.numero.numero."""
    texto = (texto or "").strip().lstrip("vV")
    partes = texto.split(".")
    if len(partes) != 3 or not all(p.isdigit() for p in partes):
        return None
    return tuple(int(p) for p in partes)


def e_mais_nova(versao: str, atual: str = conf.VERSAO) -> bool:
    nova, velha = versao_em_tupla(versao), versao_em_tupla(atual)
    return nova is not None and velha is not None and nova > velha


def _abrir_url(url: str, prazo: float = 20.0):
    pedido = urllib.request.Request(url, headers={
        "User-Agent": f"{conf.APP_ARQUIVO}/{conf.VERSAO}",
        "Accept": "application/vnd.github+json"})
    return urllib.request.urlopen(pedido, timeout=prazo)


def interpretar_release(dados: dict) -> Release | None:
    """O arquivo do .exe dentro do JSON do GitHub, ou None se nao serve."""
    if dados.get("draft") or dados.get("prerelease"):
        return None
    versao = str(dados.get("tag_name", "")).strip().lstrip("vV")
    if versao_em_tupla(versao) is None:
        return None
    for asset in dados.get("assets") or []:
        if asset.get("name") != NOME_DO_ASSET:
            continue
        url = str(asset.get("browser_download_url", ""))
        if not url.startswith(PREFIXO_DO_DOWNLOAD):
            return None  # fora do repositorio: nao se baixa
        digest = str(asset.get("digest") or "")
        sha = digest.split(":", 1)[1].lower() if digest.startswith("sha256:") else ""
        return Release(versao, url, int(asset.get("size") or 0), sha)
    return None


def buscar_ultima(abrir=_abrir_url) -> Release | None:
    """A ultima versao publicada, ou None. Falha de rede vira ErroDeAtualizacao."""
    try:
        with abrir(URL_DA_API) as resposta:
            dados = json.loads(resposta.read().decode("utf-8"))
    except Exception as exc:
        raise ErroDeAtualizacao(f"nao consegui consultar o GitHub: {exc}") from exc
    return interpretar_release(dados)


# -- baixar e conferir -------------------------------------------------------


def baixar(release: Release, destino: pathlib.Path, abrir=_abrir_url,
           permitir_sem_digest: bool = False) -> None:
    """Baixa para `destino` e so' o deixa la' se o SHA-256 bater."""
    if not release.sha256 and not permitir_sem_digest:
        raise ErroDeAtualizacao(
            "o GitHub nao informou o SHA-256 do arquivo; sem ele nao se atualiza "
            "sozinho (baixe pelo site e confira)")
    parcial = destino.with_name(destino.name + ".baixando")
    soma = hashlib.sha256()
    total = 0
    try:
        with abrir(release.url, 120.0) as resposta, open(parcial, "wb") as saida:
            while True:
                bloco = resposta.read(1 << 20)
                if not bloco:
                    break
                soma.update(bloco)
                saida.write(bloco)
                total += len(bloco)
        if release.tamanho and total != release.tamanho:
            raise ErroDeAtualizacao(
                f"o download veio com {total} bytes, o Release diz "
                f"{release.tamanho}")
        if release.sha256 and soma.hexdigest() != release.sha256:
            raise ErroDeAtualizacao("o SHA-256 do download nao bate com o do "
                                    "Release: arquivo descartado")
        os.replace(parcial, destino)
    except ErroDeAtualizacao:
        parcial.unlink(missing_ok=True)
        raise
    except Exception as exc:
        parcial.unlink(missing_ok=True)
        raise ErroDeAtualizacao(f"falha ao baixar a atualizacao: {exc}") from exc


def conferir_executavel(caminho: pathlib.Path, executar=subprocess.run) -> bool:
    """O .exe novo abre e importa tudo? (`--conferir-pacote`, sem pedir UAC)."""
    ambiente = {**os.environ, "__COMPAT_LAYER": "RunAsInvoker"}
    try:
        r = executar([str(caminho), "--conferir-pacote"], timeout=120,
                     env=ambiente, creationflags=0x08000000)
        return r.returncode == 0
    except Exception:
        return False


# -- trocar e reiniciar ------------------------------------------------------


def ler_estado(pasta: pathlib.Path) -> dict | None:
    try:
        return json.loads((pasta / ARQ_ESTADO).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _gravar_estado(pasta: pathlib.Path, estado: dict) -> None:
    (pasta / ARQ_ESTADO).write_text(json.dumps(estado, indent=2), encoding="utf-8")


def _rodar(*comando: str) -> tuple[int, str]:
    r = subprocess.run(list(comando), capture_output=True, creationflags=0x08000000)
    return r.returncode, (r.stdout + r.stderr).decode("oem", "replace").strip()


def lancar_ajudante(exe: pathlib.Path, via_tarefa: bool) -> None:
    """Sobe `exe --pos-atualizacao` FORA do processo que esta' se atualizando."""
    if via_tarefa:
        # Pela tarefa do inicio automatico o ajudante morreria junto: ela mata
        # o job inteiro. Uma tarefa agendada propria roda fora dele.
        comando = f'"{exe}" --pos-atualizacao'
        codigo, saida = _rodar("schtasks.exe", "/create", "/tn", TAREFA_AUXILIAR,
                               "/tr", comando, "/sc", "once", "/st", "00:00",
                               "/ru", "SYSTEM", "/rl", "HIGHEST", "/f")
        if codigo != 0:
            raise ErroDeAtualizacao(f"nao consegui criar a tarefa do ajudante: {saida}")
        codigo, saida = _rodar("schtasks.exe", "/run", "/tn", TAREFA_AUXILIAR)
        if codigo != 0:
            raise ErroDeAtualizacao(f"nao consegui iniciar o ajudante: {saida}")
        return
    DETACHED_PROCESS, NOVO_GRUPO, SEM_JANELA = 0x8, 0x200, 0x08000000
    subprocess.Popen([str(exe), "--pos-atualizacao"], close_fds=True,
                     creationflags=DETACHED_PROCESS | NOVO_GRUPO | SEM_JANELA)


def aplicar(release: Release, exe: pathlib.Path | None = None, *,
            via_tarefa: bool = False, baixar_=baixar, conferir=conferir_executavel,
            ajudante=lancar_ajudante, agora=time.time) -> None:
    """Baixa, confere, troca o .exe e dispara o ajudante. Quem chama deve SAIR.

    Levanta ErroDeAtualizacao SEM ter mexido no .exe em uso, exceto se a troca
    em si falhar -- e ai' ela se desfaz.
    """
    if exe is None:
        if not getattr(sys, "frozen", False):
            raise ErroDeAtualizacao("so' o .exe se atualiza; do codigo-fonte use git")
        exe = pathlib.Path(sys.executable)
    pasta = exe.parent
    novo, antigo = pasta / ARQ_NOVO, pasta / ARQ_ANTIGO
    if not e_mais_nova(release.versao):
        raise ErroDeAtualizacao(f"{release.versao} nao e' mais nova que {conf.VERSAO}")
    novo.unlink(missing_ok=True)
    baixar_(release, novo)
    if not conferir(novo):
        novo.unlink(missing_ok=True)
        raise ErroDeAtualizacao("o .exe baixado nao passou na conferencia do "
                                "pacote (`--conferir-pacote`); nada foi trocado")
    try:
        antigo.unlink(missing_ok=True)
        os.replace(exe, antigo)       # renomear um .exe em uso e' permitido
    except OSError as exc:
        novo.unlink(missing_ok=True)
        raise ErroDeAtualizacao(f"nao consegui guardar o .exe atual: {exc}") from exc
    try:
        os.replace(novo, exe)
    except OSError as exc:
        os.replace(antigo, exe)       # desfaz: o programa segue como estava
        raise ErroDeAtualizacao(f"nao consegui por o .exe novo no lugar: {exc}") from exc
    for confirmacao in (ARQ_CONFIRMACAO, ARQ_RESULTADO):
        (pasta / confirmacao).unlink(missing_ok=True)
    _gravar_estado(pasta, {"de": conf.VERSAO, "para": release.versao,
                           "exe": str(exe), "via_tarefa": bool(via_tarefa),
                           "em": agora()})
    log.info("atualizacao %s -> %s: .exe trocado; reiniciando pelo ajudante",
             conf.VERSAO, release.versao)
    try:
        ajudante(exe, via_tarefa)
    except Exception:
        # Sem ajudante ninguem reabre o programa: desfaz tudo agora.
        _desfazer(exe)
        raise


def _desfazer(exe: pathlib.Path) -> bool:
    """Devolve o .exe antigo ao lugar. O novo vai para `.falhou.exe`."""
    pasta = exe.parent
    antigo, falhou = pasta / ARQ_ANTIGO, pasta / ARQ_FALHOU
    if not antigo.is_file():
        return False
    try:
        falhou.unlink(missing_ok=True)
        os.replace(exe, falhou)
        os.replace(antigo, exe)
        return True
    except OSError:
        log.error("nao consegui devolver o .exe antigo", exc_info=True)
        return False


# -- o ajudante (`--pos-atualizacao`) ----------------------------------------


def _listar_processos() -> list[tuple[int, int, str]]:
    """[(pid, pid do pai, nome da imagem)] de todos os processos (toolhelp)."""
    import ctypes
    from ctypes import wintypes

    class ENTRADA(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", wintypes.LONG), ("dwFlags", wintypes.DWORD),
                    ("szExeFile", wintypes.WCHAR * 260)]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k32.Process32FirstW.argtypes = k32.Process32NextW.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(ENTRADA)]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    foto = k32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    achados: list[tuple[int, int, str]] = []
    if foto in (None, wintypes.HANDLE(-1).value):
        return achados
    try:
        e = ENTRADA()
        e.dwSize = ctypes.sizeof(ENTRADA)
        ok = k32.Process32FirstW(foto, ctypes.byref(e))
        while ok:
            achados.append((e.th32ProcessID, e.th32ParentProcessID, e.szExeFile))
            ok = k32.Process32NextW(foto, ctypes.byref(e))
    finally:
        k32.CloseHandle(foto)
    return achados


def _criacao_e_imagem(pid: int) -> tuple[float, str] | None:
    """(quando o processo nasceu, em epoch; caminho do .exe) ou None."""
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(
        wintypes.FILETIME)] * 4
    k32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return None
    try:
        a, b, c, d = (wintypes.FILETIME() for _ in range(4))
        if not k32.GetProcessTimes(h, ctypes.byref(a), ctypes.byref(b),
                                   ctypes.byref(c), ctypes.byref(d)):
            return None
        filetime = (a.dwHighDateTime << 32) | a.dwLowDateTime
        buf = ctypes.create_unicode_buffer(1024)
        tamanho = wintypes.DWORD(len(buf))
        if not k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(tamanho)):
            return None
        return (filetime - 116444736000000000) / 1e7, buf.value
    finally:
        k32.CloseHandle(h)


def processos_antigos(exe: pathlib.Path, antes_de: float, listar=_listar_processos,
                      info=_criacao_e_imagem) -> list[int]:
    """PIDs que rodam ESTE .exe e nasceram antes da troca (`antes_de`, epoch).

    Depois que o .exe em uso e' renomeado, os processos antigos continuam
    aparecendo com o nome e o caminho de sempre -- exatamente os do .exe novo
    --, entao a unica coisa que separa um do outro e' a hora em que nasceram.
    (Medido com processos de verdade: `tasklist /fi "imagename eq
    MultiPC-KVM.antigo.exe"` NAO acha o processo velho.) Este processo e o pai
    dele (o bootloader do --onefile) nunca entram.
    """
    meu = {os.getpid(), os.getppid()}
    # Medido com processos de verdade: depois do rename, o Windows informa o
    # caminho NOVO do arquivo (.antigo.exe) para o processo que ja' rodava, e o
    # NOME de imagem original (MultiPC-KVM.exe) na lista de processos. Valem os dois.
    alvos = {str(exe).casefold(), str(exe.with_name(ARQ_ANTIGO)).casefold()}
    achados = []
    for pid, _pai, nome in listar():
        if pid in meu or nome.casefold() != exe.name.casefold():
            continue
        dados = info(pid)
        if dados is None:
            continue
        nasceu, caminho = dados
        if caminho.casefold() in alvos and nasceu < antes_de:
            achados.append(pid)
    return achados


def _vivo(pid: int) -> bool:
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    h = k32.OpenProcess(0x100000 | 0x1000, False, pid)  # SYNCHRONIZE | QUERY_LIMITED
    if not h:
        return False
    try:
        return k32.WaitForSingleObject(h, 0) == 0x102  # WAIT_TIMEOUT: ainda roda
    finally:
        k32.CloseHandle(h)


def pos_atualizacao(pasta: pathlib.Path | None = None, *, rodar=_rodar,
                    listar_antigos=processos_antigos, vivo=_vivo, dormir=time.sleep,
                    relogio=time.monotonic, abrir_janela=None) -> int:
    """Para o antigo, abre o novo e espera a confirmacao; senao, desfaz.

    Devolve 0 se a atualizacao ficou de pe', 1 se foi desfeita, 2 se nem havia
    o que fazer.
    """
    pasta = pasta or conf.pasta_do_executavel()
    estado = ler_estado(pasta)
    if not estado:
        return 2
    exe = pathlib.Path(estado["exe"])
    via_tarefa = bool(estado.get("via_tarefa"))
    novo_pid = {"pid": 0}
    def iniciar() -> None:
        if via_tarefa:
            rodar("schtasks.exe", "/run", "/tn", TAREFA_PRINCIPAL)
        else:
            if abrir_janela is not None:
                novo_pid["pid"] = abrir_janela(exe)
            else:
                p = subprocess.Popen([str(exe)], close_fds=True,
                                     creationflags=0x8 | 0x200)
                novo_pid["pid"] = p.pid

    def parar_o_novo() -> None:
        if via_tarefa:
            rodar("schtasks.exe", "/end", "/tn", TAREFA_PRINCIPAL)
        elif novo_pid["pid"]:
            rodar("taskkill.exe", "/f", "/t", "/pid", str(novo_pid["pid"]))

    # 1. tira o antigo do caminho: os que nasceram antes da troca
    antigos = listar_antigos(exe, float(estado.get("em") or 0))
    if via_tarefa:
        rodar("schtasks.exe", "/end", "/tn", TAREFA_PRINCIPAL)
    limite = relogio() + ESPERA_DO_ANTIGO
    while any(vivo(p) for p in antigos) and relogio() < limite:
        dormir(1)
    sobraram = [p for p in antigos if vivo(p)]
    for p in sobraram:
        rodar("taskkill.exe", "/f", "/t", "/pid", str(p))
    if sobraram:
        dormir(1)
    # 2. abre o novo
    iniciar()
    # 3. espera ele dizer que esta' bem
    confirmacao = pasta / ARQ_CONFIRMACAO
    limite = relogio() + ESPERA_DA_CONFIRMACAO
    confirmou = False
    while relogio() < limite:
        try:
            confirmou = confirmacao.read_text(encoding="utf-8").strip() == estado["para"]
        except OSError:
            confirmou = False
        if confirmou:
            break
        dormir(2)
    if via_tarefa:
        rodar("schtasks.exe", "/delete", "/tn", TAREFA_AUXILIAR, "/f")
    if confirmou:
        (pasta / ARQ_ANTIGO).unlink(missing_ok=True)
        (pasta / ARQ_RESULTADO).write_text(
            f"ok {estado['de']} -> {estado['para']}", encoding="utf-8")
        return 0
    # 4. nao confirmou: desfaz
    parar_o_novo()
    dormir(2)
    voltou = _desfazer(exe)
    (pasta / ARQ_RESULTADO).write_text(
        f"FALHOU {estado['de']} -> {estado['para']}: a versao nova nao confirmou "
        f"em {ESPERA_DA_CONFIRMACAO} s; "
        + ("o .exe antigo foi devolvido" if voltou else "NAO consegui devolver o antigo"),
        encoding="utf-8")
    if voltou:
        iniciar()
    return 1


def confirmar_se_pendente(pasta: pathlib.Path | None = None, dormir=time.sleep) -> bool:
    """Chamado ao abrir: se esta' versao acabou de ser instalada, avisa o ajudante
    -- DEPOIS de ficar de pe' por CONFIRMAR_APOS segundos. Devolve True se avisou."""
    pasta = pasta or conf.pasta_do_executavel()
    estado = ler_estado(pasta)
    if not estado or estado.get("para") != conf.VERSAO:
        return False
    if (pasta / ARQ_CONFIRMACAO).is_file():
        return False
    dormir(CONFIRMAR_APOS)
    try:
        (pasta / ARQ_CONFIRMACAO).write_text(conf.VERSAO, encoding="utf-8")
    except OSError:
        return False
    log.info("versao %s confirmada ao ajudante da atualizacao", conf.VERSAO)
    return True


def limpar_restos(pasta: pathlib.Path | None = None) -> None:
    """Apaga o que sobrou de uma atualizacao interrompida (no inicio)."""
    pasta = pasta or conf.pasta_do_executavel()
    for nome in (ARQ_NOVO, ARQ_NOVO + ".baixando"):
        arquivo = pasta / nome
        try:
            # Recente = outra instancia pode estar baixando agora: deixa em paz.
            if arquivo.is_file() and time.time() - arquivo.stat().st_mtime > 600:
                arquivo.unlink()
        except OSError:
            pass


def resultado_da_ultima(pasta: pathlib.Path | None = None) -> str:
    """O que o ajudante registrou da ultima atualizacao ("" se nao houve)."""
    pasta = pasta or conf.pasta_do_executavel()
    try:
        return (pasta / ARQ_RESULTADO).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


# -- o servico em segundo plano ----------------------------------------------


class Atualizador:
    """Consulta o GitHub de tempos em tempos, conforme o modo escolhido.

    `modo()` -> "avisar" | "automatica" | "desligada" (le' a opcao a cada volta,
    para a mudanca valer sem reiniciar). `avisar(texto)` e' o balao da bandeja.
    `ocioso()` diz se o programa esta' num momento calmo (ninguem comandando um
    PC), porque atualizar reinicia o KVM por alguns segundos. `ao_atualizar()`
    fecha o processo depois que o ajudante foi disparado.
    """

    def __init__(self, modo, avisar, ocioso, ao_atualizar, via_tarefa,
                 buscar=buscar_ultima, aplicar_=aplicar):
        self.modo, self.avisar, self.ocioso = modo, avisar, ocioso
        self.ao_atualizar, self.via_tarefa = ao_atualizar, via_tarefa
        self._buscar, self._aplicar = buscar, aplicar_
        self._parar = threading.Event()
        self._thread: threading.Thread | None = None
        self.disponivel: Release | None = None
        self.ultimo_erro = ""
        self._avisada = ""

    def iniciar(self) -> None:
        """Liga a consulta periodica (se ainda nao estiver ligada)."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._parar.clear()
        self._thread = threading.Thread(target=self._ciclo, name="atualizador",
                                        daemon=True)
        self._thread.start()

    def parar(self) -> None:
        self._parar.set()

    def verificar(self) -> Release | None:
        """Uma consulta agora. Atualiza `disponivel` (None = em dia)."""
        try:
            release = self._buscar()
        except ErroDeAtualizacao as exc:
            self.ultimo_erro = str(exc)
            log.info("atualizacao: %s", exc)
            return None
        self.ultimo_erro = ""
        self.disponivel = release if release and e_mais_nova(release.versao) else None
        return self.disponivel

    def atualizar_agora(self) -> None:
        """Aplica a versao `disponivel`. Levanta ErroDeAtualizacao se nao der."""
        if self.disponivel is None:
            raise ErroDeAtualizacao("nao ha' versao nova para instalar")
        self._aplicar(self.disponivel, via_tarefa=self.via_tarefa())
        self.ao_atualizar()

    def _volta(self) -> None:
        modo = self.modo()
        if modo == "desligada":
            return
        release = self.verificar()
        if release is None:
            return
        if release.versao != self._avisada:
            self._avisada = release.versao
            if modo == "automatica":
                self.avisar(f"Atualizando para a v{release.versao}")
            else:
                self.avisar(f"Nova versao v{release.versao} disponivel: abra a "
                            f"janela ou o menu do icone e escolha Atualizar")
        if modo == "automatica" and self.ocioso():
            try:
                self.atualizar_agora()
            except ErroDeAtualizacao as exc:
                self.ultimo_erro = str(exc)
                log.warning("atualizacao automatica nao foi feita: %s", exc)

    def _ciclo(self) -> None:
        if self._parar.wait(ESPERA_INICIAL):
            return
        while True:
            try:
                self._volta()
            except Exception:
                log.warning("falha na verificacao de atualizacao", exc_info=True)
            # Com uma versao nova que ainda nao deu para instalar (nao estava
            # ocioso), volta a tentar em minutos; senao, no intervalo normal.
            espera = 300 if (self.disponivel and self.modo() == "automatica") \
                else INTERVALO_DE_CHECAGEM
            if self._parar.wait(espera):
                return
