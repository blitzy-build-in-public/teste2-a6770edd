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

_GUARD_MARKER = "__finmail_guard__"
_BLOCKED_MESSAGE = "acesso de rede externo bloqueado pela guarda de teste: "
_LOOPBACK_V4 = ipaddress.ip_network("127.0.0.0/8")
_LOOPBACK_V6 = ipaddress.IPv6Address("::1")


def _is_loopback_host(host: object) -> bool:
    """Diz se o host de um sockaddr é loopback, sem resolver nome algum.

    Um host que não é ``str`` não pode ser classificado como externo e é liberado.
    ``localhost`` e seus subdomínios são liberados porque o sistema operacional os
    resolve por ``/etc/hosts``. Qualquer outro nome é recusado: só poderia ser
    alcançado por resolução externa, que esta guarda nunca faz.
    """
    if not isinstance(host, str):
        return True
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
    """Diz se um destino de ``connect``/``connect_ex`` pode ser usado.

    Só tuplas de ``AF_INET``/``AF_INET6`` são julgadas, e apenas pelo primeiro item:
    ``AF_INET6`` chega como 4-tupla ``(host, port, flowinfo, scope_id)``. Caminhos de
    ``AF_UNIX`` (``str``/``bytes``) e ``None`` são liberados porque não alcançam a rede.
    """
    if not isinstance(address, tuple) or len(address) < 2:
        return True
    return _is_loopback_host(address[0])


def _install_socket_guard() -> tuple[Any, Any] | None:
    """Instala a guarda de loopback no processo e devolve o que restaurar.

    Devolve ``None`` quando a guarda já estava instalada — por exemplo pelo
    ``sitecustomize`` carregado no próprio processo —, para que o teardown da
    fixture não desfaça uma instalação que não é dela e nenhum invólucro se
    empilhe.
    """
    original_connect = socket.socket.connect
    if getattr(original_connect, _GUARD_MARKER, False):
        return None

    def connect(self: socket.socket, address: object, *args: Any, **kwargs: Any) -> Any:
        if not _is_allowed(address):
            # Levanta antes de tocar o socket: o destino externo nunca chega ao SO.
            raise ConnectionRefusedError(_BLOCKED_MESSAGE + repr(address))
        return original_connect(self, address, *args, **kwargs)

    setattr(connect, _GUARD_MARKER, True)

    original_connect_ex = socket.socket.connect_ex

    def connect_ex(self: socket.socket, address: object, *args: Any, **kwargs: Any) -> int:
        if not _is_allowed(address):
            # ``connect_ex`` sinaliza erro pelo retorno, não por exceção.
            return errno.ECONNREFUSED
        return original_connect_ex(self, address, *args, **kwargs)

    setattr(connect_ex, _GUARD_MARKER, True)

    # ``connect_ex`` é outro descritor do mesmo objeto C: proteger só ``connect``
    # deixaria um caminho aberto para a rede externa.
    socket.socket.connect = connect
    socket.socket.connect_ex = connect_ex
    return original_connect, original_connect_ex


@pytest.fixture(autouse=True)
def socket_guard() -> Iterator[None]:
    """Bloqueia todo destino fora de loopback durante o teste (critérios 5 e 8)."""
    installed = _install_socket_guard()
    try:
        yield
    finally:
        if installed is not None:
            socket.socket.connect, socket.socket.connect_ex = installed


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

    ``PYTHONPATH`` recebe o dublê de ``yfinance`` primeiro, para sombrear o
    pacote instalado, a guarda de rede em seguida, para que o CPython importe
    ``sitecustomize`` na inicialização, o ``PYTHONPATH`` herdado (ou o do
    chamador) e, por último, a raiz do repositório, como rede de segurança para
    uma instalação editável. ``SSL_CERT_FILE`` aponta para o certificado local,
    de modo que o ``ssl.create_default_context()`` do produto o aceite. Um
    override com valor ``None`` remove a variável do ambiente final.
    """
    env = dict(os.environ)
    for name in _CREDENTIAL_VARS:
        env.pop(name, None)

    removals: list[str] = []
    for key, value in (overrides or {}).items():
        if value is None:
            removals.append(key)
            env.pop(key, None)
        else:
            env[key] = str(value)

    pythonpath = [str(STUB_DIR), str(GUARD_DIR)]
    inherited = env.get("PYTHONPATH")
    if inherited:
        pythonpath.append(inherited)
    pythonpath.append(str(REPO_ROOT))

    env["PYTHONPATH"] = os.pathsep.join(pythonpath)
    env["SSL_CERT_FILE"] = str(CERT_PATH)
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    for key in removals:
        env.pop(key, None)
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
