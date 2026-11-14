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
``argparse`` e uma linha de diagnóstico por falha fatal, com uma única exceção
deliberada: os avisos do módulo ``warnings`` sobre perda de verificação de TLS, que
o produto não silencia, para que a degradação de segurança continue visível. Nenhum
valor de ``SMTP_USERNAME`` ou ``SMTP_PASSWORD`` é impresso, registrado ou
interpolado em mensagem de exceção. Os dois streams são fixados em UTF-8 no início
de :func:`main`, para que nem o resumo acentuado nem o travessão do assunto
dependam do locale do host.
"""

import argparse
import copy
import logging
import os
import re
import sys
import warnings
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
# Bibliotecas cujo ruído não pode se confundir com o log do produto nem escapar para
# ``stderr``: ficam em ``ERROR`` com destino nulo, e não existe opção de nível de log.
QUIET_LOGGERS = ("yfinance", "peewee", "urllib3", "curl_cffi")

# ``warnings.showwarning`` escreve direto em ``stderr``, o stream reservado à linha única
# de diagnóstico fatal e às mensagens de uso; por isso os avisos das bibliotecas ficam
# ignorados. A lista cobre os namespaces de :data:`QUIET_LOGGERS` e as bibliotecas que
# entram junto com a de cotações, e cada padrão é ancorado em ``(?:\.|$)`` para que um
# módulo de nome parecido não case por prefixo.
_THIRD_PARTY_WARNING_MODULES = r"^(?:yfinance|peewee|urllib3|curl_cffi|pandas|numpy)(?:\.|$)"
# O produto nunca chama ``warnings.warn``: um aviso atribuído a ``finmail.*`` é um aviso
# de biblioteca que ``stacklevel`` deslocou para fora dela, e quebraria o mesmo contrato.
_PRODUCT_WARNING_MODULES = r"^finmail(?:\.|$)"
# Os namespaces cujo ruído o produto cala, na ordem em que os filtros são instalados.
_WARNING_MODULES = (_THIRD_PARTY_WARNING_MODULES, _PRODUCT_WARNING_MODULES)
# As categorias que carregam o ruído das bibliotecas e podem chegar a ``stderr`` são as
# de depreciação, de futuro, de uso e de tempo de execução — não ``Warning`` inteira. A
# família ``SecurityWarning``/``HTTPWarning`` do ``urllib3`` fica de fora dela:
# ``InsecureRequestWarning``, ``NotOpenSSLWarning`` e ``SystemTimeWarning`` derivam de
# ``SecurityWarning`` < ``HTTPWarning`` < ``Warning`` e nenhuma deriva de ``UserWarning``,
# de modo que a perda de verificação de TLS continua visível. O aviso benigno de
# biblioteca SSL não suportada recebe um filtro explícito, mais abaixo.
_BENIGN_WARNING_CATEGORIES = (
    DeprecationWarning,
    PendingDeprecationWarning,
    FutureWarning,
    UserWarning,
    RuntimeWarning,
)

# Caracteres de controle de C0, DEL e C1: um deles dividiria a linha do evento em duas
# linhas físicas ou mudaria o estado do terminal do operador, então o formato do log os
# escapa antes de gravar.
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f]")

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

    É a única raiz de composição: fixa a codificação dos streams, entrega o comando
    a :func:`_dispatch` e descarrega a saída padrão antes do fim do processo. Uma
    falha esperada nunca escapa como exceção — cada desfecho vira um código de
    saída, com uma linha de diagnóstico em ``stderr`` e um ``ERROR`` no log.

    A captura final de ``Exception`` fica aqui e envolve todo o :func:`_dispatch`,
    inclusive a montagem do parser, a configuração do log e a leitura dos argumentos:
    um defeito levantado antes do handler do comando também vira ``1``, com o
    traceback no ``ERROR`` de ``stdout`` e uma única linha em ``stderr``. Somam-se a
    ela os dois desfechos que o mapa de códigos não classifica por si, a interrupção do
    operador e o fechamento do pipe por quem lia ``stdout``, que entram no mesmo código
    de falha não classificada.

    Args:
        argv: argumentos da linha de comando sem o nome do programa. ``None`` usa
            ``sys.argv[1:]``, como ``python -m finmail`` fornece.

    Returns:
        ``0`` em sucesso, ``1`` em defeito inesperado — inclusive quando o operador
        interrompe a execução ou o leitor de ``stdout`` fecha o pipe —, ``2`` quando
        nenhum ativo retornou dados, ``3`` em erro de configuração ou de uso e ``4``
        em falha de SMTP depois das tentativas.

    Raises:
        SystemExit: apenas no erro de uso do ``argparse``, que
            :class:`FinmailArgumentParser` encerra com ``EXIT_CONFIG``.
    """
    _configure_stream_encoding()
    try:
        code = _dispatch(argv)
    except KeyboardInterrupt:
        if not _LOGGER.handlers:
            # Uma interrupção que chega antes de ``_dispatch`` configurar o log: sem
            # handler, o ``lastResort`` repetiria a linha em ``stderr``.
            _configure_logging()
        _fatal("Execução interrompida pelo operador.")
        code = EXIT_UNEXPECTED
    except Exception as exc:
        # Captura ampla de propósito: este é o classificador final do processo e é o
        # que converte um defeito inesperado no código de saída 1. Ele fica aqui, e não
        # dentro de ``_dispatch``, para cobrir também a montagem do parser, a
        # configuração do log e a leitura dos argumentos. ``SystemExit`` do ``argparse``
        # não deriva de ``Exception`` e continua atravessando. O traceback é permitido
        # aqui porque nenhuma mensagem do produto interpola credencial.
        if _ensure_logging():
            _LOGGER.exception("falha inesperada durante a execução")
        # O mesmo escape de :func:`_fatal`: a linha única que o README promete em
        # ``stderr`` não pode ser dividida por um texto vindo de fora do produto.
        print(_escape_control_characters(f"falha inesperada: {exc}"), file=sys.stderr)
        code = EXIT_UNEXPECTED
    if not _flush_stdout() and code == EXIT_OK:
        # A causa não é nomeada aqui de propósito: o descarrego também falha por um
        # erro do sistema de arquivos, e culpar o leitor do pipe mentiria para o cron.
        _fatal(
            "Saída padrão interrompida: a escrita em stdout falhou e a saída do "
            "comando ficou incompleta."
        )
        code = EXIT_UNEXPECTED
    return code


