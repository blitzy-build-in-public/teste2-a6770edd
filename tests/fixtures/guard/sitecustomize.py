"""Guarda de rede da suíte de testes do finmail.

O CPython importa este módulo automaticamente, a partir do módulo ``site``, sempre
que o diretório que o contém está no ``PYTHONPATH``. A suíte coloca esse diretório
à frente do caminho de cada subprocesso que lança (``python -m finmail ...``), de
modo que a guarda existe antes de qualquer código do produto rodar. Nada no produto
a importa, e ela não deixa nenhuma marca no comportamento fora do teste.

Instalada, ela recusa com ``ConnectionRefusedError`` qualquer ``connect()`` cujo
destino não seja loopback, e faz ``connect_ex()`` devolver ``errno.ECONNREFUSED``
para esses mesmos destinos. É disso que depende o critério de aceitação segundo o
qual ``check-config`` e a suíte nunca acessam a rede externa.

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
from typing import Any

_LOOPBACK_V4 = ipaddress.ip_network('127.0.0.0/8')
_LOOPBACK_V6 = ipaddress.IPv6Address('::1')

_GUARD_MARKER = '__finmail_guard__'
_BLOCKED_MESSAGE = 'acesso de rede externo bloqueado pela guarda de teste: '


def _is_loopback_host(host: object) -> bool:
    """Diz se um host de sockaddr é loopback, sem resolver nada.

    Um host que não é ``str`` não pode ser classificado como externo e é liberado.
    Nomes ``localhost`` (e subdomínios ``.localhost``) são liberados porque o sistema
    operacional os resolve por ``/etc/hosts``. Qualquer outro nome é recusado: só
    poderia ser alcançado por resolução externa, que esta guarda nunca faz.
    """
    if not isinstance(host, str):
        return True
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


def _install() -> None:
    """Substitui ``connect`` e ``connect_ex`` pela versão que só aceita loopback."""
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    # O marcador vive no atributo da classe, não numa variável do módulo: assim um
    # recarregamento (ou a fixture autouse de ``tests/conftest.py``, que aplica a
    # mesma guarda no processo) não empilha invólucros nem faz a recusa virar duas.
    # Os dois marcadores são exigidos juntos: com ``connect`` protegido e
    # ``connect_ex`` aberto a guarda não está instalada, e tomá-la por instalada
    # deixaria aberto exatamente o caminho que ela existe para fechar.
    connect_guarded = getattr(original_connect, _GUARD_MARKER, False)
    connect_ex_guarded = getattr(original_connect_ex, _GUARD_MARKER, False)
    if connect_guarded and connect_ex_guarded:
        return

    if not connect_guarded:

        def connect(self: socket.socket, address: object, *args: Any, **kwargs: Any) -> Any:
            if not _is_allowed(address):
                # Levanta antes de tocar o socket: o destino externo nunca chega ao SO.
                raise ConnectionRefusedError(_BLOCKED_MESSAGE + repr(address))
            return original_connect(self, address, *args, **kwargs)

        setattr(connect, _GUARD_MARKER, True)
        socket.socket.connect = connect

    # ``connect_ex`` é outro descritor de método do mesmo objeto C: proteger apenas
    # ``connect`` deixaria um caminho aberto para a rede externa.
    if not connect_ex_guarded:

        def connect_ex(self: socket.socket, address: object, *args: Any, **kwargs: Any) -> int:
            if not _is_allowed(address):
                # ``connect_ex`` sinaliza erro pelo retorno, não por exceção.
                return errno.ECONNREFUSED
            return original_connect_ex(self, address, *args, **kwargs)

        setattr(connect_ex, _GUARD_MARKER, True)
        socket.socket.connect_ex = connect_ex


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
