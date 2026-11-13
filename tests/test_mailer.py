"""Prova a estrutura MIME e as tentativas de envio do adaptador de e-mail.

Os dois pontos de contato do produto com o mundo exterior são substituídos por
dublês locais: ``mailer.smtplib.SMTP`` vira :class:`FakeSMTP`, que registra a
transação e nunca abre socket, e ``mailer.time.sleep`` vira um gravador, de modo
que a política de retentativa é conferida pela lista exata de esperas em vez de
por uma espera real de 5 ou 30 segundos. Nenhuma costura de testabilidade é
acrescentada ao código de produção.

O que este arquivo prova: o ``multipart/alternative`` com o corpo texto antes do
HTML, a mensagem única endereçada a todos os destinatários, a transação
``ehlo`` → ``starttls`` → ``ehlo`` → ``login`` → ``send_message`` sobre o contexto
TLS padrão do sistema, as três tentativas com esperas de 5 s e 30 s, a ausência de
repetição para autenticação recusada, resposta 5xx, servidor sem STARTTLS e
certificado inválido, a identidade da mensagem entre tentativas e o sigilo da
senha em toda saída (AAP 0.7.2 e critério 6).
"""

import email
import email.policy
import email.utils
import inspect
import logging
import smtplib
import ssl
from collections.abc import Sequence
from email.message import EmailMessage

import pytest

import finmail.mailer as mailer
from finmail.config import Credentials, SmtpConfig
from finmail.report import Report

SMTP_HOST = "smtp.exemplo.com"
SMTP_PORT = 587
SENDER = "remetente@exemplo.com"
RECIPIENTS = (
    "destinatario1@exemplo.com",
    "destinatario2@exemplo.com",
    "destinatario3@exemplo.com",
)
# Travessão EN (U+2013) entre o título e a data, exatamente como o pedido o fixa.
SUBJECT = "Resumo de mercado \u2013 2026-11-13"
# Os dois corpos terminam com quebra de linha, como os templates empacotados os
# produzem: o ``set_content`` garante essa quebra, e sem ela a comparação exata do
# corpo depois de decodificado acusaria uma diferença que não é do produto.
TEXT_BODY = (
    "Resumo de mercado \u2013 2026-11-13\n"
    "Ativo | Nome | Moeda | Data | Fechamento | Variação | Variação % | Volume | "
    "Mín. 52s | Máx. 52s | Estado\n"
    "PETR4.SA | Petróleo Brasileiro S.A. - Petrobras | BRL | 2026-11-12 | 45,19 | "
    "+0,49 | +1,10% | 14.474.200 | 29,53 | 59,79 | ok\n"
    "Dados fornecidos pelo Yahoo Finance. Os valores podem apresentar atraso.\n"
)
HTML_BODY = (
    '<html><body><table><tr><td>Petróleo &amp; Variação \u2013 45,19</td></tr>'
    "</table></body></html>\n"
)
# Prefixo mínimo que uma entrega bem-sucedida precisa registrar, na ordem do
# RFC 3207: o segundo ``ehlo`` descarta o conhecimento obtido antes do TLS.
EXPECTED_TRANSACTION = ["ehlo", "starttls", "ehlo", "login", "send_message"]


