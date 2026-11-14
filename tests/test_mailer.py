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
repetição para autenticação recusada, resposta 5xx, servidor sem STARTTLS,
certificado inválido, todos os destinatários recusados e ausência de mecanismo de
autenticação, a recusa parcial que não pode ser repetida, a despedida recusada que
não substitui a causa real da falha, a saudação 4xx tratada como falha transitória,
o tempo esgotado embrulhado em desconexão, a fidelidade do dublê às assinaturas do
``smtplib``, a identidade da mensagem entre tentativas, o fuso do cabeçalho
``Date``, que vem do instante recebido e não do relógio do processo, e o sigilo
da senha em toda saída (AAP 0.7.2 e critério 6), inclusive na credencial não
ASCII, que vira falha permanente de entrega sem ecoar o caractere recusado.
"""

import email
import email.policy
import email.utils
import inspect
import logging
import smtplib
import ssl
from collections.abc import Sequence
from datetime import datetime, timedelta
from email.message import EmailMessage
from zoneinfo import ZoneInfo

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
    deve falhar (``connect``, ``ehlo``, ``starttls``, ``login``,
    ``send_message`` ou ``quit``) para a exceção a levantar; uma tentativa além do
    roteiro é limpa. Todo passo chamado pelo produto é anotado em :attr:`events`,
    na ordem em que aconteceu, junto com os argumentos que importam para as
    asserções.

    As assinaturas de ``starttls``, ``send_message`` e ``set_debuglevel`` são as
    do ``smtplib`` real do Python 3.12 — ``context`` só como palavra-chave, e
    ``msg`` e ``debuglevel`` como nomes dos primeiros parâmetros —, de modo que
    uma chamada que o servidor real recusaria também falha neste dublê.

    ``refusals`` é o dicionário que ``send_message`` devolve quando o passo
    roteirizado não falhou: o ``smtplib`` real devolve os destinatários recusados
    quando aceita a mensagem para parte deles, e ``ehlo_replies`` são as respostas
    de ``ehlo`` consumidas uma por chamada, para que uma saudação diferente de
    250 também possa ser roteirizada.
    """

    instances: list["FakeSMTP"] = []
    behaviors: tuple[dict[str, BaseException], ...] = ()
    refusals: dict[str, tuple[int, bytes]] = {}
    ehlo_replies: list[tuple[int, bytes]] = []

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
        replies = type(self).ehlo_replies
        if replies:
            return replies.pop(0)
        return 250, b"OK"

    def starttls(self, *, context: ssl.SSLContext | None = None) -> tuple[int, bytes]:
        # ``context`` é só palavra-chave, como em ``SMTP.starttls`` do 3.12: uma
        # passagem posicional do produto falharia no servidor real e precisa
        # falhar aqui também, em vez de passar despercebida na suíte.
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
        msg: EmailMessage,
        from_addr: str | None = None,
        to_addrs: Sequence[str] | None = None,
        *args: object,
        **kwargs: object,
    ) -> dict[str, tuple[int, bytes]]:
        # O primeiro parâmetro se chama ``msg``, como em ``SMTP.send_message`` do
        # 3.12: o produto precisa chamá-lo assim, por posição ou por esse nome.
        # O argumento é registrado antes da falha: a identidade do objeto é
        # conferida mesmo nas tentativas que o servidor recusou.
        self.events.append("send_message")
        self.sent_messages.append(msg)
        self.send_to_addrs.append(to_addrs)
        self._raise_scripted("send_message")
        self.delivered += 1
        return dict(type(self).refusals)

    def quit(self, *args: object, **kwargs: object) -> tuple[int, bytes]:
        self.events.append("quit")
        # Falha roteirizada de despedida: o ``smtplib`` real levanta quando a
        # resposta ao ``QUIT`` não é 221, e é isso que não pode esconder a recusa.
        self._raise_scripted("quit")
        return 221, b"Bye"

    def close(self, *args: object, **kwargs: object) -> None:
        self.events.append("close")

    def set_debuglevel(self, debuglevel: int) -> None:
        # Registrado para que a ausência dele nas asserções signifique algo. O
        # parâmetro se chama ``debuglevel`` e não tem valor padrão, exatamente
        # como em ``SMTP.set_debuglevel`` do 3.12.
        self.events.append("set_debuglevel")

    def _raise_scripted(self, step: str) -> None:
        error = self.behavior.get(step)
        if error is not None:
            raise error


