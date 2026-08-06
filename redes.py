"""Placas de rede desta maquina, via GetAdaptersAddresses (Win32/ctypes).

Serve para tres coisas que o truque do "socket UDP para fora" nao resolve:

* mostrar ao usuario **qual IP e' Wi-Fi e qual e' cabo**, quando ha' mais de um;
* transmitir a busca na rede por **todas** as placas, e nao so' pela rota
  padrao -- com duas redes diferentes (ex.: 192.168.0.x no Wi-Fi e 192.168.10.x
  no cabo) o outro PC pode estar do lado que nao recebe;
* calcular o broadcast certo de cada sub-rede, a partir da mascara real.

Placas virtuais (VPN, loopback, Hyper-V) e enderecos 169.254.x.x (APIPA, que
quer dizer "nao consegui IP") ficam de fora por padrao.
"""

from __future__ import annotations

import ctypes
import ipaddress
import logging
import socket
from ctypes import wintypes
from dataclasses import dataclass

log = logging.getLogger("redes")

iphlpapi = ctypes.WinDLL("iphlpapi", use_last_error=True)

AF_INET = 2
GAA_FLAG_SKIP_ANYCAST = 0x0002
GAA_FLAG_SKIP_MULTICAST = 0x0004
GAA_FLAG_SKIP_DNS_SERVER = 0x0008
ERRO_BUFFER_PEQUENO = 111  # ERROR_BUFFER_OVERFLOW

IF_ETHERNET, IF_WIFI, IF_LOOPBACK, IF_PPP, IF_TUNEL = 6, 71, 24, 23, 131
OPER_UP = 1

NOMES_DE_TIPO = {
    IF_ETHERNET: "cabo",
    IF_WIFI: "Wi-Fi",
    IF_PPP: "PPP",
    IF_TUNEL: "tunel",
    IF_LOOPBACK: "loopback",
}

# Descricoes que denunciam placa virtual -- nenhuma delas leva ao outro PC.
VIRTUAIS = ("loopback", "virtual", "vpn", "tap-windows", "wintun", "hyper-v",
            "vmware", "virtualbox", "npcap", "pseudo")


class SOCKADDR(ctypes.Structure):
    _fields_ = [("sa_family", wintypes.USHORT), ("sa_data", ctypes.c_ubyte * 26)]


class SOCKET_ADDRESS(ctypes.Structure):
    _fields_ = [("lpSockaddr", ctypes.POINTER(SOCKADDR)),
                ("iSockaddrLength", ctypes.c_int)]


class IP_ADAPTER_UNICAST_ADDRESS(ctypes.Structure):
    pass


IP_ADAPTER_UNICAST_ADDRESS._fields_ = [
    # A uniao {ULONGLONG Alignment; struct {ULONG Length; DWORD Flags;}} ocupa
    # exatamente estes 8 bytes, entao os dois campos servem no lugar dela.
    ("Length", wintypes.ULONG),
    ("Flags", wintypes.DWORD),
    ("Next", ctypes.POINTER(IP_ADAPTER_UNICAST_ADDRESS)),
    ("Address", SOCKET_ADDRESS),
    ("PrefixOrigin", ctypes.c_int),
    ("SuffixOrigin", ctypes.c_int),
    ("DadState", ctypes.c_int),
    ("ValidLifetime", wintypes.ULONG),
    ("PreferredLifetime", wintypes.ULONG),
    ("LeaseLifetime", wintypes.ULONG),
    ("OnLinkPrefixLength", ctypes.c_uint8),
]


class IP_ADAPTER_ADDRESSES(ctypes.Structure):
    pass


# Só declaramos os campos até OperStatus: o resto da struct existe na memoria
# devolvida pelo Windows, mas nao precisamos dele e a navegacao e' por `Next`.
IP_ADAPTER_ADDRESSES._fields_ = [
    ("Length", wintypes.ULONG),
    ("IfIndex", wintypes.DWORD),
    ("Next", ctypes.POINTER(IP_ADAPTER_ADDRESSES)),
    ("AdapterName", ctypes.c_char_p),
    ("FirstUnicastAddress", ctypes.POINTER(IP_ADAPTER_UNICAST_ADDRESS)),
    ("FirstAnycastAddress", ctypes.c_void_p),
    ("FirstMulticastAddress", ctypes.c_void_p),
    ("FirstDnsServerAddress", ctypes.c_void_p),
    ("DnsSuffix", ctypes.c_wchar_p),
    ("Description", ctypes.c_wchar_p),
    ("FriendlyName", ctypes.c_wchar_p),
    ("PhysicalAddress", ctypes.c_ubyte * 8),
    ("PhysicalAddressLength", wintypes.ULONG),
    ("Flags", wintypes.ULONG),
    ("Mtu", wintypes.ULONG),
    ("IfType", wintypes.DWORD),
    ("OperStatus", ctypes.c_int),
]

