"""Montagem da mensagem MIME e entrega por SMTP com STARTTLS.

Este é o único módulo do pacote que abre conexão de e-mail. A partir do
:class:`~finmail.report.Report` já renderizado ele compõe um único
``multipart/alternative`` — corpo texto primeiro, corpo HTML por último — e o
entrega a todos os destinatários em uma transação SMTP, repetida até
``MAX_ATTEMPTS`` vezes sobre o mesmo objeto de mensagem.

Segurança: as credenciais chegam prontas em :class:`Credentials` e são usadas
exclusivamente como argumentos de ``login()``; nenhum valor delas entra em
cabeçalho, log, corpo ou mensagem de erro. O TLS é sempre negociado antes da
autenticação, com o contexto padrão do sistema (verificação de certificado e de
hostname), e o nível de depuração do ``smtplib`` nunca é habilitado, porque
imprimiria o diálogo AUTH em base64.
"""

import logging
import smtplib
import ssl
import time
from collections.abc import Sequence
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Importados apenas para anotação: em tempo de execução este adaptador depende
    # só da biblioteca padrão e não importa outro adaptador do pacote.
    from finmail.config import Credentials, SmtpConfig
    from finmail.report import Report

_LOGGER = logging.getLogger("finmail.mailer")

# Total de tentativas de entrega da mesma mensagem: 1 transação + 2 repetições.
MAX_ATTEMPTS = 3
# Espera depois da primeira e da segunda tentativa, respectivamente; nunca há
# espera depois da última tentativa.
RETRY_WAITS_SECONDS = (5, 30)
# Tempo máximo de cada transação SMTP: uma execução agendada não pode travar.
ATTEMPT_TIMEOUT = 30

# Falhas que nunca são repetidas. Repetir um login recusado só reenviaria a senha
# ao servidor, e um servidor sem STARTTLS ou com certificado inválido continuará
# assim na tentativa seguinte.
_PERMANENT_ERRORS = (
    smtplib.SMTPAuthenticationError,
    smtplib.SMTPNotSupportedError,
    ssl.SSLCertVerificationError,
)
# Falhas em que uma nova conexão tem chance real de resolver o problema.
_TRANSIENT_ERRORS = (
    smtplib.SMTPServerDisconnected,
    smtplib.SMTPConnectError,
    smtplib.SMTPHeloError,
)

# Passos da transação que dependem de uma extensão anunciada pelo servidor e que
# por isso podem falhar com ``SMTPNotSupportedError``, cada um com o motivo que o
# operador precisa ler: sem a etapa, uma sessão sem AUTH seria descrita como falta
# de STARTTLS e mandaria o operador investigar o lado errado.
_STAGE_STARTTLS = "starttls"
_STAGE_LOGIN = "login"
_STAGE_SEND_MESSAGE = "send_message"
_UNSUPPORTED_STAGE_REASONS = {
    _STAGE_STARTTLS: "servidor não oferece STARTTLS",
    _STAGE_LOGIN: "servidor não oferece autenticação AUTH",
    _STAGE_SEND_MESSAGE: "servidor não aceita endereço com caracteres não ASCII (SMTPUTF8)",
}


class _UnsupportedAtStage(smtplib.SMTPNotSupportedError):
    """Ausência de uma extensão SMTP, junto com o passo que a constatou.

    ``starttls`` levanta ``SMTPNotSupportedError`` quando o servidor não anuncia
    a extensão, ``login`` levanta a mesma classe quando anuncia STARTTLS mas não
    AUTH e ``send_message`` quando precisaria de SMTPUTF8. Como herda de
    ``smtplib.SMTPNotSupportedError``, continua sendo uma falha permanente para
    :func:`_is_retryable`; o texto original do ``smtplib`` é preservado em
    ``str(exc)`` como causa encadeada.
    """

    def __init__(self, stage: str, detail: str) -> None:
        super().__init__(detail)
        self.stage = stage


class MailError(Exception):
    """Falha de entrega do e-mail, depois das tentativas ou para parte dos destinatários.

    Também é levantada quando o servidor aceita a mensagem para alguns
    destinatários e recusa os demais: nesse caso a entrega não é repetida, porque
    duplicaria o e-mail que os aceitos já receberam.

    ``str(exc)`` é a linha única que o operador vê e nomeia o host, a porta e o
    código de resposta do servidor quando existe, por exemplo ``Falha ao enviar
    o e-mail para smtp.exemplo.com:587: autenticação recusada (código 535).``.
    ``reason`` é uma descrição curta em português: a resposta crua do servidor
    não é repetida nela, porque pode conter o nome de usuário, e a senha nunca
    aparece em nenhuma forma.
    """

    def __init__(self, host: str, port: int, reason: str) -> None:
        self.host = host
        self.port = port
        self.reason = reason
        super().__init__(f"Falha ao enviar o e-mail para {host}:{port}: {reason}.")