def _dispatch(argv: list[str] | None) -> int:
    """Interpreta a linha de comando, valida a configuração e entrega o comando.

    Concentra o corpo da execução: monta o parser, configura o log, lê a
    configuração, registra a linha de início e chama o handler do comando, com um
    classificador por desfecho esperado — ``ConfigError`` vira ``EXIT_CONFIG``. Um
    defeito que ele não classifica propaga para :func:`main`, onde fica a captura
    final de ``Exception``. Não trata a interrupção do operador nem o fechamento de
    ``stdout``, que ficam em :func:`main`, e não toca nos streams.

    Args:
        argv: argumentos da linha de comando sem o nome do programa.

    Returns:
        O código de saída do desfecho: ``EXIT_OK``, ``EXIT_ALL_QUOTES_FAILED``,
        ``EXIT_CONFIG`` ou ``EXIT_SMTP``.

    Raises:
        KeyboardInterrupt: quando o operador interrompe a execução.
        SystemExit: no erro de uso do ``argparse``, com ``EXIT_CONFIG``.
        Exception: qualquer defeito não classificado, que :func:`main` converte em
            ``EXIT_UNEXPECTED``.
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

    # A biblioteca de cotações entra no processo aqui e, na importação, repõe à frente
    # da lista o filtro de avisos de depreciação dela; refazer o filtro do produto
    # depois disso é o que mantém esse ruído fora de ``stderr``.
    _configure_warnings()

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
        _LOGGER.info(
            "Imprimindo o resumo de %s no terminal (--dry-run); nenhum e-mail foi enviado.",
            _count(successes, "ativo", "ativos"),
        )
        print(f"Assunto: {report.subject}")
        print(_DRY_RUN_DELIMITER)
        print(report.text, end="" if report.text.endswith("\n") else "\n")
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
    print(f"recipients: {len(config.recipients)}")
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


def _configure_stream_encoding() -> None:
    """Fixa ``stdout`` e ``stderr`` em UTF-8, qualquer que seja o locale do host.

    A saída do produto é pt-BR: o resumo de ``check-config``, o assunto com travessão
    e as linhas de log carregam acentos e pontuação que um stream configurado pelo
    ambiente — ``PYTHONIOENCODING=ascii``, um locale POSIX sem a coerção do PEP 538,
    o ``cron`` do macOS — recusaria com ``UnicodeEncodeError``. Fixar UTF-8 aqui faz o
    contrato de streams não depender do host, e ``backslashreplace`` garante que uma
    escrita nunca falhe por um caractere que o destino não represente.

    Um stream substituído por objeto sem ``reconfigure``, como o ``StringIO`` de um
    teste, é deixado como está; um stream já fechado ou desanexado, que levanta
    ``ValueError`` ou ``OSError``, é ignorado em vez de interromper a execução.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="backslashreplace")
        except (ValueError, OSError):
            # Stream fechado ou desanexado: não há o que reconfigurar.
            continue


