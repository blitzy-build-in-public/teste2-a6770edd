"""Guarda de rede da suíte de testes do finmail.

O CPython importa este módulo automaticamente, a partir do módulo ``site``, sempre
que o diretório que o contém está no ``PYTHONPATH``. A suíte coloca esse diretório
à frente do caminho de cada subprocesso que lança (``python -m finmail ...``), de
modo que a guarda existe antes de qualquer código do produto rodar. Nada no produto
a importa, e ela não deixa nenhuma marca no comportamento fora do teste.

Instalada, ela recusa com ``ConnectionRefusedError`` qualquer ``connect()``,
``sendto()`` ou ``sendmsg()`` cujo destino não seja loopback, faz ``connect_ex()``
devolver ``errno.ECONNREFUSED`` para esses mesmos destinos e recusa a consulta de
qualquer nome externo em ``getaddrinfo()``, ``gethostbyname()``,
``gethostbyname_ex()`` e ``gethostbyaddr()``; essa recusa é também um
``socket.gaierror``, que é o que um host sem rede produz para o mesmo nome. É disso
que depende o critério de aceitação segundo o qual ``check-config`` e a suíte nunca
acessam a rede externa.

O módulo é silencioso enquanto a instalação dá certo (não imprime, não loga, não
emite avisos). Se ela falhar, a causa não é descartada nem escondida: uma única
linha nomeando a falha vai para ``stderr``, com ou sem ``FINMAIL_GUARD_DEBUG``, de
modo que um processo que rodaria sem guarda seja impossível de não notar; definir
essa variável acrescenta o traceback da falha à linha. A exceção não é relançada,
porque a suíte precisa passar numa máquina limpa. Fora isso, usa apenas a
biblioteca padrão e não resolve nome algum: ele julga o destino pelo que recebe,
sem consultar DNS.
"""

import errno
import ipaddress
import os
import socket
import sys
import traceback
from collections.abc import Callable
from typing import Any

_LOOPBACK_V4 = ipaddress.ip_network('127.0.0.0/8')
_LOOPBACK_V6 = ipaddress.IPv6Address('::1')

_GUARD_MARKER = '__finmail_guard__'
_BLOCKED_MESSAGE = 'acesso de rede externo bloqueado pela guarda de teste: '


class _NameResolutionBlocked(ConnectionRefusedError, socket.gaierror):
    """Recusa de resolução de um nome externo.

    É a ``ConnectionRefusedError`` da guarda e, ao mesmo tempo, o ``socket.gaierror``
    que um host sem rede produz para o mesmo nome. A segunda base não afrouxa a
    recusa — nada é resolvido —, mas preserva o significado que quem chama
    ``getaddrinfo``/``gethostbyname`` espera de um nome que não resolve: o
    ``smtplib`` captura ``gaierror``, e apenas ele, ao procurar o nome da própria
    máquina para o ``EHLO`` e cair no endereço literal. Sem ela, recusar um nome
    externo viraria um defeito do produto no lugar de uma falha de nome esperada.
    """


def _is_loopback_host(host: object) -> bool:
    """Diz se um host de sockaddr é loopback, sem resolver nada.

    ``bytes`` é decodificado como ASCII, que é a segunda forma que o CPython aceita
    para o host de um sockaddr; sem isso a forma em ``bytes`` seria a única porta
    aberta da guarda. O que não decodifica, e qualquer host que não seja ``str`` nem
    ``bytes``, não é algo que esta guarda consiga provar loopback, e é recusado: só o
    que ela pode provar é liberado. Nomes ``localhost`` (e subdomínios ``.localhost``)
    são liberados porque o sistema operacional os resolve por ``/etc/hosts``. Qualquer
    outro nome é recusado: só poderia ser alcançado por resolução externa, que esta
    guarda nunca faz.
    """
    if isinstance(host, bytes):
        try:
            host = host.decode('ascii')
        except UnicodeDecodeError:
            return False
    if not isinstance(host, str):
        return False
    # Normaliza antes de analisar: ``ipaddress.ip_address('LOCALHOST.')`` levanta
    # ValueError, então a forma canônica precisa vir primeiro.
    name = host.strip().lower().rstrip('.')
    if name == 'localhost' or name.endswith('.localhost'):
        return True
    try:
        address = ipaddress.ip_address(name)
    except ValueError:
        return False
    if address.version == 4:
        return address in _LOOPBACK_V4
    # Um endereço ``::ffff:a.b.c.d`` carrega um IPv4 dentro de um IPv6; a decisão tem
    # de ser tomada sobre o IPv4 mapeado, não sobre o invólucro IPv6.
    if address.ipv4_mapped is not None:
        return address.ipv4_mapped in _LOOPBACK_V4
    return address == _LOOPBACK_V6


