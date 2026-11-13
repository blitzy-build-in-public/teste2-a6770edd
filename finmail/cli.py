"""Raiz de composição do finmail: comandos, orquestração e códigos de saída.

Este módulo interpreta a linha de comando, orquestra o caminho ``config`` →
``quotes`` → ``report`` → ``mailer`` e traduz cada desfecho em um código de saída
que o ``cron`` distingue. É o único ponto do produto que lê as credenciais do
ambiente e o único que chama os adaptadores.

Os adaptadores entram sob demanda: ``quotes`` e ``report`` dentro de
:func:`handle_run`, e ``mailer`` apenas no ramo que envia. Com isso
``check-config`` não carrega o ``yfinance`` e ``run --dry-run`` não carrega o
``smtplib`` — nenhum dos dois abre conexão de e-mail.

``stdout`` recebe todas as linhas de log, o resumo de ``check-config`` e o bloco
impresso por ``--dry-run``; ``stderr`` recebe apenas as mensagens de uso do
``argparse`` e uma linha de diagnóstico por falha fatal. Nenhum valor de
``SMTP_USERNAME`` ou ``SMTP_PASSWORD`` é impresso, registrado ou interpolado em
mensagem de exceção.
"""

import argparse
import logging
import os
import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import NoReturn
from zoneinfo import ZoneInfo

# O alias evita o conflito entre o módulo ``config`` e o parâmetro ``config`` que
# as assinaturas de handle_run e handle_check_config fixam.
from finmail import config as finmail_config

# Códigos de saída do processo, distinguidos pelo cron do operador.
EXIT_OK = 0
EXIT_UNEXPECTED = 1
EXIT_ALL_QUOTES_FAILED = 2
EXIT_CONFIG = 3
EXIT_SMTP = 4

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
LOG_DATEFMT = "%Y-%m-%dT%H:%M:%S%z"
LOGGER_NAME = "finmail"
# Bibliotecas cujo ruído não pode se confundir com o log do produto: o formato é
# único para as três invocações e não existe opção de nível de log.
QUIET_LOGGERS = ("yfinance", "peewee", "urllib3", "curl_cffi")

# Variáveis de ambiente das credenciais de SMTP; só a presença é consultada aqui.
_USERNAME_VAR = "SMTP_USERNAME"
_PASSWORD_VAR = "SMTP_PASSWORD"

# Delimitador do bloco impresso por ``run --dry-run``, entre o assunto e o corpo.
_DRY_RUN_DELIMITER = "--- corpo texto ---"

_LOGGER = logging.getLogger(LOGGER_NAME)


class FinmailArgumentParser(argparse.ArgumentParser):
    """Parser que encerra com ``3`` em erro de uso, e não com ``2``.

    O ``argparse`` encerra com ``2`` por padrão, exatamente o código que o
    contrato reserva para "todos os ativos falharam". Sobrescrever ``error``
    mantém o ``2`` com um significado único; ``--help`` continua saindo com ``0``
    porque o ``exit()`` de ``argparse`` não é tocado.
    """

    def error(self, message: str) -> NoReturn:
        """Escreve a mensagem de uso em ``stderr`` e encerra com ``EXIT_CONFIG``.

        Args:
            message: diagnóstico do ``argparse``, que nomeia o argumento inválido.

        Raises:
            SystemExit: sempre, com ``EXIT_CONFIG``.
        """
        self.print_usage(sys.stderr)
        self.exit(EXIT_CONFIG, f"{self.prog}: erro de uso: {message}\n")