class FakeSMTP:
    """Substituto de ``smtplib.SMTP`` que registra a transação sem abrir socket.

    Cada instância representa uma tentativa de entrega e consome a entrada
    correspondente de ``behaviors``, um dicionário que mapeia o nome do passo que
    deve falhar (``connect``, ``ehlo``, ``starttls``, ``login`` ou
    ``send_message``) para a exceção a levantar; uma tentativa além do roteiro é
    limpa. Todo passo chamado pelo produto é anotado em :attr:`events`, na ordem
    em que aconteceu, junto com os argumentos que importam para as asserções.
    """

    instances: list["FakeSMTP"] = []
    behaviors: tuple[dict[str, BaseException], ...] = ()

    def __init__(self, host: str, port: int, timeout: float | None = None) -> None:
        index = len(type(self).instances)
        script = type(self).behaviors
        self.behavior = dict(script[index]) if index < len(script) else {}
        self.init_args = (host, port)
        self.timeout = timeout
        self.events: list[str] = []
        self.starttls_context: ssl.SSLContext | None = None
        self.login_args: tuple[str | None, str | None] | None = None
        self.sent_messages: list[EmailMessage] = []
        self.send_to_addrs: list[Sequence[str] | None] = []
        self.delivered = 0
        # A instância entra na lista antes de qualquer falha: uma tentativa que
        # morre na conexão também conta como tentativa.
        type(self).instances.append(self)
        self._raise_scripted("connect")

    def __enter__(self) -> "FakeSMTP":
        return self

    def __exit__(self, *exc_info: object) -> bool:
        self.quit()
        self.close()
        return False

    def ehlo(self, *args: object, **kwargs: object) -> tuple[int, bytes]:
        self.events.append("ehlo")
        self._raise_scripted("ehlo")
        return 250, b"OK"

    def starttls(
        self,
        context: ssl.SSLContext | None = None,
        *args: object,
        **kwargs: object,
    ) -> tuple[int, bytes]:
        self.events.append("starttls")
        self.starttls_context = context
        self._raise_scripted("starttls")
        return 220, b"Ready to start TLS"

    def login(
        self,
        user: str | None = None,
        password: str | None = None,
        *args: object,
        **kwargs: object,
    ) -> tuple[int, bytes]:
        self.events.append("login")
        self.login_args = (user, password)
        self._raise_scripted("login")
        return 235, b"Authentication successful"

    def send_message(
        self,
        message: EmailMessage,
        from_addr: str | None = None,
        to_addrs: Sequence[str] | None = None,
        *args: object,
        **kwargs: object,
    ) -> dict[str, tuple[int, bytes]]:
        # O argumento é registrado antes da falha: a identidade do objeto é
        # conferida mesmo nas tentativas que o servidor recusou.
        self.events.append("send_message")
        self.sent_messages.append(message)
        self.send_to_addrs.append(to_addrs)
        self._raise_scripted("send_message")
        self.delivered += 1
        return {}

    def quit(self, *args: object, **kwargs: object) -> tuple[int, bytes]:
        self.events.append("quit")
        return 221, b"Bye"

    def close(self, *args: object, **kwargs: object) -> None:
        self.events.append("close")

    def set_debuglevel(self, level: int = 1) -> None:
        # Registrado para que a ausência dele nas asserções signifique algo.
        self.events.append("set_debuglevel")

    def _raise_scripted(self, step: str) -> None:
        error = self.behavior.get(step)
        if error is not None:
            raise error


def install_fake_smtp(
    monkeypatch: pytest.MonkeyPatch,
    behaviors: Sequence[dict[str, BaseException]] = (),
) -> tuple[type[FakeSMTP], list[FakeSMTP]]:
    """Instala :class:`FakeSMTP` em ``mailer.smtplib.SMTP`` para um teste.

    Zera a lista de instâncias, fixa o roteiro de falhas por tentativa e devolve
    ``(classe, instâncias)``; ``monkeypatch`` restaura o ``smtplib.SMTP`` real no
    fim do teste.
    """
    FakeSMTP.instances = []
    FakeSMTP.behaviors = tuple(behaviors)
    monkeypatch.setattr(mailer.smtplib, "SMTP", FakeSMTP)
    return FakeSMTP, FakeSMTP.instances


