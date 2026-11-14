"""Harness compartilhado da suíte offline do finmail.

Este arquivo existe porque a AAP 0.12.1 lhe atribui a guarda de socket no
processo, os helpers de configuração, a fábrica do servidor SMTP local e o
runner de subprocessos, e porque a AAP 0.9 exige que os critérios 1 a 6 e 8
passem sem rede e sem variáveis de credencial definidas. Ele é também o dono do
par fictício de credenciais (``SENTINEL_USERNAME``/``SENTINEL_PASSWORD``), que
nunca é um valor real e que toda asserção de vazamento da suíte procura.

O que o harness garante:

- Nenhum teste alcança a rede externa. A guarda recusa qualquer destino fora de
  loopback no processo de teste e, por ``tests/fixtures/guard/sitecustomize.py``,
  nos subprocessos lançados pela suíte.
- Nenhum teste lê credencial real: as duas variáveis são removidas do ambiente
  de cada teste e de cada subprocesso.
- A fonte de dados é o dublê ``tests/fixtures/stub/yfinance``, colocado à frente
  do ``site-packages`` no ``PYTHONPATH`` dos subprocessos.
- O produto não ganha costura de testabilidade: toda substituição acontece aqui,
  por atributo de módulo ou por ambiente de processo.

O pacote ``finmail`` não é importado no escopo do módulo: a coleta precisa
funcionar qualquer que seja o estado do produto.
"""

import copy
import email
import errno
import ipaddress
import os
import socket
import ssl
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from email.message import Message
from pathlib import Path
from typing import Any

import pytest
import yaml

FIXTURES_DIR = Path(__file__).parent / "fixtures"
GUARD_DIR = FIXTURES_DIR / "guard"
STUB_DIR = FIXTURES_DIR / "stub"
TLS_DIR = FIXTURES_DIR / "tls"
CERT_PATH = TLS_DIR / "server.crt"
KEY_PATH = TLS_DIR / "server.key"
REPO_ROOT = Path(__file__).parent.parent

# Par fictício: nunca corresponde a uma conta real e é o valor procurado em toda
# asserção de vazamento (critério 6).
SENTINEL_USERNAME = "usuario-sentinela@exemplo.com"
SENTINEL_PASSWORD = "senha-sentinela-ficticia-42"

# Configuração válida sob o esquema da AAP 0.7.1. Os três ativos são exatamente os
# que o dublê de ``yfinance`` serve com dados.
DEFAULT_CONFIG: dict[str, Any] = {
    "tickers": ["AAPL", "PETR4.SA", "MSFT"],
    "recipients": ["destinatario1@exemplo.com", "destinatario2@exemplo.com"],
    "sender": "remetente@exemplo.com",
    "smtp": {"host": "localhost", "port": 587},
    "timezone": "America/Sao_Paulo",
}

# Marcador que remove uma chave inteira em ``config_writer``.
OMIT = object()

_CREDENTIAL_VARS = ("SMTP_USERNAME", "SMTP_PASSWORD")

# Único ambiente herdado por um subprocesso da suíte: o que a inicialização do CPython
# e o sistema de arquivos precisam, mais as ``LC_*`` do operador. Deliberadamente de
# fora ficam ``PYTHONPATH``, ``PYTHONHOME``, ``PYTHONSTARTUP``, ``PYTHONWARNINGS``, os
# proxies e qualquer credencial. A comparação ignora a caixa para valer também em um
# host que grafe ``SystemRoot``.
_ENV_ALLOWLIST = frozenset(
    {
        "PATH",
        "HOME",
        "TMPDIR",
        "TEMP",
        "TMP",
        "LANG",
        "LANGUAGE",
        "TZ",
        "SYSTEMROOT",
    }
)

_GUARD_MARKER = "__finmail_guard__"
_BLOCKED_MESSAGE = "acesso de rede externo bloqueado pela guarda de teste: "
_LOOPBACK_V4 = ipaddress.ip_network("127.0.0.0/8")
_LOOPBACK_V6 = ipaddress.IPv6Address("::1")


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