def main(argv: list[str] | None = None) -> int:
    """Executa um comando e devolve o código de saída do processo.

    É a única raiz de composição: valida a configuração antes de qualquer I/O de
    rede, registra a linha de início e entrega o comando ao handler
    correspondente. Uma falha esperada nunca escapa como exceção — cada desfecho
    vira um código de saída, com uma linha de diagnóstico em ``stderr`` e um
    ``ERROR`` no log.

    Args:
        argv: argumentos da linha de comando sem o nome do programa. ``None`` usa
            ``sys.argv[1:]``, como ``python -m finmail`` fornece.

    Returns:
        ``0`` em sucesso, ``1`` em defeito inesperado, ``2`` quando nenhum ativo
        retornou dados, ``3`` em erro de configuração ou de uso e ``4`` em falha
        de SMTP depois das tentativas.
    """
    parser = _build_parser()
    _configure_logging()
    args = parser.parse_args(argv)

    try:
        configuration = finmail_config.load_config(args.config)
    except finmail_config.ConfigError as exc:
        _fatal(str(exc))
        return EXIT_CONFIG

    # Só depois de a configuração ser conhecida o log passa a datar as linhas no
    # fuso do operador.
    _configure_logging(configuration.timezone)
    _LOGGER.info(
        "Comando '%s' com a configuração '%s' (%s).",
        args.command,
        args.config,
        _count(len(configuration.tickers), "ativo", "ativos"),
    )

    try:
        if args.command == "run":
            return handle_run(configuration, args.dry_run)
        return handle_check_config(configuration)
    except finmail_config.ConfigError as exc:
        _fatal(str(exc))
        return EXIT_CONFIG
    except Exception as exc:  # noqa: BLE001 - classificador final do processo
        # Defeito inesperado: o traceback é permitido aqui porque nenhuma
        # mensagem do produto interpola credencial.
        _LOGGER.exception("falha inesperada durante a execução")
        print(f"falha inesperada: {exc}", file=sys.stderr)
        return EXIT_UNEXPECTED


def handle_run(config: finmail_config.Config, dry_run: bool) -> int:
    """Consulta os ativos, monta o resumo e envia ou imprime o e-mail.

    O caminho é ``config`` → ``quotes`` → ``report`` → ``mailer``, com os ativos
    consultados em sequência, na ordem da configuração. As credenciais só são
    lidas quando ``dry_run`` é falso: o caminho de ``--dry-run`` nunca toca as
    variáveis de ambiente, o que mantém a senha fora da saída e impede qualquer
    conexão de e-mail.

    Args:
        config: configuração já validada, com ativos, destinatários e SMTP.
        dry_run: quando verdadeiro, imprime assunto e corpo texto e retorna sem
            importar ``mailer``.

    Returns:
        ``EXIT_OK`` quando o e-mail é enviado ou impresso, ``EXIT_ALL_QUOTES_FAILED``
        quando nenhum ativo retorna dados e ``EXIT_SMTP`` quando a entrega falha
        depois das tentativas.

    Raises:
        finmail_config.ConfigError: credencial de SMTP ausente no envio; o
            ``main`` a converte em ``EXIT_CONFIG``.
    """
    credentials: finmail_config.Credentials | None = None
    if not dry_run:
        # Único ponto do produto que lê os valores das credenciais.
        credentials = finmail_config.load_credentials(os.environ)

    from finmail import quotes
    from finmail.report import build

    entries: list[quotes.Quote | quotes.QuoteError] = []
    successes = 0
    for ticker in config.tickers:
        try:
            quote = quotes.fetch(ticker)
        except quotes.QuoteError as exc:
            # Falha de um ativo não é fatal: vira a linha "dados indisponíveis"
            # do relatório, na posição que o ticker ocupa na configuração.
            _LOGGER.warning("Ativo %s sem dados: %s.", ticker, exc.reason)
            entries.append(exc)
            continue
        entries.append(quote)
        successes += 1

    if successes == 0:
        _fatal(
            "Nenhum ativo retornou dados "
            f"({_count(len(entries), 'ativo com falha', 'ativos com falha')}); "
            "nenhum e-mail foi enviado."
        )
        return EXIT_ALL_QUOTES_FAILED

    report = build(entries, now=datetime.now(tz=config.timezone))

    if dry_run:
        print(f"Assunto: {report.subject}")
        print(_DRY_RUN_DELIMITER)
        print(report.text, end="" if report.text.endswith("\n") else "\n")
        _LOGGER.info(
            "Resumo de %s impresso no terminal (--dry-run); nenhum e-mail foi enviado.",
            _count(successes, "ativo", "ativos"),
        )
        return EXIT_OK

    from finmail import mailer

    # Uma única mensagem por execução: todos os destinatários no mesmo cabeçalho,
    # montada uma vez e reenviada por mailer.send em caso de nova tentativa.
    message = mailer.build_message(report, config.sender, config.recipients)
    try:
        # ``credentials`` está preenchida aqui: o ramo de --dry-run retorna antes.
        mailer.send(message, config.smtp, credentials)
    except mailer.MailError as exc:
        _fatal(str(exc))
        return EXIT_SMTP

    _LOGGER.info(
        "Resumo de %s enviado a %s por %s:%d.",
        _count(successes, "ativo", "ativos"),
        _count(len(config.recipients), "destinatário", "destinatários"),
        config.smtp.host,
        config.smtp.port,
    )
    return EXIT_OK