def patch_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Substitui ``mailer.time.sleep`` por um gravador e devolve os valores pedidos.

    As esperas de 5 s e 30 s nunca são executadas: a lista devolvida é a prova de
    qual política de retentativa o produto aplicou.
    """
    waits: list[float] = []
    monkeypatch.setattr(mailer.time, "sleep", waits.append)
    return waits


def failure_script(
    step: str,
    error: BaseException,
    attempts: int = 3,
) -> list[dict[str, BaseException]]:
    """Roteiro em que todas as ``attempts`` tentativas falham no mesmo passo."""
    return [{step: error} for _index in range(attempts)]


def build_report() -> Report:
    """Relatório direto, sem templates: o mailer não renderiza nada."""
    return Report(subject=SUBJECT, text=TEXT_BODY, html=HTML_BODY)


def build_test_message() -> EmailMessage:
    """A mensagem única da execução, como o ``cli`` a entrega ao ``send``."""
    return mailer.build_message(build_report(), SENDER, RECIPIENTS)


def smtp_config() -> SmtpConfig:
    """Servidor de saída configurado; o dublê nunca resolve nem conecta."""
    return SmtpConfig(host=SMTP_HOST, port=SMTP_PORT)


def credentials(username: str, password: str) -> Credentials:
    """Credenciais do par sentinela, como ``load_credentials`` as produziria."""
    return Credentials(username=username, password=password)


# --- Estrutura MIME (AAP 0.2.1 e 0.5.4) ----------------------------------------


def test_build_message_produz_multipart_alternative_com_texto_antes_do_html() -> None:
    """Duas alternativas, texto simples primeiro e HTML por último."""
    message = build_test_message()

    assert message.get_content_type() == "multipart/alternative"
    assert message.is_multipart()
    parts = message.get_payload()
    assert len(parts) == 2
    # RFC 2046 §5.1.4: o cliente escolhe a última alternativa que sabe exibir.
    assert parts[0].get_content_type() == "text/plain"
    assert parts[1].get_content_type() == "text/html"


def test_build_message_preserva_os_corpos_em_utf8() -> None:
    """Cada parte decodifica exatamente para o corpo do relatório, em UTF-8."""
    parts = build_test_message().get_payload()

    assert parts[0].get_content() == TEXT_BODY
    assert parts[1].get_content() == HTML_BODY
    assert parts[0].get_content_charset() == "utf-8"
    assert parts[1].get_content_charset() == "utf-8"
    assert "Petróleo" in parts[0].get_content()
    assert "Variação" in parts[0].get_content()
    assert "\u2013" in parts[0].get_content()
    assert "\u2013" in parts[1].get_content()


def test_build_message_preenche_os_cabecalhos_da_mensagem() -> None:
    """Remetente, assunto com travessão EN, data, identificador e nenhum Bcc."""
    message = build_test_message()

    assert message["From"] == SENDER
    assert message["Subject"] == SUBJECT
    assert "\u2013" in message["Subject"]
    sent_at = email.utils.parsedate_to_datetime(message["Date"])
    assert sent_at is not None
    assert sent_at.tzinfo is not None
    message_id = str(message["Message-ID"])
    assert message_id
    assert "@" in message_id
    assert message.get_all("Bcc") is None


def test_build_message_endereca_todos_os_destinatarios_em_um_unico_to() -> None:
    """Uma mensagem só, com os três destinatários no mesmo cabeçalho e em ordem."""
    message = build_test_message()

    assert len(message.get_all("To")) == 1
    addresses = [
        address for _name, address in email.utils.getaddresses([str(message["To"])])
    ]
    assert addresses == list(RECIPIENTS)


def test_build_message_sobrevive_a_uma_serializacao_real() -> None:
    """A biblioteca padrão relê a mensagem com partes, charsets e assunto intactos."""
    message = build_test_message()

    reparsed = email.message_from_bytes(bytes(message), policy=email.policy.default)

    assert reparsed.get_content_type() == "multipart/alternative"
    parts = reparsed.get_payload()
    assert [part.get_content_type() for part in parts] == ["text/plain", "text/html"]
    assert [part.get_content_charset() for part in parts] == ["utf-8", "utf-8"]
    assert parts[0].get_content() == TEXT_BODY
    assert parts[1].get_content() == HTML_BODY
    assert str(reparsed["Subject"]) == SUBJECT


# --- Transação de entrega (AAP 0.2.1 e 0.5.1) ----------------------------------


def test_send_abre_uma_unica_conexao_com_host_e_porta_configurados(
    monkeypatch: pytest.MonkeyPatch,
    sentinel_username: str,
    sentinel_password: str,
) -> None:
    """Sucesso devolve ``None`` depois de uma só conexão, com host e porta da config."""
    _fake, instances = install_fake_smtp(monkeypatch)
    patch_sleep(monkeypatch)

    result = mailer.send(
        build_test_message(), smtp_config(), credentials(sentinel_username, sentinel_password)
    )

    assert result is None
    assert len(instances) == 1
    assert instances[0].init_args == (SMTP_HOST, SMTP_PORT)


def test_send_negocia_tls_antes_de_autenticar(
    monkeypatch: pytest.MonkeyPatch,
    sentinel_username: str,
    sentinel_password: str,
) -> None:
    """A ordem é ``ehlo``, ``starttls``, ``ehlo``, ``login`` e só então a mensagem."""
    _fake, instances = install_fake_smtp(monkeypatch)
    patch_sleep(monkeypatch)

    mailer.send(
        build_test_message(), smtp_config(), credentials(sentinel_username, sentinel_password)
    )

    assert instances[0].events[: len(EXPECTED_TRANSACTION)] == EXPECTED_TRANSACTION


def test_send_usa_o_contexto_tls_padrao_que_verifica_certificado(
    monkeypatch: pytest.MonkeyPatch,
    sentinel_username: str,
    sentinel_password: str,
) -> None:
    """O STARTTLS recebe ``ssl.create_default_context()``: sem caminho de downgrade."""
    _fake, instances = install_fake_smtp(monkeypatch)
    patch_sleep(monkeypatch)

    mailer.send(
        build_test_message(), smtp_config(), credentials(sentinel_username, sentinel_password)
    )

    context = instances[0].starttls_context
    assert isinstance(context, ssl.SSLContext)
    assert context.check_hostname is True
    assert context.verify_mode == ssl.CERT_REQUIRED


def test_send_autentica_com_as_credenciais_recebidas(
    monkeypatch: pytest.MonkeyPatch,
    sentinel_username: str,
    sentinel_password: str,
) -> None:
    """O par usuário/senha chega ao ``login`` depois do TLS, como recebido."""
    _fake, instances = install_fake_smtp(monkeypatch)
    patch_sleep(monkeypatch)

    mailer.send(
        build_test_message(), smtp_config(), credentials(sentinel_username, sentinel_password)
    )

    assert instances[0].login_args == (sentinel_username, sentinel_password)


def test_send_entrega_a_mesma_mensagem_uma_vez(
    monkeypatch: pytest.MonkeyPatch,
    sentinel_username: str,
    sentinel_password: str,
) -> None:
    """Uma execução entrega exatamente uma mensagem, e é o objeto que recebeu."""
    _fake, instances = install_fake_smtp(monkeypatch)
    patch_sleep(monkeypatch)
    message = build_test_message()

    mailer.send(message, smtp_config(), credentials(sentinel_username, sentinel_password))

    instance = instances[0]
    assert len(instance.sent_messages) == 1
    assert instance.sent_messages[0] is message
    to_addrs = instance.send_to_addrs[0]
    assert to_addrs is None or list(to_addrs) == list(RECIPIENTS)
    assert instance.delivered == 1


def test_send_nunca_liga_o_nivel_de_depuracao_do_smtplib(
    monkeypatch: pytest.MonkeyPatch,
    sentinel_username: str,
    sentinel_password: str,
) -> None:
    """O diálogo AUTH em base64 nunca é impresso: nem na transação, nem no código."""
    _fake, instances = install_fake_smtp(monkeypatch)
    patch_sleep(monkeypatch)

    mailer.send(
        build_test_message(), smtp_config(), credentials(sentinel_username, sentinel_password)
    )

    assert "set_debuglevel" not in instances[0].events
    assert "set_debuglevel" not in inspect.getsource(mailer)


# --- Política de retentativa (AAP 0.1.2, 0.7.3 e 0.8) --------------------------


def test_send_repete_a_entrega_apos_falha_transitoria(
    monkeypatch: pytest.MonkeyPatch,
    sentinel_username: str,
    sentinel_password: str,
) -> None:
    """Desconexão do servidor na primeira tentativa: espera 5 s e entrega na segunda."""
    transient = smtplib.SMTPServerDisconnected("conexão encerrada pelo servidor")
    _fake, instances = install_fake_smtp(monkeypatch, [{"send_message": transient}])
    waits = patch_sleep(monkeypatch)

    result = mailer.send(
        build_test_message(), smtp_config(), credentials(sentinel_username, sentinel_password)
    )

    assert result is None
    assert len(instances) == 2
    assert waits == [5]
    assert sum(instance.delivered for instance in instances) == 1


def test_falha_persistente_levanta_mail_error(
    monkeypatch: pytest.MonkeyPatch,
    sentinel_username: str,
    sentinel_password: str,
) -> None:
    """Resposta 4xx em todas as tentativas: três tentativas, esperas 5 s e 30 s."""
    error = smtplib.SMTPDataError(451, b"4.3.0 try later")
    _fake, instances = install_fake_smtp(monkeypatch, failure_script("send_message", error))
    waits = patch_sleep(monkeypatch)

    with pytest.raises(mailer.MailError) as caught:
        mailer.send(
            build_test_message(), smtp_config(),
            credentials(sentinel_username, sentinel_password),
        )

    assert len(instances) == 3
    assert waits == [5, 30]
    text = str(caught.value)
    assert SMTP_HOST in text
    assert str(SMTP_PORT) in text
    assert "451" in text


def test_send_repete_conexao_recusada_ate_tres_tentativas(
    monkeypatch: pytest.MonkeyPatch,
    sentinel_username: str,
    sentinel_password: str,
) -> None:
    """Conexão recusada é transitória: três tentativas com 5 s e 30 s, depois erro."""
    refused = ConnectionRefusedError(111, "Connection refused")
    _fake, instances = install_fake_smtp(monkeypatch, failure_script("connect", refused))
    waits = patch_sleep(monkeypatch)

    with pytest.raises(mailer.MailError) as caught:
        mailer.send(
            build_test_message(), smtp_config(),
            credentials(sentinel_username, sentinel_password),
        )

    assert len(instances) == 3
    assert waits == [5, 30]
    text = str(caught.value)
    assert SMTP_HOST in text
    assert str(SMTP_PORT) in text


# --- Falhas que nunca são repetidas (AAP 0.2.1 e 0.7.3) -----------------------

NON_RETRYABLE_FAILURES = (
    pytest.param(
        "login",
        smtplib.SMTPAuthenticationError(535, b"5.7.8 bad credentials"),
        "535",
        id="autenticacao-recusada",
    ),
    pytest.param(
        "send_message",
        smtplib.SMTPDataError(554, b"5.6.0 rejected"),
        "554",
        id="resposta-5xx",
    ),
    pytest.param(
        "starttls",
        smtplib.SMTPNotSupportedError("STARTTLS not supported"),
        "STARTTLS",
        id="starttls-nao-oferecido",
    ),
    pytest.param(
        "starttls",
        ssl.SSLCertVerificationError("certificate verify failed"),
        "certificado",
        id="certificado-invalido",
    ),
)


@pytest.mark.parametrize(("step", "error", "expected_fragment"), NON_RETRYABLE_FAILURES)
def test_send_nao_repete_falha_permanente(
    monkeypatch: pytest.MonkeyPatch,
    sentinel_username: str,
    sentinel_password: str,
    step: str,
    error: BaseException,
    expected_fragment: str,
) -> None:
    """Autenticação recusada, 5xx, STARTTLS ausente e certificado inválido não repetem.

    Cada um deles falha uma única vez, sem espera alguma, e o ``MailError`` cita o
    código do servidor quando existe um.
    """
    _fake, instances = install_fake_smtp(monkeypatch, failure_script(step, error))
    waits = patch_sleep(monkeypatch)

    with pytest.raises(mailer.MailError) as caught:
        mailer.send(
            build_test_message(), smtp_config(),
            credentials(sentinel_username, sentinel_password),
        )

    assert len(instances) == 1
    assert waits == []
    assert expected_fragment in str(caught.value)
    assert SMTP_HOST in str(caught.value)


# --- Sigilo das credenciais (AAP 0.7.2 e critério 6) --------------------------


def test_senha_nao_aparece_na_mensagem_de_erro(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    sentinel_username: str,
    sentinel_password: str,
) -> None:
    """Autenticação recusada: o código aparece, a senha não, em saída nem em log."""
    caplog.set_level(logging.DEBUG)
    error = smtplib.SMTPAuthenticationError(535, b"5.7.8 bad credentials")
    install_fake_smtp(monkeypatch, failure_script("login", error))
    patch_sleep(monkeypatch)

    with pytest.raises(mailer.MailError) as caught:
        mailer.send(
            build_test_message(), smtp_config(),
            credentials(sentinel_username, sentinel_password),
        )

    failure = caught.value
    assert "535" in str(failure)
    assert sentinel_password not in str(failure)
    assert sentinel_password not in repr(failure)
    for chained in (failure.__cause__, failure.__context__):
        if chained is not None:
            assert sentinel_password not in str(chained)
            assert sentinel_password not in repr(chained)
    assert sentinel_password not in caplog.text
    for record in caplog.records:
        assert sentinel_password not in record.getMessage()
        assert sentinel_password not in repr(record.args)


def test_falha_4xx_persistente_nomeia_o_servidor_sem_expor_credenciais(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    sentinel_username: str,
    sentinel_password: str,
) -> None:
    """O aviso de cada nova tentativa cita host, porta e código, nunca a credencial."""
    caplog.set_level(logging.DEBUG)
    error = smtplib.SMTPDataError(451, b"4.3.0 try later")
    install_fake_smtp(monkeypatch, failure_script("send_message", error))
    patch_sleep(monkeypatch)

    with pytest.raises(mailer.MailError) as caught:
        mailer.send(
            build_test_message(), smtp_config(),
            credentials(sentinel_username, sentinel_password),
        )

    text = str(caught.value)
    assert SMTP_HOST in text
    assert str(SMTP_PORT) in text
    assert "451" in text
    assert sentinel_username not in text
    assert sentinel_password not in text
    assert sentinel_username not in caplog.text
    assert sentinel_password not in caplog.text
    for record in caplog.records:
        assert sentinel_password not in record.getMessage()
        assert sentinel_password not in repr(record.args)


def test_repr_das_credenciais_nao_expoe_a_senha(
    sentinel_username: str,
    sentinel_password: str,
) -> None:
    """A representação do objeto carrega o usuário e omite a senha."""
    credencial = credentials(sentinel_username, sentinel_password)

    assert sentinel_username in repr(credencial)
    assert sentinel_password not in repr(credencial)


# --- Identidade da mensagem entre tentativas (AAP 0.9) ------------------------


def test_repeticoes_reenviam_o_mesmo_objeto_de_mensagem(
    monkeypatch: pytest.MonkeyPatch,
    sentinel_username: str,
    sentinel_password: str,
) -> None:
    """Duas falhas transitórias e um sucesso: a mensagem nunca é recomposta."""
    first = smtplib.SMTPServerDisconnected("queda na primeira tentativa")
    second = smtplib.SMTPServerDisconnected("queda na segunda tentativa")
    _fake, instances = install_fake_smtp(
        monkeypatch,
        [{"send_message": first}, {"send_message": second}, {}],
    )
    waits = patch_sleep(monkeypatch)
    message = build_test_message()

    result = mailer.send(
        message, smtp_config(), credentials(sentinel_username, sentinel_password)
    )

    assert result is None
    assert waits == [5, 30]
    assert len(instances) == 3
    assert all(instance.sent_messages[0] is message for instance in instances)