def _is_allowed(address: object) -> bool:
    """Diz se um sockaddr pode ser usado.

    Só tuplas de ``AF_INET``/``AF_INET6`` são julgadas, e apenas pelo seu primeiro
    item; o restante é ignorado, porque ``AF_INET6`` chega como uma 4-tupla
    ``(host, port, flowinfo, scope_id)``. Caminhos de ``AF_UNIX`` (``str``/``bytes``)
    e ``None`` são liberados: eles não alcançam a rede.
    """
    if not isinstance(address, tuple) or len(address) < 2:
        return True
    return _is_loopback_host(address[0])


def _is_resolvable_host(host: object) -> bool:
    """Diz se um host entregue à resolução de nome pode ser consultado.

    ``None`` e a string vazia são o "sem nome" de ``getaddrinfo`` — a consulta de bind
    de quem vai escutar, com ``AI_PASSIVE`` —, e não há nome a resolver. Todo o resto
    é julgado como o host de um sockaddr, pelo mesmo predicado de ``connect``: um nome
    só de espaços não é o curinga da consulta de bind e é recusado, e a forma em
    ``bytes`` é decodificada lá, como em qualquer outro destino.
    """
    if host is None or (isinstance(host, str) and not host):
        return True
    return _is_loopback_host(host)


def _guarded_connect(original: Any) -> Any:
    """Produz o ``connect`` que recusa um destino fora de loopback."""

    def connect(self: socket.socket, address: object, *args: Any, **kwargs: Any) -> Any:
        if not _is_allowed(address):
            # Levanta antes de tocar o socket: o destino externo nunca chega ao SO.
            raise ConnectionRefusedError(_BLOCKED_MESSAGE + repr(address))
        return original(self, address, *args, **kwargs)

    return connect


def _guarded_connect_ex(original: Any) -> Any:
    """Produz o ``connect_ex`` que sinaliza um destino fora de loopback pelo retorno."""

    def connect_ex(self: socket.socket, address: object, *args: Any, **kwargs: Any) -> int:
        if not _is_allowed(address):
            # ``connect_ex`` sinaliza erro pelo retorno, não por exceção.
            return errno.ECONNREFUSED
        return original(self, address, *args, **kwargs)

    return connect_ex


def _guarded_sendto(original: Any) -> Any:
    """Produz o ``sendto`` que recusa o datagrama destinado a fora de loopback."""

    def sendto(self: socket.socket, *args: Any, **kwargs: Any) -> int:
        # O endereço é o último argumento posicional nas duas formas do método:
        # ``sendto(data, address)`` e ``sendto(data, flags, address)``.
        address = kwargs.get('address', args[-1] if args else None)
        if not _is_allowed(address):
            # Recusa antes da entrega: o datagrama externo nunca chega ao SO.
            raise ConnectionRefusedError(_BLOCKED_MESSAGE + repr(address))
        return original(self, *args, **kwargs)

    return sendto