# Sondagem da guarda de rede, executada uma vez por sessão em um subprocesso cujo
# ambiente é o de ``_subprocess_env``. Os dois argumentos são o diretório da guarda e
# o diretório do dublê de ``yfinance``. O código roda no filho, não imprime nada no
# caminho de sucesso e falha com um traceback que nomeia a verificação que caiu. O
# destino 192.0.2.1 (TEST-NET-1) é recusado pela guarda antes de o SO ser tocado; sem
# guarda, o ``timeout`` de 1 s limita a espera e a tentativa não termina em
# ``ConnectionRefusedError`` nem em ``errno.ECONNREFUSED``. Os alvos da prova de
# datagrama e de resolução são escolhidos para o mesmo efeito: sem guarda, um
# ``sendto`` para um endereço TEST-NET-1 não levanta nada e ``externo.invalid``
# (RFC 6761, que nunca resolve) levanta ``socket.gaierror``, de modo que uma guarda
# ausente reprova a sondagem em vez de alcançar um serviço real. O host em ``bytes`` —
# forma que o CPython aceita tanto num sockaddr quanto na resolução — é provado em
# todas as entradas junto da forma em ``str``: sem a decodificação, ele seria a única
# porta aberta da superfície.
_GUARD_PROBE_CODE = """
import errno
import os
import pathlib
import socket
import sys

guard_dir = pathlib.Path(sys.argv[1]).resolve()
stub_dir = pathlib.Path(sys.argv[2]).resolve()
bloqueio = "acesso de rede externo bloqueado pela guarda de teste: "

guarda = sys.modules.get("sitecustomize")
assert guarda is not None, "sitecustomize nao foi carregado no subprocesso"
carregada = pathlib.Path(guarda.__file__).resolve()
assert carregada == guard_dir / "sitecustomize.py", (
    "sitecustomize veio de %s, esperado %s" % (carregada, guard_dir / "sitecustomize.py")
)

for dono, prefixo, nomes in (
    (socket.socket, "socket.socket", ("connect", "connect_ex", "sendto", "sendmsg")),
    (
        socket,
        "socket",
        ("getaddrinfo", "gethostbyname", "gethostbyname_ex", "gethostbyaddr"),
    ),
):
    for nome in nomes:
        alvo = getattr(dono, nome)
        assert getattr(alvo, "__finmail_guard__", False), (
            "%s.%s nao carrega a marca __finmail_guard__" % (prefixo, nome)
        )

for nome in ("SMTP_USERNAME", "SMTP_PASSWORD"):
    assert nome not in os.environ, (
        "credencial %s herdada pelo subprocesso; ela so pode chegar por override" % nome
    )

import yfinance

origem = pathlib.Path(yfinance.__file__).resolve()
assert origem.is_relative_to(stub_dir), (
    "yfinance veio de %s, esperado o duble sob %s" % (origem, stub_dir)
)

try:
    socket.create_connection(("192.0.2.1", 80), timeout=1)
except ConnectionRefusedError:
    pass
else:
    raise AssertionError("create_connection para destino externo nao foi recusado")


def recusa(nome, envio):
    try:
        envio()
    except ConnectionRefusedError as exc:
        assert str(exc).startswith(bloqueio), (
            "%s recusado com mensagem inesperada: %s" % (nome, exc)
        )
    else:
        raise AssertionError("%s nao foi recusado" % nome)


for host in ("192.0.2.1", b"192.0.2.1"):
    sonda = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sonda.settimeout(1)
        codigo = sonda.connect_ex((host, 80))
    finally:
        sonda.close()
    assert codigo == errno.ECONNREFUSED, (
        "connect_ex com host %r para destino externo devolveu %r, esperado ECONNREFUSED (%d)"
        % (host, codigo, errno.ECONNREFUSED)
    )

datagrama = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
try:
    datagrama.settimeout(1)
    for host in ("192.0.2.1", b"192.0.2.1"):
        recusa(
            "sendto externo com host %r" % (host,),
            lambda: datagrama.sendto(b"x", (host, 53)),
        )
        recusa(
            "sendmsg externo com host %r" % (host,),
            lambda: datagrama.sendmsg([b"x"], [], 0, (host, 53)),
        )
finally:
    datagrama.close()

for nome, resolver, argumentos in (
    ("getaddrinfo de nome externo", socket.getaddrinfo, ("externo.invalid", 443)),
    ("gethostbyname de nome externo", socket.gethostbyname, ("externo.invalid",)),
    ("getaddrinfo de nome em branco", socket.getaddrinfo, (" ", 80)),
):
    try:
        resolver(*argumentos)
    except ConnectionRefusedError as exc:
        assert str(exc).startswith(bloqueio), (
            "%s recusado com mensagem inesperada: %s" % (nome, exc)
        )
    else:
        raise AssertionError("%s nao foi recusado" % nome)

locais = socket.getaddrinfo("localhost", 587)
assert locais, "getaddrinfo('localhost', 587) nao devolveu endereco algum"

receptor = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
emissor = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
try:
    receptor.bind(("127.0.0.1", 0))
    receptor.settimeout(2)
    destino = receptor.getsockname()
    assert emissor.sendto(b"p", destino) == 1, "sendto de loopback falhou"
    assert receptor.recvfrom(1)[0] == b"p", "o datagrama de loopback nao chegou"
    assert emissor.sendmsg([b"q"], [], 0, destino) == 1, "sendmsg de loopback falhou"
    assert receptor.recvfrom(1)[0] == b"q", "o datagrama de sendmsg nao chegou"
    destino_bytes = (destino[0].encode("ascii"), destino[1])
    assert emissor.sendto(b"r", destino_bytes) == 1, "sendto de loopback com host bytes falhou"
    assert receptor.recvfrom(1)[0] == b"r", "o datagrama de loopback com host bytes nao chegou"
finally:
    emissor.close()
    receptor.close()
"""