def _configure_logging(timezone: ZoneInfo | None = None) -> None:
    """Configura o logger do produto para escrever em ``stdout``.

    Idempotente: chamado antes de a configuração ser conhecida (fuso local) e de
    novo depois dela (fuso do operador), mantém um único handler e apenas
    reaponta o destino e o formatador. Reapontar o destino a cada chamada também
    mantém a saída correta quando ``sys.stdout`` é substituído entre execuções,
    como acontece na captura dos testes. Os loggers de :data:`QUIET_LOGGERS` ficam
    em ``ERROR`` com ``propagate = False`` e um destino nulo, de modo que o ruído
    deles não se confunda com o log do produto nem escape para ``stderr``; e o
    filtro que mantém os avisos das bibliotecas fora de ``stderr`` é reposto a cada
    chamada.

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
        quiet = logging.getLogger(name)
        quiet.setLevel(logging.ERROR)
        quiet.propagate = False
        # O nível sozinho não basta: sem handler na cadeia inteira, o registro que
        # passa pelo nível é escrito em ``stderr`` por ``logging.lastResort``, o
        # stream que o contrato reserva à linha única de diagnóstico fatal.
        if not quiet.handlers:
            quiet.addHandler(logging.NullHandler())
    # Um logger de biblioteca fora de :data:`QUIET_LOGGERS` ainda propaga até a raiz;
    # o handler nulo na raiz, quando ela não tem nenhum, fecha o mesmo caminho.
    root = logging.getLogger()
    if not root.handlers:
        root.addHandler(logging.NullHandler())
    _configure_warnings()


def _ensure_logging() -> bool:
    """Configura o log quando ainda não há handler e diz se ele é utilizável.

    O classificador final de :func:`main` roda também quando o defeito aconteceu antes de
    :func:`_configure_logging`, e sem handler o ``logging.lastResort`` escreveria o
    traceback em ``stderr``, dividindo a linha única de diagnóstico. Uma segunda falha
    aqui — o log não pôde ser configurado, provavelmente pelo mesmo defeito que trouxe a
    execução até este ponto — não pode escapar: a linha única de ``stderr`` e o código de
    saída continuam valendo, e repetir a falha esconderia o defeito original.

    Returns:
        ``True`` quando o logger do produto tem handler e o registro chega a ``stdout``;
        ``False`` quando o log não pôde ser configurado.
    """
    if _LOGGER.handlers:
        return True
    try:
        _configure_logging()
    except Exception:
        return False
    return bool(_LOGGER.handlers)


def _configure_warnings() -> None:
    """Mantém o ruído das bibliotecas fora de ``stderr`` sem calar os avisos de segurança.

    ``stderr`` carrega a linha única de diagnóstico fatal e as mensagens de uso do
    ``argparse``, e a única exceção são os avisos de segurança que este módulo deixa
    passar de propósito. Um aviso não passa por logger: ``warnings.showwarning``
    escreve direto em ``stderr``, e a biblioteca de cotações ainda liga o próprio filtro
    de depreciação quando é importada, inserindo-o no início da lista; por isso os
    filtros deste produto são repostos à frente a cada chamada, inclusive depois de a
    biblioteca entrar no processo.

    O que fica ignorado é cada categoria de :data:`_BENIGN_WARNING_CATEGORIES` em cada
    padrão de :data:`_WARNING_MODULES`, e não ``Warning`` inteira: a família de avisos de
    segurança do ``urllib3``, com ``InsecureRequestWarning`` incluído, continua chegando
    a ``stderr``, para que uma futura perda de verificação de TLS apareça. O aviso
    benigno de biblioteca SSL não suportada ganha um filtro explícito, porque a categoria
    dele fica fora desse conjunto.

    O namespace ``finmail`` entra na lista porque o produto não emite aviso algum: um
    aviso atribuído a um módulo do produto só pode ser um aviso de biblioteca que
    ``stacklevel`` deslocou para fora dela, e ele quebraria o contrato do mesmo modo.

    Se o operador pediu avisos como erro em ``-W``/``PYTHONWARNINGS``, nada é instalado:
    os filtros do produto entram no início de ``warnings.filters``, à frente dos que o
    interpretador montou a partir dessas opções, e calar o namespace de uma biblioteca
    sobreporia a escolha dele.

    Idempotente: cada chamada deixa uma única cópia de cada filtro do produto, mesmo com
    as duas chamadas de :func:`_configure_logging` de uma execução.
    """
    if _warnings_are_errors():
        return
    product_filters = [
        (category, module)
        for module in _WARNING_MODULES
        for category in _BENIGN_WARNING_CATEGORIES
    ]
    not_openssl = _not_openssl_warning()
    if not_openssl is not None:
        product_filters.append((not_openssl, _THIRD_PARTY_WARNING_MODULES))
    for category, module in product_filters:
        warnings.filterwarnings("ignore", category=category, module=module)
    installed = warnings.filters
    own = {
        _warning_filter_identity(entry) for entry in installed[: len(product_filters)]
    }
    # As inserções acima repetem as entradas a cada chamada; manter só a primeira cópia
    # de cada filtro impede que a lista cresça a cada execução dentro do mesmo processo.
    installed[len(product_filters):] = [
        entry
        for entry in installed[len(product_filters):]
        if _warning_filter_identity(entry) not in own
    ]


def _warnings_are_errors() -> bool:
    """Diz se o operador pediu avisos como erro em ``-W`` ou em ``PYTHONWARNINGS``.

    ``sys.warnoptions`` guarda cada entrada dessas opções como o operador a escreveu, e a
    ação é o texto que antecede o primeiro ``:`` — ``error`` em ``-W error`` e em
    ``error::DeprecationWarning``. É essa lista que revela o pedido dele antes de
    qualquer filtro ter sido instalado.

    Returns:
        ``True`` quando alguma entrada pede a ação ``error``.
    """
    return any(option.split(":", 1)[0].strip() == "error" for option in sys.warnoptions)


def _not_openssl_warning() -> type[Warning] | None:
    """Devolve ``urllib3.exceptions.NotOpenSSLWarning``, se a classe já estiver carregada.

    O aviso de biblioteca SSL não suportada é benigno e chegaria a ``stderr`` em um host
    cujo CPython foi compilado contra LibreSSL, mas a categoria dele fica fora de
    :data:`_BENIGN_WARNING_CATEGORIES`. A classe é procurada apenas em ``sys.modules``:
    ``urllib3`` é dependência transitiva que o produto não importa, e ``check-config``
    só pode carregar a biblioteca padrão e :mod:`finmail.config`.

    Returns:
        A classe de aviso quando ``urllib3.exceptions`` está em ``sys.modules`` e expõe o
        nome, e ``None`` quando a biblioteca não entrou no processo.
    """
    module = sys.modules.get("urllib3.exceptions")
    if module is None:
        return None
    candidate = getattr(module, "NotOpenSSLWarning", None)
    if isinstance(candidate, type) and issubclass(candidate, Warning):
        return candidate
    return None


def _warning_filter_identity(entry: tuple[object, ...]) -> tuple[object, ...]:
    """Reduz uma entrada de ``warnings.filters`` a uma forma comparável entre chamadas.

    ``warnings.filterwarnings`` guarda o módulo como expressão regular compilada, e duas
    compilações do mesmo padrão são objetos distintos; comparar as entradas como estão
    deixaria passar a cópia repetida de um filtro do produto. O padrão em texto, quando
    existe, é o que dá identidade estável à entrada.

    Args:
        entry: entrada ``(ação, mensagem, categoria, módulo, linha)`` de ``warnings.filters``.

    Returns:
        A mesma entrada com o módulo reduzido ao texto do padrão.
    """
    action, message, category, module, lineno = entry
    return (action, message, category, getattr(module, "pattern", module), lineno)


def _stdout_handler(logger: logging.Logger) -> logging.StreamHandler:
    """Devolve o handler de ``stdout`` do produto, criando-o na primeira chamada."""
    for handler in logger.handlers:
        if isinstance(handler, logging.StreamHandler):
            return handler
    handler = _StdoutHandler(sys.stdout)
    logger.addHandler(handler)
    return handler


def _escape_control_characters(value: str) -> str:
    """Troca cada caractere de controle por ``\\xNN`` em hexadecimal minúsculo.

    O log do produto ocupa uma linha por evento e a linha fatal de ``stderr`` também
    ocupa uma só: um ``\\n`` dentro de um valor interpolado dividiria o evento em duas
    linhas físicas, e um escape ANSI mudaria o estado do terminal do operador. Todo
    caractere imprimível — ``á``, ``ç``, ``ã`` e o travessão inclusive — passa intacto.

    Idempotente: a saída é formada só por caracteres imprimíveis, então escapar de novo
    o resultado não muda nada.

    Args:
        value: texto do produto ou vindo de fora dele, como o ticker da configuração.

    Returns:
        O mesmo texto com C0 (0x00-0x1f), DEL (0x7f) e C1 (0x80-0x9f) escritos como
        ``\\xNN``.
    """
    return _CONTROL_CHARS.sub(lambda match: f"\\x{ord(match.group()):02x}", value)


def _flush_stdout() -> bool:
    """Descarrega ``stdout`` e diz se a saída do comando chegou inteira ao destino.

    Um leitor que fecha o pipe antes do fim da execução faz a próxima escrita
    levantar ``BrokenPipeError``; sem tratamento, o interpretador ainda imprime
    ``Exception ignored in: <_io.TextIOWrapper ...>`` na finalização e sai com um
    código que o mapa do produto não tem. A saída documentada para esse caso é
    abandonar o descritor — :func:`_abandon_stdout` — e deixar o desfecho ser
    classificado como falha. Um stream já fechado, que levanta ``ValueError``, e um
    erro de escrita do sistema operacional caem no mesmo tratamento.

    Returns:
        ``True`` quando a saída foi descarregada por inteiro; ``False`` quando não
        foi possível descarregá-la e ``stdout`` teve de ser abandonado.
    """
    try:
        sys.stdout.flush()
    except (OSError, ValueError):
        _abandon_stdout()
        return False
    return True


def _abandon_stdout() -> None:
    """Aponta o descritor de ``sys.stdout`` para ``os.devnull``.

    É o remédio documentado para o pipe fechado: as escritas que restarem — inclusive
    o ``flush`` que o interpretador faz na finalização — passam a ter um destino
    válido, de modo que o processo termina com o código de saída do desfecho e sem
    ``Exception ignored`` em ``stderr``. Um stream sem descritor utilizável não tem o
    que redirecionar e é deixado como está.
    """
    fileno = getattr(sys.stdout, "fileno", None)
    if fileno is None:
        return
    try:
        null_fd = os.open(os.devnull, os.O_WRONLY)
    except OSError:
        return
    try:
        os.dup2(null_fd, fileno())
    except (OSError, ValueError):
        # Descritor já fechado ou sem suporte a ``fileno``: nada a redirecionar.
        pass
    finally:
        os.close(null_fd)


class _StdoutHandler(logging.StreamHandler):
    """Handler de ``stdout`` que não transforma um pipe fechado em traceback.

    ``stderr`` é reservado à linha única de diagnóstico fatal e às mensagens de uso do
    ``argparse``, então um leitor que fecha o pipe não pode converter uma linha de log
    perdida em ``--- Logging error ---`` com traceback. Por isso a escrita é feita
    aqui, dentro do ``try``: ``BrokenPipeError`` e o ``ValueError`` de um stream já
    fechado ou descartado são abandonados em silêncio, enquanto todo o resto continua
    chegando a :meth:`logging.StreamHandler.handleError`, para não esconder um defeito
    de verdade do formatador. ``RecursionError`` é a exceção que o próprio ``logging``
    trata como fatal e volta a subir.
    """

    def emit(self, record: logging.LogRecord) -> None:
        """Formata ``record`` e o escreve em ``self.stream``, tolerando o pipe fechado."""
        try:
            message = self.format(record)
            self.stream.write(message + self.terminator)
            self.flush()
        except (BrokenPipeError, ValueError):
            # O leitor de stdout fechou o pipe ou o stream já foi fechado: a linha de
            # log se perde, e é o suficiente — nada de diagnóstico em stderr.
            pass
        except RecursionError:
            raise
        except Exception:
            self.handleError(record)


class _OperatorFormatter(logging.Formatter):
    """Formatter cujo ``asctime`` sai em ISO-8601 no fuso do operador e em uma linha.

    O ``logging.Formatter`` padrão converte o instante com ``time.localtime`` e
    formata o ``struct_time`` resultante, que não carrega deslocamento de fuso:
    ``%z`` sairia vazio. Aqui ``converter`` devolve um ``datetime`` já ciente do
    fuso efetivo, de modo que ``%z`` apareça na linha.

    A mensagem sai com os caracteres de controle escapados, para que toda linha de log
    tenha a forma fixa do contrato mesmo quando um valor interpolado carrega ``\\n`` ou
    um escape ANSI.
    """

    def __init__(self, timezone: ZoneInfo | None) -> None:
        super().__init__(LOG_FORMAT, LOG_DATEFMT)
        self.converter = _timezone_converter(timezone)

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        """Data e hora do registro no fuso efetivo, com o deslocamento."""
        return self.converter(record.created).strftime(datefmt or LOG_DATEFMT)

    def formatMessage(self, record: logging.LogRecord) -> str:
        """A mensagem do registro com os caracteres de controle escapados.

        O escape acontece antes de o formato ser aplicado, então vale para qualquer valor
        que o produto interpole em um registro, e não só para o ticker. O ``exc_info``
        continua sendo anexado pelo formatador base, depois da mensagem, e é isso que
        mantém o traceback da falha inesperada em várias linhas, como o README documenta.

        Args:
            record: registro em formatação.

        Returns:
            A linha formatada, sem caractere de controle.
        """
        escapado = copy.copy(record)
        escapado.message = _escape_control_characters(record.getMessage())
        return super().formatMessage(escapado)


def _timezone_converter(timezone: ZoneInfo | None) -> Callable[[float], datetime]:
    """Conversor de instante do formatador: ``float`` → ``datetime`` ciente do fuso.

    O ``datetime`` devolvido sempre carrega o deslocamento, então ``%z`` nunca sai
    vazio: com ``ZoneInfo`` vale o fuso do operador e, sem ele — o caso das linhas
    gravadas antes de a configuração ser lida —, o deslocamento local do host.
    """
    if timezone is None:
        return lambda timestamp: datetime.fromtimestamp(timestamp).astimezone()
    return lambda timestamp: datetime.fromtimestamp(timestamp, timezone)


def _fatal(message: str) -> None:
    """Registra a falha fatal uma única vez: ``ERROR`` no log e em ``stderr``.

    A linha de ``stderr`` passa pelo mesmo escape do formatador: um valor hostil vindo da
    configuração não pode dividir a linha única que o README promete ali.

    Args:
        message: linha única de diagnóstico, já sem qualquer valor de credencial.
    """
    _LOGGER.error("%s", message)
    print(_escape_control_characters(message), file=sys.stderr)


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

    Definida significa um valor que ``finmail_config.load_credentials`` aceita:
    uma variável ausente, vazia ou composta apenas de espaços conta como não
    definida, do mesmo modo que ele a recusa. Apenas a presença é consultada,
    porque ``check-config`` não exige credencial.

    Args:
        variable: nome da variável de ambiente, como ``SMTP_PASSWORD``.

    Returns:
        ``definido`` ou ``não definido``.
    """
    return "definido" if os.environ.get(variable, "").strip() else "não definido"