def _guarded_sendmsg(original: Any) -> Any:
    """Produz o ``sendmsg`` que recusa o datagrama destinado a fora de loopback."""

    def sendmsg(self: socket.socket, *args: Any, **kwargs: Any) -> int:
        # ``sendmsg(buffers[, ancdata[, flags[, address]]])``: o endereço é a quarta
        # posição e é opcional. Sem ele o socket já está conectado, e uma conexão
        # externa só existiria se o ``connect`` desta mesma guarda a tivesse liberado.
        if 'address' in kwargs:
            address = kwargs['address']
        else:
            address = args[3] if len(args) > 3 else None
        if address is not None and not _is_allowed(address):
            raise ConnectionRefusedError(_BLOCKED_MESSAGE + repr(address))
        return original(self, *args, **kwargs)

    return sendmsg


def _guarded_resolver(original: Any) -> Any:
    """Produz a entrada de resolução de nome que recusa consultar um nome externo."""

    def resolve(host: object, *args: Any, **kwargs: Any) -> Any:
        if not _is_resolvable_host(host):
            raise _NameResolutionBlocked(_BLOCKED_MESSAGE + repr(host))
        return original(host, *args, **kwargs)

    return resolve


# Superfície protegida numa tabela única: o atributo substituído e a fábrica do invólucro
# que o substitui. ``tests/conftest.py`` instala exatamente esta superfície no processo de
# teste, e a sondagem da suíte confere a marca em cada entrada; um nome acrescentado aqui
# sem o correspondente lá — ou o contrário — deixaria o egresso aberto em um dos escopos.
_HOOKS: tuple[tuple[Any, str, Callable[[Any], Any]], ...] = (
    (socket.socket, 'connect', _guarded_connect),
    (socket.socket, 'connect_ex', _guarded_connect_ex),
    (socket.socket, 'sendto', _guarded_sendto),
    (socket.socket, 'sendmsg', _guarded_sendmsg),
    (socket, 'getaddrinfo', _guarded_resolver),
    (socket, 'gethostbyname', _guarded_resolver),
    (socket, 'gethostbyname_ex', _guarded_resolver),
    (socket, 'gethostbyaddr', _guarded_resolver),
)


def _install() -> None:
    """Substitui toda a superfície de egresso pela versão que só aceita loopback."""
    # O marcador vive no atributo substituído, não numa variável do módulo: assim um
    # recarregamento (ou a fixture autouse de ``tests/conftest.py``, que aplica a mesma
    # guarda no processo) não empilha invólucros nem faz a recusa virar duas. Um atributo
    # já marcado é deixado como está, e a guarda só se considera instalada quando todos
    # os da tabela carregam a marca: com um deles aberto, tomá-la por instalada deixaria
    # livre exatamente o caminho que ela existe para fechar.
    for owner, name, factory in _HOOKS:
        current = getattr(owner, name)
        if getattr(current, _GUARD_MARKER, False):
            continue
        wrapper = factory(current)
        setattr(wrapper, _GUARD_MARKER, True)
        setattr(owner, name, wrapper)


def _surface_install_failure(exc: BaseException) -> None:
    """Anuncia em ``stderr`` a falha de instalação da guarda, sem descartar a causa.

    Uma guarda que não instala não pode transformar a inicialização num
    ``ImportError`` — a suíte precisa passar numa máquina limpa —, mas também não
    pode deixar o processo rodando desprotegido em silêncio. Por isso a causa vira
    sempre uma única linha em ``stderr``, definida ou não a variável de ambiente
    ``FINMAIL_GUARD_DEBUG``; definida, ela é um acréscimo e soma o traceback da
    falha, nunca o único gatilho do aviso.
    """
    cause = ' '.join(f'{exc!r}'.splitlines())
    print(f'guarda de rede do finmail não instalada: {cause}', file=sys.stderr)
    if os.environ.get('FINMAIL_GUARD_DEBUG'):
        traceback.print_exception(exc)


try:
    _install()
except Exception as exc:  # guarda que não instala não pode transformar a suíte num ImportError
    _surface_install_failure(exc)