def _is_loopback_host(host: object) -> bool:
    """Diz se o host de um sockaddr é loopback, sem resolver nome algum.

    ``bytes`` é decodificado como ASCII, que é a segunda forma que o CPython aceita
    para o host de um sockaddr; sem isso a forma em ``bytes`` seria a única porta
    aberta da guarda. O que não decodifica, e qualquer host que não seja ``str`` nem
    ``bytes``, não é algo que esta guarda consiga provar loopback, e é recusado: só o
    que ela pode provar é liberado. ``localhost`` e seus subdomínios são liberados
    porque o sistema operacional os resolve por ``/etc/hosts``. Qualquer outro nome é
    recusado: só poderia ser alcançado por resolução externa, que esta guarda nunca faz.
    """
    if isinstance(host, bytes):
        try:
            host = host.decode("ascii")
        except UnicodeDecodeError:
            return False
    if not isinstance(host, str):
        return False
    # A forma canônica vem primeiro: ``ipaddress.ip_address('LOCALHOST.')`` levanta
    # ValueError, e o sufixo ponto final só aparece depois de normalizar.
    name = host.strip().lower().rstrip(".")
    if name == "localhost" or name.endswith(".localhost"):
        return True
    try:
        address = ipaddress.ip_address(name)
    except ValueError:
        return False
    if address.version == 4:
        return address in _LOOPBACK_V4
    # ``::ffff:a.b.c.d`` carrega um IPv4 dentro do IPv6: a decisão é sobre o IPv4
    # mapeado, não sobre o invólucro.
    if address.ipv4_mapped is not None:
        return address.ipv4_mapped in _LOOPBACK_V4
    return address == _LOOPBACK_V6