def build_message(
    report: "Report",
    sender: str,
    recipients: Sequence[str],
) -> EmailMessage:
    """Monta a única mensagem da execução, endereçada a todos os destinatários.

    O corpo é um ``multipart/alternative`` com a parte ``text/plain`` primeiro e
    a parte ``text/html`` por último, ordem exigida pelo RFC 2046 §5.1.4: o
    cliente escolhe a última alternativa que sabe exibir. Todos os destinatários
    viajam em um único cabeçalho ``To``, de modo que a execução nunca produza mais
    de um e-mail.

    Args:
        report: Relatório renderizado, com assunto e os dois corpos.
        sender: Endereço do remetente, já validado pela configuração; o domínio
            dele nomeia o ``Message-ID``.
        recipients: Endereços dos destinatários, já validados pela configuração.

    Returns:
        A mensagem pronta para :func:`send`.
    """
    message = EmailMessage()
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    message["Subject"] = report.subject
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid(domain=_sender_domain(sender))
    # O charset explícito em ambas as partes preserva acentos e o travessão do
    # assunto; set_content + add_alternative é o que produz o multipart esperado.
    message.set_content(report.text, subtype="plain", charset="utf-8")
    message.add_alternative(report.html, subtype="html", charset="utf-8")
    return message


def send(
    message: EmailMessage,
    smtp: "SmtpConfig",
    credentials: "Credentials",
) -> None:
    """Entrega ``message`` aos destinatários do próprio cabeçalho, via STARTTLS.

    A mensagem é montada uma única vez pelo chamador: cada tentativa reenvia o
    mesmo objeto, nunca uma mensagem nova e nunca uma mensagem por destinatário.
    Cada tentativa negocia TLS antes de autenticar e fecha a conexão ao final.

    Args:
        message: Mensagem MIME já completa, produzida por :func:`build_message`.
        smtp: Host e porta do servidor de saída.
        credentials: Par usuário/senha, usado apenas no ``login()``.

    Returns:
        ``None`` quando o servidor aceita a mensagem.

    Raises:
        MailError: quando a entrega falha de forma permanente ou continua
            falhando depois de ``MAX_ATTEMPTS`` tentativas, com a exceção
            original encadeada como causa, e quando o servidor recusa parte dos
            destinatários, caso em que repetir duplicaria o e-mail já entregue.
    """
    attempt = 1
    while True:
        try:
            _deliver(message, smtp, credentials)
        except (smtplib.SMTPException, OSError) as exc:
            if not _is_retryable(exc) or attempt >= MAX_ATTEMPTS:
                reason = _describe_failure(exc, attempt)
                raise MailError(smtp.host, smtp.port, reason) from exc
            wait_seconds = RETRY_WAITS_SECONDS[min(attempt, len(RETRY_WAITS_SECONDS)) - 1]
            _LOGGER.warning(
                "Nova tentativa de envio a %s:%d em %d s (tentativa %d de %d) após %s.",
                smtp.host,
                smtp.port,
                wait_seconds,
                attempt,
                MAX_ATTEMPTS,
                _failure_label(exc),
            )
            time.sleep(wait_seconds)
            attempt += 1
        else:
            return


def _check_ehlo(client: smtplib.SMTP) -> None:
    """Confere a resposta do ``EHLO``, que não levanta exceção por si só.

    ``smtplib.SMTP.ehlo()`` devolve ``(código, resposta)`` e não levanta exceção
    quando o servidor responde algo diferente de 250. Sem esta checagem uma
    saudação 4xx seguiria para o ``starttls``, que falharia por não encontrar a
    extensão: a recusa transitória da saudação perderia as três tentativas e
    chegaria ao operador como se o servidor não oferecesse TLS.

    Raises:
        smtplib.SMTPHeloError: quando a resposta ao ``EHLO`` não é 2xx; a falha é
            transitória e descrita como saudação recusada.
    """
    code, reply = client.ehlo()
    if code // 100 != 2:
        raise smtplib.SMTPHeloError(code, reply)