def handle_check_config(config: finmail_config.Config) -> int:
    """Imprime o resumo da configuração validada, sem rede e sem credencial.

    Não abre conexão, não importa ``quotes``, ``report`` nem ``mailer`` e não
    chama ``load_credentials``: uma credencial ausente não é erro para este
    comando, apenas uma informação do resumo.

    Args:
        config: configuração já validada por ``main``.

    Returns:
        ``EXIT_OK`` sempre, porque uma configuração inválida nem chega aqui.
    """
    print("Configuração válida.")
    print(f"tickers: {len(config.tickers)}")
    print(f"ativos: {', '.join(config.tickers)}")
    print(f"recipients: {len(config.recipients)}")
    print(f"destinatários: {', '.join(config.recipients)}")
    print(f"sender: {config.sender}")
    print(f"smtp.host: {config.smtp.host}")
    print(f"smtp.port: {config.smtp.port}")
    print(f"timezone: {config.timezone_name}")
    print(f"{_USERNAME_VAR}: {_credential_presence(_USERNAME_VAR)}")
    print(f"{_PASSWORD_VAR}: {_credential_presence(_PASSWORD_VAR)}")
    return EXIT_OK


def _build_parser() -> argparse.ArgumentParser:
    """Monta o parser dos dois subcomandos e da opção global ``--config``.

    ``--config`` existe no nível raiz e é repetido em cada subcomando com
    ``default=argparse.SUPPRESS``: assim a opção vale antes ou depois do
    subcomando e, quando aparece mais de uma vez, a ocorrência mais à direita é a
    que fica no espaço de nomes final.
    """
    config_help = "caminho do arquivo de configuração (padrão: ./config.yaml)"
    parser = FinmailArgumentParser(
        prog="python -m finmail",
        description=(
            "Resumo diário de mercado por e-mail, com cotações do Yahoo Finance."
        ),
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=finmail_config.DEFAULT_CONFIG_PATH,
        metavar="CAMINHO",
        help=config_help,
    )
    subparsers = parser.add_subparsers(
        dest="command",
        required=True,
        metavar="{run,check-config}",
        title="comandos",
    )

    run_parser = subparsers.add_parser(
        "run",
        help="consulta os ativos, monta o resumo e envia o e-mail",
        description=(
            "Consulta cada ativo configurado, monta o resumo de mercado e envia "
            "uma única mensagem a todos os destinatários."
        ),
    )
    run_parser.add_argument(
        "--config",
        type=Path,
        default=argparse.SUPPRESS,
        metavar="CAMINHO",
        help=config_help,
    )
    run_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="consulta e imprime o e-mail no terminal, sem enviar nada",
    )

    check_parser = subparsers.add_parser(
        "check-config",
        help="valida a configuração sem acessar a rede",
        description=(
            "Valida o arquivo de configuração e imprime o resumo, sem consultar "
            "cotações, sem enviar e-mail e sem exigir credenciais."
        ),
    )
    check_parser.add_argument(
        "--config",
        type=Path,
        default=argparse.SUPPRESS,
        metavar="CAMINHO",
        help=config_help,
    )
    return parser