def _is_allowed(address: object) -> bool:
    """Diz se um destino de conexão ou de envio de datagrama pode ser usado.

    Só tuplas de ``AF_INET``/``AF_INET6`` são julgadas, e apenas pelo primeiro item:
    ``AF_INET6`` chega como 4-tupla ``(host, port, flowinfo, scope_id)``. Caminhos de
    ``AF_UNIX`` (``str``/``bytes``) e ``None`` são liberados porque não alcançam a rede.
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
        address = kwargs.get("address", args[-1] if args else None)
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
        if "address" in kwargs:
            address = kwargs["address"]
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
# que o substitui. É a mesma superfície que ``tests/fixtures/guard/sitecustomize.py``
# aplica nos subprocessos, e a sondagem de ``guard_liveness`` confere a marca em cada
# entrada; um nome acrescentado de um lado sem o correspondente do outro deixaria o
# egresso aberto em um dos escopos.
_HOOKS: tuple[tuple[Any, str, Callable[[Any], Any]], ...] = (
    (socket.socket, "connect", _guarded_connect),
    (socket.socket, "connect_ex", _guarded_connect_ex),
    (socket.socket, "sendto", _guarded_sendto),
    (socket.socket, "sendmsg", _guarded_sendmsg),
    (socket, "getaddrinfo", _guarded_resolver),
    (socket, "gethostbyname", _guarded_resolver),
    (socket, "gethostbyname_ex", _guarded_resolver),
    (socket, "gethostbyaddr", _guarded_resolver),
)


def _install_socket_guard() -> list[tuple[Any, str, Any]] | None:
    """Instala a guarda de loopback no processo e devolve o que restaurar.

    Devolve a lista de ``(dono, nome, original)`` de cada atributo substituído, para
    que o teardown da fixture reponha exatamente o que esta função trocou — inclusive
    um invólucro que já existia. Devolve ``None`` só quando toda a superfície de
    ``_HOOKS`` já estava instalada, por exemplo pelo ``sitecustomize`` carregado no
    próprio processo, para que o teardown não desfaça uma instalação que não é dela e
    nenhum invólucro se empilhe. Uma guarda pela metade não conta como instalada: o
    que falta é posto aqui e cada atributo trocado entra na lista.
    """
    replaced: list[tuple[Any, str, Any]] = []
    for owner, name, factory in _HOOKS:
        current = getattr(owner, name)
        if getattr(current, _GUARD_MARKER, False):
            continue
        wrapper = factory(current)
        setattr(wrapper, _GUARD_MARKER, True)
        setattr(owner, name, wrapper)
        replaced.append((owner, name, current))
    return replaced or None


@pytest.fixture(autouse=True)
def socket_guard() -> Iterator[None]:
    """Bloqueia todo destino e todo nome fora de loopback durante o teste (5 e 8).

    A superfície é a de ``_HOOKS``: ``connect``, ``connect_ex``, ``sendto`` e
    ``sendmsg`` em ``socket.socket``, e ``getaddrinfo``, ``gethostbyname``,
    ``gethostbyname_ex`` e ``gethostbyaddr`` no módulo ``socket``. O teardown repõe
    exatamente os atributos que a instalação trocou, e nada quando a guarda já estava
    inteira — por exemplo em um processo que carregou o ``sitecustomize``.
    """
    installed = _install_socket_guard()
    try:
        yield
    finally:
        for owner, name, original in installed or ():
            setattr(owner, name, original)


@pytest.fixture(autouse=True)
def no_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove as credenciais do ambiente: a suíte roda sem elas (critério 8).

    Um teste que precise de credencial a redefine com ``monkeypatch.setenv``,
    sempre com o valor sentinela.
    """
    for name in _CREDENTIAL_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def config_writer(tmp_path: Path) -> Callable[..., Path]:
    """Devolve o escritor de ``config.yaml`` usado pelos testes do produto.

    O arquivo produzido é válido sob a AAP 0.7.1, exceto quando um override
    quebra deliberadamente uma única chave. ``raw=`` escreve o texto exatamente
    como recebido, para os casos de arquivo inválido; ``path=`` troca o destino
    padrão ``<tmp_path>/config.yaml``; um valor ``OMIT`` remove a chave e um
    ``smtp={...}`` é mesclado um nível dentro do bloco ``smtp`` padrão.
    """

    def write_config(
        path: Path | None = None,
        raw: str | None = None,
        **overrides: Any,
    ) -> Path:
        target = Path(path) if path is not None else tmp_path / "config.yaml"
        target.parent.mkdir(parents=True, exist_ok=True)
        if raw is not None:
            target.write_text(raw, encoding="utf-8")
            return target
        data = copy.deepcopy(DEFAULT_CONFIG)
        for key, value in overrides.items():
            if value is OMIT:
                data.pop(key, None)
                continue
            if key == "smtp" and isinstance(value, dict):
                block = dict(data.get("smtp") or {})
                for sub_key, sub_value in value.items():
                    if sub_value is OMIT:
                        block.pop(sub_key, None)
                    else:
                        block[sub_key] = sub_value
                data["smtp"] = block
                continue
            data[key] = value
        target.write_text(
            yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
        return target

    return write_config


@dataclass
class CapturedEnvelope:
    """Um envelope aceito pelo servidor SMTP local, como o servidor o recebeu."""

    mail_from: str
    rcpt_tos: list[str]
    content: bytes
    message: Message


@dataclass
class SmtpHarness:
    """Servidor SMTP local de teste e tudo que ele recebeu durante o teste."""

    host: str
    port: int
    envelopes: list[CapturedEnvelope] = field(default_factory=list)
    controller: Any = None
    # Handlers rodam na thread do event loop enquanto o teste lê a lista na thread
    # principal: o lock é o que torna a leitura de ``envelopes`` segura.
    lock: Any = field(default_factory=threading.Lock, repr=False)

    @property
    def messages(self) -> list[Message]:
        """As mensagens já parseadas, na ordem em que o servidor as aceitou."""
        with self.lock:
            return [envelope.message for envelope in self.envelopes]

    def add(self, envelope: CapturedEnvelope) -> None:
        """Registra um envelope aceito; chamado na thread do event loop."""
        with self.lock:
            self.envelopes.append(envelope)


class _CapturingHandler:
    """Handler do aiosmtpd que guarda os envelopes e aceita todos os destinatários.

    Os ganchos são corrotinas porque o aiosmtpd 1.4.6 faz ``await`` no retorno de
    cada gancho; um gancho síncrono falha com ``TypeError: object str can't be
    used in 'await' expression`` e o destinatário é recusado com 500.
    """

    def __init__(self, harness: SmtpHarness) -> None:
        self._harness = harness

    async def handle_RCPT(
        self,
        server: Any,
        session: Any,
        envelope: Any,
        address: str,
        rcpt_options: list[str],
    ) -> str:
        """Aceita o destinatário e o anota no envelope em construção."""
        envelope.rcpt_tos.append(address)
        return "250 OK"

    async def handle_DATA(self, server: Any, session: Any, envelope: Any) -> str:
        """Guarda a mensagem recebida e confirma a entrega."""
        content = envelope.content
        if isinstance(content, str):
            content = content.encode("utf-8")
        self._harness.add(
            CapturedEnvelope(
                mail_from=envelope.mail_from,
                rcpt_tos=list(envelope.rcpt_tos),
                content=content,
                message=email.message_from_bytes(content),
            )
        )
        return "250 Message accepted for delivery"


def _authenticate(
    server: Any,
    session: Any,
    envelope: Any,
    mechanism: str,
    auth_data: Any,
) -> Any:
    """Aceita apenas o par sentinela; qualquer outro par é recusado.

    Importa ``aiosmtpd.smtp`` aqui, e não no topo, para que a coleta da suíte
    funcione mesmo quando só as dependências de runtime estão instaladas.
    """
    from aiosmtpd.smtp import AuthResult, LoginPassword

    if not isinstance(auth_data, LoginPassword):
        return AuthResult(success=False, handled=False)

    def _text(value: object) -> object:
        if isinstance(value, (bytes, bytearray)):
            return bytes(value).decode("utf-8", errors="replace")
        return value

    login = _text(auth_data.login)
    password = _text(auth_data.password)
    if login == SENTINEL_USERNAME and password == SENTINEL_PASSWORD:
        return AuthResult(success=True)
    return AuthResult(success=False, handled=False)


def _reserve_loopback_port() -> int:
    """Reserva e libera uma porta livre de ``127.0.0.1``, devolvendo o número.

    ``aiosmtpd.Controller`` não aceita ``port=0``: ``start()`` falha ao tentar
    acionar o servidor numa porta que o SO ainda não escolheu.
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])
    finally:
        probe.close()


def _start_controller(harness: SmtpHarness) -> Any:
    """Sobe um ``Controller`` do aiosmtpd com STARTTLS obrigatório e AUTH.

    Reserva a porta, tenta subir uma vez e repete com outra porta se a reservada
    tiver sido tomada entre a sondagem e o ``bind`` do servidor.
    """
    from aiosmtpd.controller import Controller

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile=str(CERT_PATH), keyfile=str(KEY_PATH))
    handler = _CapturingHandler(harness)

    last_error: BaseException | None = None
    for _attempt in range(2):
        port = _reserve_loopback_port()
        controller = Controller(
            handler,
            hostname="localhost",
            port=port,
            tls_context=context,
            authenticator=_authenticate,
            require_starttls=True,
            timeout=10,
        )
        try:
            controller.start()
        except OSError as exc:  # porta tomada entre a sondagem e o bind
            last_error = exc
            try:
                controller.stop()
            except Exception:  # o servidor nunca subiu; nada a derrubar
                pass
            continue
        harness.port = port
        return controller
    raise AssertionError(
        f"não foi possível subir o servidor SMTP local de teste: {last_error!r}"
    )


@pytest.fixture
def smtp_server(monkeypatch: pytest.MonkeyPatch) -> Iterator[SmtpHarness]:
    """Servidor SMTP local com STARTTLS e AUTH, de pé durante todo o teste.

    O certificado é o descartável de ``tests/fixtures/tls``, com ``CN=localhost``
    e SAN para ``localhost`` e ``127.0.0.1``; por isso o cliente conecta por
    ``localhost``. No processo do teste, ``SSL_CERT_FILE`` é apontado para ele
    para que o ``ssl.create_default_context()`` de um cliente in-process o
    aceite; nos subprocessos, o mesmo valor é posto por ``_subprocess_env``.
    """
    monkeypatch.setenv("SSL_CERT_FILE", str(CERT_PATH))
    harness = SmtpHarness(host="localhost", port=0)
    controller = _start_controller(harness)
    harness.controller = controller
    try:
        yield harness
    finally:
        controller.stop()


def _subprocess_env(overrides: dict[str, str | None] | None = None) -> dict[str, str]:
    """Ambiente de um subprocesso da suíte: isolado do real e sem credencial.

    O ambiente nasce de um allowlist explícito — só o que a inicialização do CPython
    e o sistema de arquivos precisam, mais as ``LC_*`` do operador —, não de
    ``dict(os.environ)``: uma falha do filho tem de depender do código em teste, nunca
    do shell de quem roda a suíte, e um ``finmail`` ou ``yfinance`` de outro checkout
    não pode sombrear a árvore em teste por um ``PYTHONPATH`` herdado.

    O ``PYTHONPATH`` do filho é composto apenas por este harness: o dublê de
    ``yfinance`` primeiro, para sombrear o pacote instalado, a guarda de rede em
    seguida, para que o CPython importe ``sitecustomize`` na inicialização, e por
    último a raiz do repositório, como rede de segurança para uma instalação
    editável. ``SSL_CERT_FILE`` aponta para o certificado local, de modo que o
    ``ssl.create_default_context()`` do produto o aceite, e ``PYTHONUNBUFFERED`` e
    ``PYTHONDONTWRITEBYTECODE`` fixam a escrita e o cache do filho.

    ``overrides`` é aplicado por último e vence o padrão do harness: um override de
    ``PYTHONPATH``, ``SSL_CERT_FILE``, ``PYTHONUNBUFFERED`` ou
    ``PYTHONDONTWRITEBYTECODE`` é honrado como veio, e o de ``PYTHONPATH`` substitui
    deliberadamente o arranjo dublê mais guarda. Credencial chega ao filho apenas por
    um override, nunca por herança; um override com valor ``None`` remove a variável
    do ambiente final.
    """
    env: dict[str, str] = {}
    for name, value in os.environ.items():
        name_upper = name.upper()
        if name_upper in _ENV_ALLOWLIST or name_upper.startswith("LC_"):
            env[name] = value
    # A credencial nunca entra por herança: o allowlist já a exclui, e a remoção
    # explícita mantém a garantia do critério 8 mesmo se o allowlist mudar.
    for name in _CREDENTIAL_VARS:
        env.pop(name, None)

    env["PYTHONPATH"] = os.pathsep.join([str(STUB_DIR), str(GUARD_DIR), str(REPO_ROOT)])
    env["SSL_CERT_FILE"] = str(CERT_PATH)
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"

    for key, value in (overrides or {}).items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = str(value)
    return env


def _run(
    command: list[str],
    *,
    env: dict[str, str | None] | None,
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    """Executa o comando e devolve o resultado, ou falha com a saída à mão.

    ``TimeoutExpired`` vira ``AssertionError`` com o que o processo chegou a
    escrever, porque uma execução travada não diz nada sobre o produto e tudo
    sobre onde ela travou.
    """
    try:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=_subprocess_env(env),
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        if isinstance(stdout, (bytes, bytearray)):
            stdout = bytes(stdout).decode("utf-8", errors="replace")
        if isinstance(stderr, (bytes, bytearray)):
            stderr = bytes(stderr).decode("utf-8", errors="replace")
        raise AssertionError(
            f"o comando {command!r} excedeu {timeout}s.\n"
            f"stdout:\n{stdout}\nstderr:\n{stderr}"
        ) from exc


@pytest.fixture(scope="session", autouse=True)
def guard_liveness() -> None:
    """Prova, em um subprocesso por sessão, que a guarda de rede está viva.

    Compor o ambiente de ``_subprocess_env`` só *arranja* o bloqueio: sem esta
    sonda, uma guarda que tivesse deixado de instalar, uma marca ausente em qualquer
    elo da superfície — ``connect``, ``connect_ex``, ``sendto`` e ``sendmsg`` em
    ``socket.socket``, mais ``getaddrinfo``, ``gethostbyname``, ``gethostbyname_ex``
    e ``gethostbyaddr`` — ou um dublê de ``yfinance`` que perdesse a precedência
    deixariam a suíte verde com a metade "sem rede" do critério 5 da AAP 0.9 falsa.
    O filho carrega a guarda pelo ``sitecustomize``, confirma a marca em cada elo e a
    origem do dublê, prova que conexão, datagrama e resolução de nome para fora de
    loopback são recusados e que o caminho de loopback equivalente continua
    funcionando. O caminho de sucesso é silencioso, como o de qualquer subprocesso do
    produto — ``tests/test_end_to_end.py`` observa que não há nada em ``stderr``.
    """
    probe = _run(
        [sys.executable, "-c", _GUARD_PROBE_CODE, str(GUARD_DIR), str(STUB_DIR)],
        env=None,
        timeout=60,
    )
    stdout = probe.stdout or ""
    stderr = probe.stderr or ""
    assert probe.returncode == 0, (
        f"a sondagem da guarda de rede falhou no subprocesso (código "
        f"{probe.returncode}): a guarda de {GUARD_DIR / 'sitecustomize.py'} não foi "
        "carregada a partir do PYTHONPATH, a marca __finmail_guard__ falta em algum "
        "elo da superfície (socket.socket.connect, connect_ex, sendto e sendmsg; "
        "socket.getaddrinfo, gethostbyname, gethostbyname_ex e gethostbyaddr), o "
        "dublê de yfinance não sombreia o pacote instalado, um destino externo de "
        "conexão, de datagrama ou de resolução de nome não foi recusado, ou o "
        "caminho de loopback deixou de funcionar.\n"
        f"stdout:\n{stdout}\nstderr:\n{stderr}"
    )
    assert stdout == "" and stderr == "", (
        "a sondagem da guarda de rede escreveu nos streams do subprocesso; o caminho de "
        f"sucesso tem de ser silencioso.\nstdout:\n{stdout}\nstderr:\n{stderr}"
    )


@pytest.fixture
def run_finmail() -> Callable[..., subprocess.CompletedProcess[str]]:
    """Devolve o runner que executa o caminho real da CLI em um subprocesso.

    O comando é ``[sys.executable, '-m', 'finmail', *args]``, exatamente como o
    operador o executa — é isso que o critério 1 chama de caminho real da CLI,
    então nenhuma função do produto é chamada no lugar dele.
    """

    def run(
        *args: str,
        env: dict[str, str | None] | None = None,
        timeout: float = 120,
    ) -> subprocess.CompletedProcess[str]:
        return _run(
            [sys.executable, "-m", "finmail", *args],
            env=env,
            timeout=timeout,
        )

    return run


@pytest.fixture
def run_python() -> Callable[..., subprocess.CompletedProcess[str]]:
    """Devolve o runner de um trecho de código Python no mesmo ambiente.

    Os testes de isolamento o usam para provar o que um subprocesso do produto
    vê: o dublê de ``yfinance`` no lugar da biblioteca real, ``SSL_CERT_FILE``
    apontado para o certificado local e nenhuma credencial no ambiente.
    """

    def run(
        code: str,
        *args: str,
        env: dict[str, str | None] | None = None,
        timeout: float = 120,
    ) -> subprocess.CompletedProcess[str]:
        return _run(
            [sys.executable, "-c", code, *args],
            env=env,
            timeout=timeout,
        )

    return run


@pytest.fixture
def fixtures_dir() -> Path:
    """Diretório das fixtures de teste: guarda, dublê de ``yfinance`` e TLS."""
    return FIXTURES_DIR


@pytest.fixture
def sentinel_username() -> str:
    """Usuário SMTP fictício que o servidor local aceita."""
    return SENTINEL_USERNAME


@pytest.fixture
def sentinel_password() -> str:
    """Senha SMTP fictícia; é o valor procurado em toda asserção de vazamento."""
    return SENTINEL_PASSWORD


@pytest.fixture
def closed_port() -> Iterator[int]:
    """Porta de ``127.0.0.1`` onde nada escuta, para provar que não houve conexão.

    O critério 4 aponta ``smtp.port`` para esta porta: qualquer tentativa de
    conexão SMTP — inclusive uma disfarçada dentro de ``run --dry-run`` — é
    recusada pelo SO. O socket fica ligado, sem ``listen()``, até o teardown,
    em vez de ser liberado na hora: assim nenhum outro processo da máquina pode
    ocupar a porta no meio do teste e transformar a recusa em sucesso.
    """
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        holder.bind(("127.0.0.1", 0))
        yield int(holder.getsockname()[1])
    finally:
        holder.close()