def _deliver(
    message: EmailMessage,
    smtp: "SmtpConfig",
    credentials: "Credentials",
) -> None:
    """Executa uma transação SMTP completa: TLS primeiro, credencial depois.

    Raises:
        MailError: quando o servidor aceita a mensagem para parte dos
            destinatários e recusa os demais; a entrega não pode ser repetida,
            porque duplicaria o e-mail já recebido pelos aceitos.
    """
    context = ssl.create_default_context()
    refused: dict[str, tuple[int, bytes]] = {}
    try:
        with smtplib.SMTP(smtp.host, smtp.port, timeout=ATTEMPT_TIMEOUT) as client:
            _check_ehlo(client)
            # O RFC 3207 manda descartar o conhecimento obtido antes do TLS, então
            # o EHLO é reenviado depois do STARTTLS e antes de qualquer credencial.
            try:
                client.starttls(context=context)
            except smtplib.SMTPNotSupportedError as exc:
                raise _UnsupportedAtStage(_STAGE_STARTTLS, str(exc)) from exc
            _check_ehlo(client)
            try:
                client.login(credentials.username, credentials.password)
            except smtplib.SMTPNotSupportedError as exc:
                raise _UnsupportedAtStage(_STAGE_LOGIN, str(exc)) from exc
            try:
                refused = client.send_message(message)
            except smtplib.SMTPNotSupportedError as exc:
                raise _UnsupportedAtStage(_STAGE_SEND_MESSAGE, str(exc)) from exc
    except (smtplib.SMTPException, OSError) as exc:
        # O encerramento da sessão também levanta: uma resposta diferente de 221 ao
        # QUIT vira exceção. Com parte dos destinatários já recusada, essa falha de
        # despedida não pode esconder a recusa nem fazer a entrega ser repetida.
        if not refused:
            raise
        raise _partial_refusal(smtp, refused) from exc
    if refused:
        # O ``sendmail`` do smtplib só levanta exceção quando *todos* os
        # destinatários são recusados; com parte deles recusada a mensagem já
        # seguiu para os demais, então repetir a entrega duplicaria o e-mail. A
        # recusa é relatada depois do ``with`` para não ser mascarada.
        raise _partial_refusal(smtp, refused)


def _partial_refusal(smtp: "SmtpConfig", refused: dict[str, tuple[int, bytes]]) -> MailError:
    """Erro de entrega parcial, com os códigos de resposta e sem os endereços.

    Repetir a transação duplicaria a mensagem para os destinatários que o servidor
    aceitou, então esta falha não é retentável: vira :class:`MailError` direto,
    que :func:`send` não captura e portanto não repete. Os códigos de resposta são
    o que o operador precisa ler; os endereços ficam fora do texto.
    """
    codes = [code for code, _reply in refused.values()]
    reason = "parte dos destinatários foi recusada (" + _codes_label(codes) + ")"
    return MailError(smtp.host, smtp.port, reason)