def install_fake_smtp(
    monkeypatch: pytest.MonkeyPatch,
    behaviors: Sequence[dict[str, BaseException]] = (),
    refusals: dict[str, tuple[int, bytes]] | None = None,
    ehlo_replies: Sequence[tuple[int, bytes]] = (),
) -> tuple[type[FakeSMTP], list[FakeSMTP]]:
    """Instala :class:`FakeSMTP` em ``mailer.smtplib.SMTP`` para um teste.

    Zera a lista de instâncias e fixa o roteiro de falhas por tentativa, o
    dicionário de destinatários recusados que ``send_message`` devolve e as
    respostas de ``ehlo`` consumidas em ordem; devolve ``(classe, instâncias)``.
    ``monkeypatch`` restaura o ``smtplib.SMTP`` real no fim do teste.
    """
    FakeSMTP.instances = []
    FakeSMTP.behaviors = tuple(behaviors)
    FakeSMTP.refusals = dict(refusals) if refusals else {}
    FakeSMTP.ehlo_replies = list(ehlo_replies)
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


def capture_product_logs(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Leva os registros do produto até o ``caplog``, qualquer que seja a ordem.

    ``cli._configure_logging`` fixa ``propagate = False`` no logger ``finmail``
    para que o log do produto tenha um formato único (AAP 0.6.2), e esse valor
    sobrevive ao teste que chamou ``cli.main``. Como ``test_cli.py`` é coletado
    antes deste arquivo, os registros de ``finmail.mailer`` deixariam de alcançar
    o logger raiz, onde o ``caplog`` os recolhe, e as asserções de ausência da
    senha percorreriam um log vazio sem provar nada. O helper devolve a
    propagação só durante o teste (``monkeypatch`` restaura o valor de produção
    ao final), fixa o nível do logger do produto, confirma com uma sonda que a
    captura está viva e descarta a sonda, de modo que os registros que o teste
    examina sejam apenas os emitidos pelo produto.
    """
    monkeypatch.setattr(logging.getLogger("finmail"), "propagate", True)
    caplog.set_level(logging.DEBUG, logger="finmail")

    probe = "sonda de captura do log do produto"
    logging.getLogger("finmail.mailer").warning(probe)
    assert any(record.getMessage() == probe for record in caplog.records), (
        "o caplog não recebeu a sonda: sem captura viva, as asserções de sigilo "
        "que seguem não provariam nada"
    )
    caplog.clear()


# --- Fidelidade do dublê ao smtplib real (AAP 0.9 e 0.15.2) -------------------


def test_dobro_do_smtp_espelha_as_assinaturas_da_biblioteca_padrao() -> None:
    """O dublê recusa o que o ``smtplib`` real do Python 3.12 recusa.

    ``SMTP.starttls`` aceita ``context`` apenas como palavra-chave, e
    ``send_message`` e ``set_debuglevel`` nomeiam seus primeiros parâmetros de
    ``msg`` e ``debuglevel``. Um dublê mais frouxo deixaria a suíte verde com um
    ``starttls(context)`` ou um ``send_message(message=…)`` do produto, e o
    ``TypeError`` só apareceria em produção; estas comparações fecham a porta.
    """
    starttls_real = inspect.signature(smtplib.SMTP.starttls)
    starttls_dobro = inspect.signature(FakeSMTP.starttls)
    send_message_real = inspect.signature(smtplib.SMTP.send_message)
    send_message_dobro = inspect.signature(FakeSMTP.send_message)
    debuglevel_real = inspect.signature(smtplib.SMTP.set_debuglevel)
    debuglevel_dobro = inspect.signature(FakeSMTP.set_debuglevel)

    assert starttls_real.parameters["context"].kind is inspect.Parameter.KEYWORD_ONLY
    assert starttls_dobro.parameters["context"].kind is inspect.Parameter.KEYWORD_ONLY
    # O primeiro parâmetro depois de ``self`` tem o mesmo nome dos dois lados.
    assert list(send_message_real.parameters)[1] == "msg"
    assert list(send_message_dobro.parameters)[1] == "msg"
    assert list(debuglevel_real.parameters)[1] == "debuglevel"
    assert list(debuglevel_dobro.parameters)[1] == "debuglevel"
    # A prova comportamental: a passagem posicional precisa levantar ``TypeError``.
    with pytest.raises(TypeError):
        FakeSMTP(SMTP_HOST, SMTP_PORT).starttls(ssl.create_default_context())


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


# Os dois fusos em jogo em novembro de 2026: -03:00 e +14:00. Um processo tem uma
# única zona local, então o par prova que o deslocamento do cabeçalho vem do
# instante recebido e não do relógio do processo.
DATE_TIMEZONES = (("America/Sao_Paulo", -3), ("Pacific/Kiritimati", 14))


@pytest.mark.parametrize(("zone", "offset_hours"), DATE_TIMEZONES)
def test_build_message_data_do_cabecalho_vem_do_instante_no_fuso_recebido(
    zone: str,
    offset_hours: int,
) -> None:
    """O ``Date`` segue o fuso de ``now`` e nomeia o mesmo dia do assunto.

    Com ``formatdate(localtime=True)`` a data vinha do relógio e do fuso do
    processo: um host em UTC+14 com ``timezone: America/Sao_Paulo`` entregava o
    ``Date`` num dia e o assunto no anterior. Com o instante injetado, o
    deslocamento do cabeçalho é o do fuso configurado, a data de calendário é a de
    ``now`` naquele fuso — a mesma do assunto — e o texto do cabeçalho é
    exatamente o que ``email.utils.format_datetime`` produz.
    """
    now = datetime(2026, 11, 13, 23, 30, tzinfo=ZoneInfo(zone))
    report = build_report()

    message = mailer.build_message(report, SENDER, RECIPIENTS, now=now)

    assert str(message["Date"]) == email.utils.format_datetime(now)
    sent_at = email.utils.parsedate_to_datetime(str(message["Date"]))
    assert sent_at.utcoffset() == timedelta(hours=offset_hours)
    assert sent_at.date() == now.date()
    # O assunto fixo deste módulo carrega a data 2026-11-13: cabeçalho e assunto
    # nomeiam o mesmo dia de calendário.
    assert sent_at.date().isoformat() == report.subject.rsplit(" ", 1)[-1]


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


def test_send_repete_recusa_4xx_de_todos_os_destinatarios(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    sentinel_username: str,
    sentinel_password: str,
) -> None:
    """Recusa 4xx de todos os destinatários é transitória: três tentativas, 5 s e 30 s.

    Com todos recusados nada foi entregue, então repetir não duplica e-mail. O
    código da resposta precisa aparecer tanto no erro final quanto no aviso de
    cada nova tentativa, que é a única linha do operador antes do desfecho.
    """
    capture_product_logs(monkeypatch, caplog)
    refused = {address: (451, b"4.2.0 mailbox busy") for address in RECIPIENTS}
    error = smtplib.SMTPRecipientsRefused(refused)
    _fake, instances = install_fake_smtp(monkeypatch, failure_script("send_message", error))
    waits = patch_sleep(monkeypatch)

    with pytest.raises(mailer.MailError) as caught:
        mailer.send(
            build_test_message(), smtp_config(),
            credentials(sentinel_username, sentinel_password),
        )

    assert len(instances) == 3
    assert waits == [5, 30]
    assert caught.value.reason == (
        "todos os destinatários foram recusados (código 451) após 3 tentativas"
    )
    text = str(caught.value)
    assert SMTP_HOST in text
    assert str(SMTP_PORT) in text
    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 2
    for record in warnings:
        assert "SMTPRecipientsRefused (código 451)" in record.getMessage()


def test_send_repete_saudacao_recusada_em_vez_de_culpar_o_starttls(
    monkeypatch: pytest.MonkeyPatch,
    sentinel_username: str,
    sentinel_password: str,
) -> None:
    """EHLO respondido com 421 é falha transitória de saudação, não falta de STARTTLS.

    ``SMTP.ehlo`` não levanta exceção por si só: sem a checagem do código, a
    tentativa seguiria para o ``starttls``, que falharia com
    ``SMTPNotSupportedError`` — permanente —, e a recusa 4xx da saudação perderia
    as três tentativas e chegaria ao operador como servidor sem TLS.
    """
    _fake, instances = install_fake_smtp(
        monkeypatch, ehlo_replies=[(421, b"4.7.0 service busy")] * 3
    )
    waits = patch_sleep(monkeypatch)

    with pytest.raises(mailer.MailError) as caught:
        mailer.send(
            build_test_message(), smtp_config(),
            credentials(sentinel_username, sentinel_password),
        )

    assert len(instances) == 3
    assert waits == [5, 30]
    assert caught.value.reason == "saudação recusada (código 421) após 3 tentativas"
    assert "STARTTLS" not in str(caught.value)
    # Nenhuma tentativa chegou a pedir TLS: a recusa foi da saudação.
    assert all("starttls" not in instance.events for instance in instances)


def test_send_repete_tempo_esgotado_mesmo_embrulhado_em_desconexao(
    monkeypatch: pytest.MonkeyPatch,
    sentinel_username: str,
    sentinel_password: str,
) -> None:
    """Servidor que aceita a conexão e nunca responde: tempo esgotado, não queda.

    O ``smtplib`` embrulha o tempo esgotado do socket em
    ``SMTPServerDisconnected``. Descrevê-lo como conexão encerrada pelo servidor
    mandaria o operador investigar o lado errado do enlace.
    """
    timeout = smtplib.SMTPServerDisconnected("Connection unexpectedly closed: timed out")
    _fake, instances = install_fake_smtp(monkeypatch, failure_script("send_message", timeout))
    waits = patch_sleep(monkeypatch)

    with pytest.raises(mailer.MailError) as caught:
        mailer.send(
            build_test_message(), smtp_config(),
            credentials(sentinel_username, sentinel_password),
        )

    assert len(instances) == 3
    assert waits == [5, 30]
    assert caught.value.reason == "tempo esgotado após 3 tentativas"
    assert "conexão encerrada pelo servidor" not in str(caught.value)


NON_NUMERIC_CODES = (
    pytest.param(
        "connect",
        smtplib.SMTPConnectError(-1, b"nao numerico"),
        "falha de conexão após 3 tentativas",
        id="saudacao-inicial-invalida",
    ),
    pytest.param(
        "ehlo",
        smtplib.SMTPHeloError(-1, b"nao numerico"),
        "saudação recusada após 3 tentativas",
        id="resposta-ao-ehlo-invalida",
    ),
)


@pytest.mark.parametrize(("step", "error", "expected_reason"), NON_NUMERIC_CODES)
def test_resposta_nao_numerica_nao_cita_codigo_inexistente(
    monkeypatch: pytest.MonkeyPatch,
    sentinel_username: str,
    sentinel_password: str,
    step: str,
    error: BaseException,
    expected_reason: str,
) -> None:
    """Resposta não numérica é repetida e descrita sem um código que não existe.

    O ``smtplib`` devolve ``-1`` quando os três primeiros bytes da resposta não são
    um número de três dígitos. Esse valor não é resposta de servidor nenhum: a
    linha do operador nomeia host e porta e omite o código, em vez de mandá-lo
    procurar um ``-1`` no servidor.
    """
    _fake, instances = install_fake_smtp(monkeypatch, failure_script(step, error))
    waits = patch_sleep(monkeypatch)

    with pytest.raises(mailer.MailError) as caught:
        mailer.send(
            build_test_message(), smtp_config(),
            credentials(sentinel_username, sentinel_password),
        )

    assert len(instances) == 3
    assert waits == [5, 30]
    assert caught.value.reason == expected_reason
    text = str(caught.value)
    assert SMTP_HOST in text
    assert str(SMTP_PORT) in text
    assert "-1" not in text


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
    pytest.param(
        "login",
        smtplib.SMTPNotSupportedError("SMTP AUTH extension not supported by server."),
        "autenticação AUTH",
        id="auth-nao-oferecido",
    ),
    pytest.param(
        "send_message",
        smtplib.SMTPNotSupportedError("One or more addresses require SMTPUTF8"),
        "SMTPUTF8",
        id="smtputf8-nao-oferecido",
    ),
    pytest.param(
        "send_message",
        smtplib.SMTPRecipientsRefused(
            {address: (550, b"5.1.1 no such user") for address in RECIPIENTS}
        ),
        "550",
        id="todos-os-destinatarios-recusados-5xx",
    ),
    pytest.param(
        "login",
        smtplib.SMTPException("No suitable authentication method found."),
        "falha de SMTP",
        id="sem-mecanismo-de-autenticacao",
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
    """Nenhuma falha permanente ou de desfecho desconhecido é repetida.

    Autenticação recusada, 5xx, STARTTLS ausente, certificado inválido, todos os
    destinatários recusados com 5xx, a ausência de qualquer mecanismo de
    autenticação e a ausência de AUTH ou de SMTPUTF8 falham uma única vez, sem
    espera alguma; o ``MailError`` cita o código do servidor quando existe um e,
    nos casos de ``SMTPNotSupportedError``, nomeia a etapa que faltou.
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


def test_recusa_parcial_levanta_mail_error_sem_repetir(
    monkeypatch: pytest.MonkeyPatch,
    sentinel_username: str,
    sentinel_password: str,
) -> None:
    """Parte dos destinatários recusada: uma transação, ``MailError`` e nenhuma repetição.

    O ``smtplib`` devolve um dicionário quando aceita a mensagem para uns e recusa
    outros; a entrega não pode ser repetida porque duplicaria o e-mail já recebido
    pelos aceitos. O texto nomeia host, porta e código de resposta e não repete
    nenhum endereço.
    """
    recusado = {RECIPIENTS[-1]: (550, b"5.1.1 no such user")}
    _fake, instances = install_fake_smtp(monkeypatch, refusals=recusado)
    waits = patch_sleep(monkeypatch)

    with pytest.raises(mailer.MailError) as caught:
        mailer.send(
            build_test_message(), smtp_config(),
            credentials(sentinel_username, sentinel_password),
        )

    assert len(instances) == 1
    assert waits == []
    assert caught.value.reason == "parte dos destinatários foi recusada (código 550)"
    text = str(caught.value)
    assert SMTP_HOST in text
    assert str(SMTP_PORT) in text
    assert len(instances[0].sent_messages) == 1
    assert instances[0].delivered == 1
    for address in RECIPIENTS:
        assert address not in caught.value.reason
        assert address not in text


def test_falha_no_quit_nao_esconde_a_recusa_parcial(
    monkeypatch: pytest.MonkeyPatch,
    sentinel_username: str,
    sentinel_password: str,
) -> None:
    """Despedida recusada depois de uma recusa parcial não vira nova tentativa.

    O ``smtplib`` levanta quando a resposta ao ``QUIT`` não é 221, e essa exceção
    nasce depois de a mensagem já ter seguido para parte dos destinatários. Se ela
    escapasse, o desfecho seria uma falha transitória e a entrega seria repetida,
    duplicando o e-mail de quem já o recebeu: a recusa registrada tem de prevalecer.
    """
    recusado = {RECIPIENTS[-1]: (550, b"5.1.1 no such user")}
    despedida = smtplib.SMTPServerDisconnected("QUIT sem resposta")
    _fake, instances = install_fake_smtp(
        monkeypatch,
        [{"quit": despedida}],
        refusals=recusado,
    )
    waits = patch_sleep(monkeypatch)

    with pytest.raises(mailer.MailError) as caught:
        mailer.send(
            build_test_message(), smtp_config(),
            credentials(sentinel_username, sentinel_password),
        )

    assert len(instances) == 1
    assert waits == []
    assert caught.value.reason == "parte dos destinatários foi recusada (código 550)"
    assert isinstance(caught.value.__cause__, smtplib.SMTPServerDisconnected)


# --- Despedida recusada não substitui a falha em curso -----------------------

# Senha fora do ASCII, fictícia como o par sentinela, usada só para reproduzir o
# ``UnicodeEncodeError`` que a biblioteca padrão levanta ao montar o AUTH PLAIN.
NON_ASCII_PASSWORD = "senha-Café-42"
# Resposta diferente de 221 ao ``QUIT``, a que o ``__exit__`` do ``smtplib``
# transforma em ``SMTPResponseException`` depois do corpo do ``with``.
DESPEDIDA_RECUSADA = smtplib.SMTPResponseException(454, b"4.7.0 despedida recusada")


def non_ascii_credentials_failure() -> UnicodeEncodeError:
    """O ``UnicodeEncodeError`` real de uma credencial que o ``AUTH`` não codifica.

    ``auth`` monta ``\\0usuário\\0senha`` e codifica a resposta em ASCII sem tratar a
    falha, de modo que a exceção carrega o caractere recusado e a posição dele
    dentro dessa resposta: nada disso pode chegar à linha do operador.
    """
    try:
        ("\0%s\0%s" % (SENDER, NON_ASCII_PASSWORD)).encode("ascii")
    except UnicodeEncodeError as exc:
        return exc
    raise AssertionError("a senha do caso precisa conter caractere fora do ASCII")


MASKED_IN_FLIGHT_FAILURES = (
    pytest.param(
        "starttls",
        smtplib.SMTPNotSupportedError("STARTTLS not supported"),
        "servidor não oferece STARTTLS",
        1,
        [],
        id="starttls-nao-oferecido",
    ),
    pytest.param(
        "login",
        smtplib.SMTPNotSupportedError("SMTP AUTH extension not supported by server."),
        "servidor não oferece autenticação AUTH",
        1,
        [],
        id="auth-nao-oferecido",
    ),
    pytest.param(
        "login",
        non_ascii_credentials_failure(),
        "as credenciais SMTP precisam conter apenas caracteres ASCII",
        1,
        [],
        id="credencial-nao-ascii",
    ),
    pytest.param(
        "send_message",
        smtplib.SMTPNotSupportedError("One or more addresses require SMTPUTF8"),
        "servidor não aceita endereço com caracteres não ASCII (SMTPUTF8)",
        1,
        [],
        id="smtputf8-nao-oferecido",
    ),
    pytest.param(
        "ehlo",
        smtplib.SMTPHeloError(421, b"4.7.0 servico ocupado"),
        "saudação recusada (código 421) após 3 tentativas",
        3,
        [5, 30],
        id="saudacao-recusada",
    ),
)


@pytest.mark.parametrize(
    ("step", "error", "expected_reason", "expected_instances", "expected_waits"),
    MASKED_IN_FLIGHT_FAILURES,
)
def test_despedida_recusada_nao_esconde_a_falha_em_curso(
    monkeypatch: pytest.MonkeyPatch,
    sentinel_username: str,
    sentinel_password: str,
    step: str,
    error: BaseException,
    expected_reason: str,
    expected_instances: int,
    expected_waits: list[float],
) -> None:
    """A resposta ao QUIT não substitui a causa que a transação já tinha levantado.

    Cada caso roteiriza a falha do passo e uma despedida com resposta 454 na mesma
    tentativa, que é o que o ``__exit__`` do ``smtplib`` converte em exceção. Se ela
    escapasse, o desfecho viria da despedida: as falhas permanentes seriam repetidas
    três vezes — reenviando a credencial não ASCII ao servidor — e o operador leria
    ``servidor recusou a mensagem (código 454)`` no lugar do motivo real. A recusa
    do ``EHLO`` é transitória e por isso se repete, mas o motivo continua sendo a
    saudação recusada, nunca a despedida.
    """
    script = [
        {step: error, "quit": DESPEDIDA_RECUSADA} for _tentativa in range(expected_instances)
    ]
    _fake, instances = install_fake_smtp(monkeypatch, script)
    waits = patch_sleep(monkeypatch)

    with pytest.raises(mailer.MailError) as caught:
        mailer.send(
            build_test_message(), smtp_config(),
            credentials(sentinel_username, sentinel_password),
        )

    assert len(instances) == expected_instances
    assert waits == expected_waits
    assert caught.value.reason == expected_reason
    text = str(caught.value)
    assert "454" not in text
    assert "454" not in caught.value.reason
    assert sentinel_username not in text
    assert sentinel_password not in text
    assert NON_ASCII_PASSWORD not in text
    # A despedida foi tentada em toda tentativa: é ela que poderia mascarar a causa.
    assert all("quit" in instance.events for instance in instances)
    # E a exceção dela não substitui a causa em nenhum elo da cadeia encadeada.
    encadeada: BaseException | None = caught.value
    while encadeada is not None:
        assert encadeada is not DESPEDIDA_RECUSADA
        encadeada = encadeada.__cause__


def test_despedida_recusada_apos_entrega_aceita_ainda_repete_a_entrega(
    monkeypatch: pytest.MonkeyPatch,
    sentinel_username: str,
    sentinel_password: str,
) -> None:
    """Sem falha em curso, a despedida recusada continua sendo falha transitória.

    Aqui a transação inteira foi aceita e só o ``QUIT`` foi respondido com 454: é a
    janela de duplicidade que a AAP aceita depois do ``DATA``. Guardar a falha em
    curso não pode engolir este caso — ele precisa continuar virando nova tentativa,
    com três conexões e esperas de 5 s e 30 s, senão a entrega deixaria de ser
    repetida quando o pedido manda repeti-la.
    """
    _fake, instances = install_fake_smtp(
        monkeypatch, failure_script("quit", DESPEDIDA_RECUSADA)
    )
    waits = patch_sleep(monkeypatch)

    with pytest.raises(mailer.MailError) as caught:
        mailer.send(
            build_test_message(), smtp_config(),
            credentials(sentinel_username, sentinel_password),
        )

    assert len(instances) == 3
    assert waits == [5, 30]
    assert caught.value.reason == (
        "servidor recusou a mensagem (código 454) após 3 tentativas"
    )
    # Cada tentativa teve a mensagem aceita antes da despedida: é a duplicidade que
    # a AAP aceita nessa janela, e é por isso que ela não pode ser silenciada aqui.
    assert all(instance.delivered == 1 for instance in instances)


def test_defeito_interno_da_transacao_nao_vira_erro_de_smtp(
    monkeypatch: pytest.MonkeyPatch,
    sentinel_username: str,
    sentinel_password: str,
) -> None:
    """Defeito interno da transação atravessa o envio intacto, mesmo com QUIT recusado.

    Uma exceção que não é de SMTP nem de rede não pode ser substituída pela resposta
    ao ``QUIT``: ela continua atravessando :func:`send`, que não captura essa classe,
    até a captura final do ``cli``, onde vira código de saída 1 em vez de falha de
    entrega repetida três vezes.
    """
    defeito = TypeError("defeito interno da transação")
    _fake, instances = install_fake_smtp(
        monkeypatch, [{"send_message": defeito, "quit": DESPEDIDA_RECUSADA}]
    )
    waits = patch_sleep(monkeypatch)

    with pytest.raises(TypeError) as caught:
        mailer.send(
            build_test_message(), smtp_config(),
            credentials(sentinel_username, sentinel_password),
        )

    assert caught.value is defeito
    assert len(instances) == 1
    assert waits == []


# --- Sigilo das credenciais (AAP 0.7.2 e critério 6) --------------------------


def test_senha_nao_aparece_na_mensagem_de_erro(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    sentinel_username: str,
    sentinel_password: str,
) -> None:
    """Autenticação recusada: o código aparece, a senha não, em saída nem em log."""
    capture_product_logs(monkeypatch, caplog)
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
    capture_product_logs(monkeypatch, caplog)
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
    # As duas tentativas recusadas deixam um WARNING cada: a captura precisa tê-los.
    assert caplog.records
    for record in caplog.records:
        assert sentinel_password not in record.getMessage()
        assert sentinel_password not in repr(record.args)


def test_credencial_nao_ascii_vira_falha_permanente_sem_ecoa_la(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    sentinel_username: str,
) -> None:
    """Credencial fora do ASCII: falha permanente de entrega, sem ecoar o caractere.

    O ``smtplib`` monta a resposta inicial do AUTH PLAIN como ``\\0usuário\\0senha`` e
    a codifica em ASCII sem tratar a falha, então uma senha acentuada levanta
    ``UnicodeEncodeError`` — classe que não é ``SMTPException`` nem ``OSError`` e
    que, sem classificação, atravessaria o tratador de :func:`send` até a captura
    final do ``cli``. O texto dela cita o caractere recusado e a posição dele na
    resposta, de modo que um trecho da credencial chegaria ao ``stdout`` e ao
    ``stderr`` como defeito interno (código 1), em vez de falha de entrega
    (código 4) sem eco algum.
    """
    capture_product_logs(monkeypatch, caplog)
    senha_nao_ascii = "senha-Café-42"
    # A falha roteirizada é a que a própria biblioteca padrão produz, não um texto
    # escrito à mão: ``auth_plain`` monta a mesma resposta que ``auth`` codifica.
    with pytest.raises(UnicodeEncodeError) as encode_failure:
        ("\0%s\0%s" % (sentinel_username, senha_nao_ascii)).encode("ascii")
    erro = encode_failure.value
    recusado = erro.object[erro.start:erro.end]
    escape = ascii(recusado)[1:-1]
    _fake, instances = install_fake_smtp(monkeypatch, failure_script("login", erro))
    waits = patch_sleep(monkeypatch)

    with pytest.raises(mailer.MailError) as caught:
        mailer.send(
            build_test_message(), smtp_config(),
            credentials(sentinel_username, senha_nao_ascii),
        )

    failure = caught.value
    assert failure.reason == "as credenciais SMTP precisam conter apenas caracteres ASCII"
    text = str(failure)
    assert SMTP_HOST in text
    assert str(SMTP_PORT) in text
    assert isinstance(failure.__cause__, UnicodeEncodeError)
    assert len(instances) == 1
    assert waits == []
    for renderizacao in (text, repr(failure), caplog.text):
        assert senha_nao_ascii not in renderizacao
        assert recusado not in renderizacao
        assert escape not in renderizacao
        assert "position" not in renderizacao
    for record in caplog.records:
        for renderizacao in (record.getMessage(), repr(record.args)):
            assert senha_nao_ascii not in renderizacao
            assert recusado not in renderizacao
            assert escape not in renderizacao
            assert "position" not in renderizacao


def test_repr_e_str_das_credenciais_nao_expoem_os_valores(
    sentinel_username: str,
    sentinel_password: str,
) -> None:
    """Nem a representação nem o texto do objeto carregam o usuário ou a senha."""
    credencial = credentials(sentinel_username, sentinel_password)

    assert "Credentials(" in repr(credencial)
    assert sentinel_username not in repr(credencial)
    assert sentinel_password not in repr(credencial)
    assert sentinel_username not in str(credencial)
    assert sentinel_password not in str(credencial)


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
