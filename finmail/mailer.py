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


class MailError(Exception):
    """Falha de entrega do e-mail depois de esgotadas as tentativas.

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
            falhando depois de ``MAX_ATTEMPTS`` tentativas; a exceção original
            fica encadeada como causa.
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
                type(exc).__name__,
            )
            time.sleep(wait_seconds)
            attempt += 1
        else:
            return


def _deliver(
    message: EmailMessage,
    smtp: "SmtpConfig",
    credentials: "Credentials",
) -> None:
    """Executa uma transação SMTP completa: TLS primeiro, credencial depois."""
    context = ssl.create_default_context()
    with smtplib.SMTP(smtp.host, smtp.port, timeout=ATTEMPT_TIMEOUT) as client:
        client.ehlo()
        # O RFC 3207 manda descartar o conhecimento obtido antes do TLS, então o
        # EHLO é reenviado depois do STARTTLS e antes de qualquer credencial.
        client.starttls(context=context)
        client.ehlo()
        client.login(credentials.username, credentials.password)
        client.send_message(message)


def _is_retryable(exc: BaseException) -> bool:
    """Diz se a falha pode ser repetida em uma nova conexão."""
    if isinstance(exc, _PERMANENT_ERRORS):
        # A recusa de autenticação vem antes da regra de 4xx temporário: um par
        # usuário/senha inválido não tem chance de sucesso na repetição.
        return False
    if isinstance(exc, _TRANSIENT_ERRORS):
        return True
    if isinstance(exc, smtplib.SMTPResponseException):
        return exc.smtp_code // 100 == 4
    if isinstance(exc, OSError):
        return True
    # Qualquer outra falha de SMTP tem desfecho desconhecido; repetir arriscaria
    # a entrega duplicada que uma execução não pode produzir.
    return False


def _describe_failure(exc: BaseException, attempts: int) -> str:
    """Descreve a falha em português, sem ecoar a resposta crua do servidor."""
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return f"autenticação recusada (código {exc.smtp_code})"
    if isinstance(exc, smtplib.SMTPNotSupportedError):
        return "servidor não oferece STARTTLS"
    if isinstance(exc, ssl.SSLCertVerificationError):
        return "certificado TLS do servidor não pôde ser verificado"
    # SMTPConnectError, SMTPHeloError e SMTPAuthenticationError derivam de
    # SMTPResponseException: precisam ser reconhecidos antes do caso genérico.
    if isinstance(exc, smtplib.SMTPConnectError):
        return "falha de conexão" + _attempts_suffix(attempts)
    if isinstance(exc, smtplib.SMTPHeloError):
        return f"saudação recusada (código {exc.smtp_code})" + _attempts_suffix(attempts)
    if isinstance(exc, smtplib.SMTPResponseException):
        return f"servidor recusou a mensagem (código {exc.smtp_code})" + _attempts_suffix(attempts)
    if isinstance(exc, smtplib.SMTPServerDisconnected):
        return "conexão encerrada pelo servidor" + _attempts_suffix(attempts)
    if isinstance(exc, TimeoutError):
        return "tempo esgotado" + _attempts_suffix(attempts)
    if isinstance(exc, OSError):
        return "falha de rede" + _attempts_suffix(attempts)
    if isinstance(exc, smtplib.SMTPException):
        return f"falha de SMTP ({type(exc).__name__})"
    return f"falha inesperada ({type(exc).__name__})"


def _attempts_suffix(attempts: int) -> str:
    """Complemento com a contagem de tentativas, presente quando houve repetição."""
    if attempts <= 1:
        return ""
    return f" após {attempts} tentativas"


def _sender_domain(sender: str) -> str | None:
    """Domínio do remetente, usado no ``Message-ID``; ``None`` se não houver."""
    _, separator, domain = sender.strip().rpartition("@")
    if not separator or not domain:
        return None
    return domain