def _is_retryable(exc: BaseException) -> bool:
    """Diz se a falha pode ser repetida em uma nova conexão.

    As famílias de SMTP são reconhecidas antes do caso genérico de ``OSError``,
    porque ``smtplib.SMTPException`` deriva de ``OSError``: na ordem inversa toda
    falha de SMTP — inclusive uma recusa permanente de destinatários — cairia na
    falha de rede e seria repetida.
    """
    if isinstance(exc, _PERMANENT_ERRORS):
        # A recusa de autenticação vem antes da regra de 4xx temporário: um par
        # usuário/senha inválido não tem chance de sucesso na repetição.
        return False
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        # Todos os destinatários foram recusados, então nada foi entregue e
        # repetir não duplica e-mail: uma recusa 4xx (caixa cheia, limite
        # temporário) pode passar na tentativa seguinte, uma 5xx não tem por quê.
        return any(code // 100 == 4 for code, _reply in exc.recipients.values())
    if isinstance(exc, _TRANSIENT_ERRORS):
        return True
    if isinstance(exc, smtplib.SMTPResponseException):
        return exc.smtp_code // 100 == 4
    if isinstance(exc, smtplib.SMTPException):
        # Desfecho desconhecido, como o servidor que não oferece nenhum mecanismo
        # de autenticação utilizável: repetir arriscaria a entrega duplicada que
        # uma execução não pode produzir.
        return False
    if isinstance(exc, OSError):
        # Só chegam aqui erros da etapa de conexão: recusa, tempo esgotado, DNS.
        return True
    return False


def _is_timeout_failure(exc: BaseException) -> bool:
    """Diz se a falha é um tempo esgotado, mesmo quando o ``smtplib`` a embrulha.

    Uma conexão que aceita TCP e nunca responde faz o ``smtplib`` levantar
    ``SMTPServerDisconnected`` com o tempo esgotado dentro, e uma conexão que
    demora a abrir vira ``SMTPConnectError(-1, "timed out")``. Sem esta checagem o
    operador leria que o servidor encerrou a conexão ou veria um código ``-1``, que
    não é resposta de servidor nenhum.
    """
    if isinstance(exc, TimeoutError):
        return True
    if "timed out" in str(exc).lower():
        return True
    return any(
        isinstance(chained, TimeoutError) for chained in (exc.__cause__, exc.__context__)
    )


def _reply_codes(exc: BaseException) -> list[int]:
    """Códigos de resposta que a exceção carrega, distintos e em ordem crescente.

    ``SMTPRecipientsRefused`` guarda um par por destinatário; as demais exceções de
    resposta trazem um código só. Códigos não positivos ficam de fora: o ``smtplib``
    usa ``-1`` para tempo esgotado, que não é resposta do servidor.
    """
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return sorted({code for code, _reply in exc.recipients.values() if code > 0})
    if isinstance(exc, smtplib.SMTPResponseException):
        return [exc.smtp_code] if exc.smtp_code > 0 else []
    return []


def _describe_failure(exc: BaseException, attempts: int) -> str:
    """Descreve a falha em português, sem ecoar a resposta crua do servidor.

    As famílias de SMTP vêm antes do caso genérico de rede e de ``TimeoutError``
    porque ``smtplib.SMTPException`` deriva de ``OSError``: na ordem inversa toda
    falha de SMTP — inclusive uma recusa permanente de destinatários — seria
    descrita como falha de rede, sem o código de resposta.
    """
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return f"autenticação recusada (código {exc.smtp_code})"
    if isinstance(exc, _UnsupportedAtStage):
        return _UNSUPPORTED_STAGE_REASONS.get(
            exc.stage, "servidor não oferece o recurso SMTP necessário"
        )
    if isinstance(exc, smtplib.SMTPNotSupportedError):
        return "servidor não oferece o recurso SMTP necessário"
    if isinstance(exc, ssl.SSLCertVerificationError):
        return "certificado TLS do servidor não pôde ser verificado"
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return (
            "todos os destinatários foram recusados ("
            + _codes_label(_reply_codes(exc))
            + ")"
            + _attempts_suffix(attempts)
        )
    if isinstance(exc, smtplib.SMTPConnectError):
        if _is_timeout_failure(exc):
            return "tempo esgotado" + _attempts_suffix(attempts)
        return _response_reason("falha de conexão", exc.smtp_code) + _attempts_suffix(attempts)
    if isinstance(exc, smtplib.SMTPHeloError):
        return _response_reason("saudação recusada", exc.smtp_code) + _attempts_suffix(attempts)
    if isinstance(exc, smtplib.SMTPResponseException):
        reason = _response_reason("servidor recusou a mensagem", exc.smtp_code)
        return reason + _attempts_suffix(attempts)
    if isinstance(exc, smtplib.SMTPServerDisconnected):
        if _is_timeout_failure(exc):
            return "tempo esgotado" + _attempts_suffix(attempts)
        return "conexão encerrada pelo servidor" + _attempts_suffix(attempts)
    if isinstance(exc, TimeoutError):
        return "tempo esgotado" + _attempts_suffix(attempts)
    if isinstance(exc, smtplib.SMTPException):
        return f"falha de SMTP ({type(exc).__name__})" + _attempts_suffix(attempts)
    if isinstance(exc, OSError):
        return "falha de rede" + _attempts_suffix(attempts)
    return f"falha inesperada ({type(exc).__name__})"


def _attempts_suffix(attempts: int) -> str:
    """Complemento com a contagem de tentativas, presente quando houve repetição."""
    if attempts <= 1:
        return ""
    return f" após {attempts} tentativas"


def _response_reason(reason: str, smtp_code: int) -> str:
    """Motivo seguido do código de resposta, quando o servidor enviou um.

    ``smtplib`` usa ``-1`` quando a resposta recebida não é numérica, e esse valor
    não é resposta de servidor nenhum: citá-lo mandaria o operador procurar um
    código que não existe. Sem código, o motivo fica sozinho, como o de qualquer
    outra falha que não traz um.
    """
    if smtp_code <= 0:
        return reason
    return f"{reason} (código {smtp_code})"


def _codes_label(codes: Sequence[int]) -> str:
    """Formata códigos de resposta SMTP como ``código 550`` ou ``códigos 451 e 550``."""
    distinct = sorted(set(codes))
    if not distinct:
        return "código não informado"
    if len(distinct) == 1:
        return f"código {distinct[0]}"
    return "códigos " + ", ".join(str(code) for code in distinct[:-1]) + f" e {distinct[-1]}"


def _failure_label(exc: BaseException) -> str:
    """Rótulo curto da falha para o aviso de nova tentativa.

    Acrescenta os códigos de resposta que a exceção carrega, e só eles: sem o
    código o aviso não diz o que o servidor respondeu, e com o texto cru da
    resposta ele repetiria o nome de usuário, que não pode ir para o log.
    """
    codes = _reply_codes(exc)
    if not codes:
        return type(exc).__name__
    return f"{type(exc).__name__} ({_codes_label(codes)})"


def _sender_domain(sender: str) -> str | None:
    """Domínio do remetente, usado no ``Message-ID``; ``None`` se não houver."""
    _, separator, domain = sender.strip().rpartition("@")
    if not separator or not domain:
        return None
    return domain