iphlpapi.GetAdaptersAddresses.restype = wintypes.ULONG
iphlpapi.GetAdaptersAddresses.argtypes = [
    wintypes.ULONG, wintypes.ULONG, ctypes.c_void_p,
    ctypes.POINTER(IP_ADAPTER_ADDRESSES), ctypes.POINTER(wintypes.ULONG)]


@dataclass
class Placa:
    ip: str
    prefixo: int
    nome: str          # nome amigavel ("Wi-Fi 4", "Ethernet")
    descricao: str
    tipo: str          # "cabo", "Wi-Fi", ...
    virtual: bool

    @property
    def broadcast(self) -> str:
        try:
            rede = ipaddress.ip_network(f"{self.ip}/{self.prefixo}", strict=False)
            return str(rede.broadcast_address)
        except ValueError:
            return "255.255.255.255"

    def mesma_rede(self, outro_ip: str) -> bool:
        try:
            rede = ipaddress.ip_network(f"{self.ip}/{self.prefixo}", strict=False)
            return ipaddress.ip_address(outro_ip) in rede
        except ValueError:
            return False

    def rotulo(self) -> str:
        """'192.168.1.10 (Wi-Fi)' -- o que aparece na lista da interface."""
        detalhe = self.nome or self.tipo
        if self.tipo in ("cabo", "Wi-Fi") and self.tipo.lower() not in detalhe.lower():
            detalhe = f"{detalhe} -- {self.tipo}"
        return f"{self.ip} ({detalhe})"


def _ipv4_de(endereco: SOCKET_ADDRESS) -> str | None:
    sa = endereco.lpSockaddr
    if not sa or sa.contents.sa_family != AF_INET:
        return None
    # sockaddr_in: familia(2) porta(2) endereco(4) -- sa_data comeca apos a familia
    octetos = bytes(sa.contents.sa_data[2:6])
    return socket.inet_ntoa(octetos)


def listar(so_reais: bool = True) -> list[Placa]:
    """Placas IPv4 ativas. Com `so_reais`, descarta virtuais, loopback e APIPA."""
    tamanho = wintypes.ULONG(15000)
    flags = (GAA_FLAG_SKIP_ANYCAST | GAA_FLAG_SKIP_MULTICAST
             | GAA_FLAG_SKIP_DNS_SERVER)
    for _ in range(3):
        buffer = ctypes.create_string_buffer(tamanho.value)
        ponteiro = ctypes.cast(buffer, ctypes.POINTER(IP_ADAPTER_ADDRESSES))
        erro = iphlpapi.GetAdaptersAddresses(AF_INET, flags, None, ponteiro,
                                             ctypes.byref(tamanho))
        if erro == 0:
            break
        if erro != ERRO_BUFFER_PEQUENO:
            log.warning("GetAdaptersAddresses falhou (erro %d)", erro)
            return []
    else:
        return []

    placas: list[Placa] = []
    adaptador = ponteiro
    while adaptador:
        a = adaptador.contents
        descricao = a.Description or ""
        nome = a.FriendlyName or ""
        virtual = (a.IfType == IF_LOOPBACK
                   or any(m in descricao.lower() or m in nome.lower()
                          for m in VIRTUAIS))
        if a.OperStatus == OPER_UP:
            unicast = a.FirstUnicastAddress
            while unicast:
                u = unicast.contents
                ip = _ipv4_de(u.Address)
                if ip and not ip.startswith("127."):
                    placas.append(Placa(
                        ip=ip, prefixo=u.OnLinkPrefixLength, nome=nome,
                        descricao=descricao,
                        tipo=NOMES_DE_TIPO.get(a.IfType, f"tipo {a.IfType}"),
                        virtual=virtual))
                unicast = u.Next if u.Next else None
        adaptador = a.Next if a.Next else None

    if so_reais:
        placas = [p for p in placas
                  if not p.virtual and not p.ip.startswith("169.254.")]
    # Cabo antes de Wi-Fi: numa rede mista o cabo costuma ser o caminho bom.
    ordem = {"cabo": 0, "Wi-Fi": 1}
    placas.sort(key=lambda p: (ordem.get(p.tipo, 9), p.ip))
    return placas


def ip_padrao() -> str:
    """IP a sugerir quando o usuario ainda nao escolheu."""
    placas = listar()
    if placas:
        return placas[0].ip
    # Sem placa "real": cai no truque do socket, que ao menos devolve algo.
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.168.1.1", 1))
            return s.getsockname()[0]
    except OSError:
        return ""


def placa_de(ip: str) -> Placa | None:
    return next((p for p in listar(so_reais=False) if p.ip == ip), None)


def alcanca(ip_destino: str) -> Placa | None:
    """Por qual placa o destino esta' na mesma sub-rede (None = precisa rotear)."""
    return next((p for p in listar() if p.mesma_rede(ip_destino)), None)