def _configure_logging(timezone: ZoneInfo | None = None) -> None:
    """Configura o logger do produto para escrever em ``stdout``.

    Idempotente: chamado antes de a configuração ser conhecida (fuso local) e de
    novo depois dela (fuso do operador), mantém um único handler e apenas
    reaponta o destino e o formatador. Reapontar o destino a cada chamada também
    mantém a saída correta quando ``sys.stdout`` é substituído entre execuções,
    como acontece na captura dos testes.

    Args:
        timezone: fuso efetivo das datas do log; ``None`` usa o fuso local.
    """
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.INFO)
    logger.propagate = False

    handler = _stdout_handler(logger)
    if handler.stream is not sys.stdout:
        # Troca direta: ``setStream`` faria flush no stream antigo, que pode ser o
        # buffer de uma captura já encerrada.
        handler.stream = sys.stdout
    handler.setFormatter(_OperatorFormatter(timezone))

    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.ERROR)


def _stdout_handler(logger: logging.Logger) -> logging.StreamHandler:
    """Devolve o handler de ``stdout`` do produto, criando-o na primeira chamada."""
    for handler in logger.handlers:
        if isinstance(handler, logging.StreamHandler):
            return handler
    handler = logging.StreamHandler(sys.stdout)
    logger.addHandler(handler)
    return handler


class _OperatorFormatter(logging.Formatter):
    """Formatter cujo ``asctime`` sai em ISO-8601 no fuso do operador.

    O ``logging.Formatter`` padrão converte o instante com ``time.localtime`` e
    formata o ``struct_time`` resultante, que não carrega deslocamento de fuso:
    ``%z`` sairia vazio. Aqui ``converter`` devolve um ``datetime`` já ciente do
    fuso efetivo, de modo que ``%z`` apareça na linha.
    """

    def __init__(self, timezone: ZoneInfo | None) -> None:
        super().__init__(LOG_FORMAT, LOG_DATEFMT)
        self.converter = _timezone_converter(timezone)

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        """Data e hora do registro no fuso efetivo, com o deslocamento."""
        return self.converter(record.created).strftime(datefmt or LOG_DATEFMT)


def _timezone_converter(timezone: ZoneInfo | None) -> Callable[[float], datetime]:
    """Conversor de instante do formatador: ``float`` → ``datetime`` no fuso dado.

    Um ``ZoneInfo`` ausente produz ``datetime`` local sem fuso, e então ``%z``
    sai vazio — o caso das linhas gravadas antes de a configuração ser lida.
    """
    return lambda timestamp: datetime.fromtimestamp(timestamp, timezone)


def _fatal(message: str) -> None:
    """Registra a falha fatal uma única vez: ``ERROR`` no log e em ``stderr``.

    Args:
        message: linha única de diagnóstico, já sem qualquer valor de credencial.
    """
    _LOGGER.error("%s", message)
    print(message, file=sys.stderr)


def _count(quantity: int, singular: str, plural: str) -> str:
    """Contagem com a palavra no singular ou no plural, sem "1 ativos".

    Args:
        quantity: número contado.
        singular: palavra usada quando ``quantity`` é ``1``.
        plural: palavra usada nos demais casos.

    Returns:
        A contagem já concordada, como ``3 ativos`` ou ``1 ativo``.
    """
    return f"{quantity} {singular if quantity == 1 else plural}"


def _credential_presence(variable: str) -> str:
    """Diz se a variável de credencial está definida, sem ler o valor.

    Uma variável vazia conta como não definida, do mesmo modo que
    ``finmail_config.load_credentials`` a trata; apenas a presença é consultada,
    porque ``check-config`` não exige credencial.

    Args:
        variable: nome da variável de ambiente, como ``SMTP_PASSWORD``.

    Returns:
        ``definido`` ou ``não definido``.
    """
    return "definido" if os.environ.get(variable) else "não definido"
