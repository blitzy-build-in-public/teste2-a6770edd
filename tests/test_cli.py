"""Contrato de ``finmail/cli.py``: comandos, códigos de saída e disciplina de streams.

Cobre o mapa de códigos de saída da AAP 0.6.2 (``0`` sucesso, ``1`` defeito
inesperado, ``2`` nenhum ativo com dados, ``3`` configuração/credencial/uso,
``4`` falha de SMTP), a superfície de argumentos da AAP 0.6.1 (``run``,
``run --dry-run``, ``check-config`` e a opção global ``--config`` com precedência
da ocorrência mais à direita), o bloco impresso por ``--dry-run``, o fuso do
assunto, o resumo de ``check-config`` e as duas provas de isolamento: nenhum
segredo em ``stdout``/``stderr`` (critério 6) e nenhuma importação de
``yfinance``/``mailer`` fora do ramo que os usa (AAP 0.5.1, critério 5).

Os adaptadores são substituídos por atributo de módulo — ``finmail.quotes.fetch``,
``finmail.report.build``, ``finmail.mailer.send`` e
``finmail.config.load_credentials`` —, que é exatamente como o ``cli`` os
consulta em tempo de chamada. Nenhuma costura de produção é acrescentada ao
produto, e nenhum teste aqui toca a rede: as fixtures autouse do harness bloqueiam
qualquer destino fora de loopback e removem as variáveis de credencial.

O teste de ``cli.main`` que exercita os streams usa ``capfd``, e não ``capsys``:
o ``cli`` instala o próprio handler de log em ``sys.stdout`` e só a captura por
descritor de arquivo lê com confiança o que ele escreveu.
"""

import logging
import os
import re
import sys
import tomllib
import warnings
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from email.utils import getaddresses
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

import finmail.cli as cli
import finmail.config as config_module
import finmail.mailer as mailer_module
import finmail.quotes as quotes_module
import finmail.report as report_module
from finmail.config import Config, Credentials, SmtpConfig
from finmail.quotes import Quote, QuoteError
from finmail.report import Report

# Remetentes distintos dos dois arquivos de configuração: é a diferença que revela
# qual arquivo o ``--config`` escolheu, já que ``check-config`` imprime o remetente.
SENDER_A = "remetente-a@exemplo.com"
SENDER_B = "remetente-b@exemplo.com"

# Configuração usada nos casos de falha parcial: o terceiro ativo é o que o dublê
# de ``yfinance`` não serve, então ele é o único que falha.
TICKERS_COM_FALHA = ["AAPL", "PETR4.SA", "BTC-USD"]
MOTIVO_DA_FALHA = "falha de rede após 3 tentativas"

# Linha de log no formato único de AAP 0.6.2: data ISO-8601, nível, nome do logger
# e mensagem. Toda linha carrega o deslocamento de fuso, inclusive a da falha de
# configuração, gravada antes de a configuração ser lida.
LOG_LINE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{4} "
    r"(DEBUG|INFO|WARNING|ERROR) finmail: "
)
# Deslocamento de fuso explícito, exigido de toda linha de log (AAP 0.6.2).
ZONE_OFFSET = re.compile(r"T\d{2}:\d{2}:\d{2}[+-]\d{4} ")


@dataclass(frozen=True)
class SentMail:
    """Uma chamada registrada de ``finmail.mailer.send``, com todos os argumentos."""

    message: EmailMessage
    smtp: SmtpConfig
    credentials: Credentials


def fake_quote(ticker: str) -> Quote:
    """Cotação completa e fixa para ``ticker``, usada no lugar do Yahoo Finance.

    Os valores não importam para este arquivo — os corpos do relatório são provados
    em ``tests/test_report.py``; aqui a cotação só precisa ser um ``Quote`` válido
    para que ``report.build`` renderize uma linha.
    """
    return Quote(
        ticker=ticker,
        name=f"Ativo {ticker}",
        currency="USD",
        last_trading_date=date(2026, 11, 12),
        close=123.45,
        change=1.23,
        change_pct=1.01,
        volume=1_234_567,
        week52_low=100.0,
        week52_high=200.0,
    )


def patch_quotes(
    monkeypatch: pytest.MonkeyPatch,
    *,
    good: int,
    bad: Sequence[tuple[str, str]] = (),
) -> list[str]:
    """Substitui ``finmail.quotes.fetch`` e devolve a ordem das consultas.

    Os primeiros ``good`` ativos consultados que não estejam em ``bad`` devolvem um
    ``Quote`` completo; os ativos de ``bad`` levantam ``QuoteError`` com o motivo
    dado, e qualquer excedente falha como se o Yahoo não tivesse dados. A lista
    devolvida registra a ordem das chamadas, o que prova a passagem sequencial e
    única por cada ativo da configuração.
    """
    failures = dict(bad)
    remaining = good
    calls: list[str] = []

    def fetch(ticker: str) -> Quote:
        nonlocal remaining
        calls.append(ticker)
        if ticker in failures:
            raise QuoteError(ticker, failures[ticker])
        if remaining <= 0:
            raise QuoteError(ticker, MOTIVO_DA_FALHA)
        remaining -= 1
        return fake_quote(ticker)

    monkeypatch.setattr(quotes_module, "fetch", fetch)
    return calls


def patch_mailer(monkeypatch: pytest.MonkeyPatch) -> list[SentMail]:
    """Substitui ``finmail.mailer.send`` por um registrador e devolve a lista.

    A mensagem continua sendo montada pelo ``build_message`` real, de modo que a
    asserção sobre o cabeçalho ``To`` verifica o que o operador receberia. Nenhum
    socket é aberto: o registrador só guarda os argumentos.
    """
    sent: list[SentMail] = []

    def send(
        message: EmailMessage,
        smtp: SmtpConfig,
        credentials: Credentials,
    ) -> None:
        sent.append(SentMail(message=message, smtp=smtp, credentials=credentials))

    monkeypatch.setattr(mailer_module, "send", send)
    return sent


def patch_build(monkeypatch: pytest.MonkeyPatch) -> list[datetime]:
    """Substitui ``finmail.report.build`` e devolve os instantes que ele recebeu.

    É assim que o fuso do assunto é verificado no ponto exato em que o ``cli``
    entrega a data ao relatório, sem depender do relógio do processo de teste.
    """
    moments: list[datetime] = []

    def build(entries: Sequence[Any], now: datetime) -> Report:
        moments.append(now)
        subject = f"Resumo de mercado – {now:%Y-%m-%d}"
        return Report(subject=subject, text=f"{subject}\n", html="<html></html>")

    monkeypatch.setattr(report_module, "build", build)
    return moments


def capture(capfd: pytest.CaptureFixture[str]) -> tuple[str, str]:
    """Devolve ``(stdout, stderr)`` já em texto, lidos por descritor de arquivo."""
    captured = capfd.readouterr()
    return captured.out, captured.err


def log_lines(text: str) -> list[str]:
    """As linhas de ``text`` que seguem o formato único de log da AAP 0.6.2."""
    return [line for line in text.splitlines() if LOG_LINE.match(line)]


def lines_with_level(text: str, level: str) -> list[str]:
    """As linhas de log de ``text`` gravadas no nível indicado."""
    return [line for line in log_lines(text) if f" {level} " in line]


def assert_absent(streams: Mapping[str, str], secret: str) -> None:
    """Falha se ``secret`` aparecer em algum stream, inclusive dentro de um log.

    A mensagem de falha nomeia apenas o stream, nunca o valor procurado.
    """
    for label, text in streams.items():
        assert secret not in text, f"o valor sensível aparece em {label}"
        for line in log_lines(text):
            assert secret not in line, f"o valor sensível aparece em uma linha de log de {label}"


def subprocess_field(output: str, name: str) -> str:
    """Extrai o valor de uma linha ``<name> <value>`` impressa por um subprocesso."""
    for line in output.splitlines():
        if line.startswith(f"{name} "):
            return line.split(" ", 1)[1].strip()
    raise AssertionError(f"o subprocesso não imprimiu {name}:\n{output}")


def config_tickers(path: Path) -> list[str]:
    """Os ativos da configuração, na ordem do arquivo, lidos pelo próprio produto."""
    return list(config_module.load_config(path).tickers)


@pytest.fixture
def two_configs(config_writer: Any, tmp_path: Path) -> tuple[Path, Path]:
    """Dois arquivos válidos que diferem apenas no remetente."""
    first = config_writer(path=tmp_path / "a.yaml", sender=SENDER_A)
    second = config_writer(path=tmp_path / "b.yaml", sender=SENDER_B)
    return first, second


@pytest.fixture
def credentials_env(
    monkeypatch: pytest.MonkeyPatch,
    sentinel_username: str,
    sentinel_password: str,
    config_writer: Any,
) -> Callable[..., Path]:
    """Define as duas credenciais do ambiente e devolve o escritor de configuração.

    A credencial é precondição de ``run`` — sem ela o comando sai com ``3`` antes de
    consultar qualquer ativo —, não o assunto do teste que a usa: só os casos que
    provam a *ausência* do valor nas saídas precisam declará-la. O escritor é
    devolvido inteiro, então um teste que precise de outra configuração continua
    passando os overrides que quiser.
    """
    monkeypatch.setenv("SMTP_USERNAME", sentinel_username)
    monkeypatch.setenv("SMTP_PASSWORD", sentinel_password)
    return config_writer


# Mapa de códigos de saída e superfície de argumentos (AAP 0.6.1, AAP 0.6.2).


def test_constantes_de_saida_seguem_o_mapa_da_aap() -> None:
    """Os cinco códigos têm exatamente o significado que o ``cron`` distingue."""
    assert cli.EXIT_OK == 0
    assert cli.EXIT_UNEXPECTED == 1
    assert cli.EXIT_ALL_QUOTES_FAILED == 2
    assert cli.EXIT_CONFIG == 3
    assert cli.EXIT_SMTP == 4


def test_help_sai_com_0_e_lista_os_dois_comandos(
    run_finmail: Any,
) -> None:
    """``--help`` continua sendo o caminho do ``argparse`` que sai com ``0``."""
    result = run_finmail("--help")

    assert result.returncode == cli.EXIT_OK
    assert "run" in result.stdout
    assert "check-config" in result.stdout
    assert result.stderr == ""


@pytest.mark.parametrize(
    ("arguments", "offender"),
    [
        pytest.param(("bogus",), "bogus", id="subcomando-desconhecido"),
        pytest.param(("run", "--config"), "--config", id="valor-faltante"),
        pytest.param(("run", "--nao-existe"), "--nao-existe", id="opcao-desconhecida"),
    ],
)
def test_uso_invalido_sai_com_3(
    run_finmail: Any,
    arguments: tuple[str, ...],
    offender: str,
) -> None:
    """Erro de uso sai com ``3``, nunca com o ``2`` que o ``argparse`` usaria.

    O ``2`` é exclusivo de "todos os ativos falharam"; se o erro de uso também
    saísse com ``2``, o ``cron`` não distinguiria as duas situações.
    """
    result = run_finmail(*arguments)

    assert result.returncode == cli.EXIT_CONFIG
    assert offender in result.stderr
    assert "erro de uso" in result.stderr


def test_erro_de_uso_em_processo_encerra_com_3(
    capfd: pytest.CaptureFixture[str],
) -> None:
    """O mesmo desfecho de uso, observado no processo que chama ``main``."""
    try:
        rc = cli.main(["bogus"])
    except SystemExit as exc:
        rc = exc.code

    assert rc == cli.EXIT_CONFIG
    out, err = capture(capfd)
    assert "bogus" in err
    assert "erro de uso" in err
    assert out == ""


def test_config_aceita_depois_do_subcomando(
    two_configs: tuple[Path, Path],
    capfd: pytest.CaptureFixture[str],
) -> None:
    """``check-config --config CAMINHO`` usa o arquivo indicado."""
    _, second = two_configs

    assert cli.main(["check-config", "--config", str(second)]) == cli.EXIT_OK

    out, err = capture(capfd)
    assert f"sender: {SENDER_B}" in out
    assert SENDER_A not in out
    assert err == ""


def test_config_aceita_antes_do_subcomando(
    two_configs: tuple[Path, Path],
    capfd: pytest.CaptureFixture[str],
) -> None:
    """``--config CAMINHO check-config`` também vale: a opção é global."""
    first, _ = two_configs

    assert cli.main(["--config", str(first), "check-config"]) == cli.EXIT_OK

    out, err = capture(capfd)
    assert f"sender: {SENDER_A}" in out
    assert SENDER_B not in out
    assert err == ""


def test_config_mais_a_direita_vence(
    two_configs: tuple[Path, Path],
    capfd: pytest.CaptureFixture[str],
) -> None:
    """Repetida antes e depois do subcomando, a ocorrência mais à direita decide."""
    first, second = two_configs

    arguments = ["--config", str(first), "check-config", "--config", str(second)]

    assert cli.main(arguments) == cli.EXIT_OK

    out, err = capture(capfd)
    assert f"sender: {SENDER_B}" in out
    assert SENDER_A not in out
    assert err == ""


def test_config_padrao_e_o_config_yaml_do_diretorio_corrente(
    config_writer: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
) -> None:
    """Sem ``--config``, o caminho é ``./config.yaml`` resolvido no diretório atual.

    O padrão é verificado em ``finmail.config``, onde ``DEFAULT_CONFIG_PATH`` é
    definido; o ``cli`` o consome por um alias interno e não o reexporta.
    """
    assert Path(config_module.DEFAULT_CONFIG_PATH).name == "config.yaml"

    monkeypatch.chdir(tmp_path)
    config_writer(sender=SENDER_A)

    assert cli.main(["check-config"]) == cli.EXIT_OK

    out, err = capture(capfd)
    assert f"sender: {SENDER_A}" in out
    assert err == ""


# ``run`` em sucesso e em falha parcial (AAP 0.8, linhas 1 e 2).


def test_run_envia_uma_unica_mensagem_com_um_fetch_por_ativo(
    monkeypatch: pytest.MonkeyPatch,
    credentials_env: Any,
    sentinel_username: str,
    sentinel_password: str,
    capfd: pytest.CaptureFixture[str],
) -> None:
    """Sucesso: uma consulta por ativo, na ordem da configuração, e um único envio."""
    path = credentials_env()
    configuration = config_module.load_config(path)
    order = patch_quotes(monkeypatch, good=len(configuration.tickers))
    sent = patch_mailer(monkeypatch)

    assert cli.main(["run", "--config", str(path)]) == cli.EXIT_OK

    assert order == list(configuration.tickers)
    assert len(sent) == 1
    message = sent[0].message
    delivered = [address for _, address in getaddresses([str(message["To"])])]
    assert delivered == list(configuration.recipients)
    assert message["From"] == configuration.sender
    assert sent[0].smtp == configuration.smtp
    # As credenciais vêm do ambiente e chegam a ``send`` sem passar por outra camada.
    assert sent[0].credentials.username == sentinel_username
    assert sent[0].credentials.password == sentinel_password

    out, err = capture(capfd)
    assert err == ""
    # No sucesso, tudo o que vai para ``stdout`` é linha de log, com o fuso do
    # operador no campo de data.
    assert len(log_lines(out)) == len(out.splitlines())
    assert lines_with_level(out, "INFO")
    assert all(ZONE_OFFSET.search(line) for line in log_lines(out))


def test_run_parcial_mantem_a_mensagem_e_sai_com_0(
    monkeypatch: pytest.MonkeyPatch,
    credentials_env: Any,
    capfd: pytest.CaptureFixture[str],
) -> None:
    """Um ativo sem dados não impede o envio: a execução continua e sai com ``0``."""
    path = credentials_env(tickers=TICKERS_COM_FALHA)
    order = patch_quotes(monkeypatch, good=2, bad=[("BTC-USD", MOTIVO_DA_FALHA)])
    sent = patch_mailer(monkeypatch)

    assert cli.main(["run", "--config", str(path)]) == cli.EXIT_OK

    assert order == TICKERS_COM_FALHA
    assert len(sent) == 1
    out, err = capture(capfd)
    assert err == ""
    warnings = lines_with_level(out, "WARNING")
    assert len(warnings) == 1
    assert "BTC-USD" in warnings[0]
    assert MOTIVO_DA_FALHA in warnings[0]


# Demais códigos de saída: ``2``, ``3``, ``4`` e ``1`` (AAP 0.6.2).


def test_todos_os_ativos_falhando_sai_com_2_sem_montar_mensagem(
    monkeypatch: pytest.MonkeyPatch,
    credentials_env: Any,
    capfd: pytest.CaptureFixture[str],
) -> None:
    """Nenhum ativo com dados: nenhuma mensagem é montada nem enviada, e sai ``2``."""
    path = credentials_env()
    configured = config_tickers(path)
    order = patch_quotes(
        monkeypatch,
        good=0,
        bad=[(ticker, MOTIVO_DA_FALHA) for ticker in configured],
    )
    sent = patch_mailer(monkeypatch)

    assert cli.main(["run", "--config", str(path)]) == cli.EXIT_ALL_QUOTES_FAILED

    assert order == configured
    assert sent == []
    out, err = capture(capfd)
    assert len(lines_with_level(out, "WARNING")) == len(configured)
    errors = lines_with_level(out, "ERROR")
    assert len(errors) == 1
    assert str(len(configured)) in errors[0]
    # A falha fatal é uma linha única, no log e em ``stderr``.
    assert len(err.splitlines()) == 1


def test_config_ausente_sai_com_3_e_nomeia_o_arquivo(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
) -> None:
    """Configuração inexistente: ``3`` e o caminho na mensagem, sem traceback."""
    missing = tmp_path / "nao-existe.yaml"
    order = patch_quotes(monkeypatch, good=0)

    assert cli.main(["run", "--config", str(missing)]) == cli.EXIT_CONFIG

    assert order == []
    out, err = capture(capfd)
    assert missing.name in err
    assert missing.name in out
    assert lines_with_level(out, "ERROR")
    assert "Traceback" not in err


def test_config_invalida_sai_com_3_citando_o_campo(
    config_writer: Any,
    capfd: pytest.CaptureFixture[str],
) -> None:
    """Lista de destinatários vazia: ``3`` com o nome do campo, sem rede."""
    path = config_writer(recipients=[])

    assert cli.main(["check-config", "--config", str(path)]) == cli.EXIT_CONFIG

    out, err = capture(capfd)
    assert "recipients" in err
    assert "recipients" in out
    assert "Traceback" not in err


@pytest.mark.parametrize("missing", ["SMTP_USERNAME", "SMTP_PASSWORD"])
def test_credencial_ausente_sai_com_3_nomeando_a_variavel(
    monkeypatch: pytest.MonkeyPatch,
    config_writer: Any,
    sentinel_username: str,
    sentinel_password: str,
    missing: str,
    capfd: pytest.CaptureFixture[str],
) -> None:
    """Sem uma das variáveis, ``run`` para em ``3`` antes de consultar ou enviar."""
    defined = {"SMTP_USERNAME": sentinel_username, "SMTP_PASSWORD": sentinel_password}
    for name, value in defined.items():
        if name != missing:
            monkeypatch.setenv(name, value)
    path = config_writer()
    order = patch_quotes(monkeypatch, good=3)
    sent = patch_mailer(monkeypatch)

    assert cli.main(["run", "--config", str(path)]) == cli.EXIT_CONFIG

    out, err = capture(capfd)
    assert missing in err
    assert missing in out
    # As credenciais são lidas antes do primeiro fetch e de qualquer envio.
    assert order == []
    assert sent == []
    # A mensagem cita a variável ausente e nunca o valor da que está definida.
    present = defined["SMTP_USERNAME" if missing == "SMTP_PASSWORD" else "SMTP_PASSWORD"]
    assert_absent({"stdout": out, "stderr": err}, present)


def test_credenciais_ausentes_nao_impedem_dry_run_nem_check_config(
    monkeypatch: pytest.MonkeyPatch,
    config_writer: Any,
    capfd: pytest.CaptureFixture[str],
) -> None:
    """``--dry-run`` e ``check-config`` não exigem credencial (AAP 0.7.1)."""
    path = config_writer()
    patch_quotes(monkeypatch, good=3)
    sent = patch_mailer(monkeypatch)

    assert cli.main(["run", "--dry-run", "--config", str(path)]) == cli.EXIT_OK
    out, err = capture(capfd)
    assert err == ""
    assert "Assunto: " in out

    assert cli.main(["check-config", "--config", str(path)]) == cli.EXIT_OK
    summary, summary_err = capture(capfd)
    assert summary_err == ""
    assert "SMTP_USERNAME: não definido" in summary
    assert "SMTP_PASSWORD: não definido" in summary
    assert sent == []


def test_nem_dry_run_nem_check_config_chamam_load_credentials(
    monkeypatch: pytest.MonkeyPatch,
    config_writer: Any,
    capfd: pytest.CaptureFixture[str],
) -> None:
    """Nenhum dos dois caminhos toca ``load_credentials``, nem para ler a senha."""

    def forbidden(*args: Any, **kwargs: Any) -> Credentials:
        raise AssertionError("load_credentials não pode ser chamado aqui")

    monkeypatch.setattr(config_module, "load_credentials", forbidden)
    path = config_writer()
    patch_quotes(monkeypatch, good=3)
    patch_mailer(monkeypatch)

    assert cli.main(["run", "--dry-run", "--config", str(path)]) == cli.EXIT_OK
    assert cli.main(["check-config", "--config", str(path)]) == cli.EXIT_OK

    out, err = capture(capfd)
    assert err == ""
    assert "Assunto: " in out


def test_falha_de_smtp_sai_com_4(
    monkeypatch: pytest.MonkeyPatch,
    credentials_env: Any,
    sentinel_password: str,
    capfd: pytest.CaptureFixture[str],
) -> None:
    """Depois das tentativas de envio, a execução termina em ``4`` e sem senha."""
    path = credentials_env()
    patch_quotes(monkeypatch, good=3)

    def send(*args: Any, **kwargs: Any) -> None:
        raise mailer_module.MailError(
            "smtp.exemplo.com",
            587,
            "autenticação recusada (código 535)",
        )

    monkeypatch.setattr(mailer_module, "send", send)

    assert cli.main(["run", "--config", str(path)]) == cli.EXIT_SMTP

    out, err = capture(capfd)
    assert len(err.splitlines()) == 1
    assert "smtp.exemplo.com:587" in err
    assert "código 535" in err
    assert lines_with_level(out, "ERROR")
    assert_absent({"stdout": out, "stderr": err}, sentinel_password)


def test_defeito_inesperado_sai_com_1_e_um_diagnostico_unico_em_stderr(
    monkeypatch: pytest.MonkeyPatch,
    config_writer: Any,
    capfd: pytest.CaptureFixture[str],
) -> None:
    """Uma exceção não classificada vira ``1``, com uma linha de diagnóstico.

    O traceback pertence ao registro ``ERROR`` do log em ``stdout`` (AAP 0.6.2:
    ``stdout`` recebe todas as linhas de log), então o que se exige de ``stderr``
    é exatamente uma linha, sem traceback. A mensagem da exceção carrega uma quebra
    de linha de propósito: nem um texto de fora do produto divide a linha única.
    """
    path = config_writer()

    def explode(ticker: str) -> Quote:
        raise RuntimeError("falha inesperada\ncom quebra de linha")

    monkeypatch.setattr(quotes_module, "fetch", explode)
    patch_mailer(monkeypatch)

    assert cli.main(["run", "--dry-run", "--config", str(path)]) == cli.EXIT_UNEXPECTED

    out, err = capture(capfd)
    assert len(err.splitlines()) == 1
    assert "falha inesperada" in err
    assert "com quebra de linha" in err
    assert "\\x0a" in err
    assert "Traceback" not in err
    assert lines_with_level(out, "ERROR")


def test_defeito_no_prologo_do_dispatch_sai_com_1_e_um_diagnostico_unico_em_stderr(
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    """Um defeito anterior ao handler do comando também vira ``1``, com log em ``stdout``.

    O classificador final fica em ``main``, e não em ``_dispatch``, justamente para
    cobrir o prólogo: o parser é montado antes de qualquer handler, e uma falha ali
    escaparia ao interpretador, que imprimiria o traceback em ``stderr`` e deixaria
    ``stdout`` sem o ``ERROR``. A falha é levantada antes de ``_configure_logging``
    rodar, então o caso também prova que a captura configura o log antes de registrar a
    exceção — sem handler, o ``lastResort`` escreveria o traceback no ``stderr``.
    """

    def explode() -> Any:
        raise RuntimeError("falha na montagem do parser")

    monkeypatch.setattr(cli, "_build_parser", explode)

    assert cli.main(["check-config"]) == cli.EXIT_UNEXPECTED

    out, err = capture(capfd)
    assert len(err.splitlines()) == 1
    assert "falha inesperada" in err
    assert "falha na montagem do parser" in err
    assert "Traceback" not in err
    assert lines_with_level(out, "ERROR")
    assert "Traceback" in out


def test_defeito_na_configuracao_do_log_sai_com_1_e_um_diagnostico_unico_em_stderr(
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    """Sem log configurável, ``stderr`` ainda recebe exatamente uma linha de diagnóstico.

    O mesmo defeito que derruba a execução pode impedir ``_configure_logging`` de rodar; e
    sem handler o ``lastResort`` escreveria o traceback em ``stderr``, dividindo a linha
    única. A captura final não pode falhar por isso: ela configura o log quando consegue,
    registra a exceção e imprime a linha de ``stderr`` de qualquer modo.
    """

    def explode(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("falha ao configurar o log")

    monkeypatch.setattr(cli, "_configure_logging", explode)
    logging.getLogger(cli.LOGGER_NAME).handlers.clear()

    assert cli.main(["check-config"]) == cli.EXIT_UNEXPECTED

    out, err = capture(capfd)
    assert len(err.splitlines()) == 1
    assert "falha inesperada" in err
    assert "falha ao configurar o log" in err
    assert "Traceback" not in err
    assert "Traceback" not in out


# ``cli._configure_logging`` mexe em estado global do processo — o handler do logger do
# produto (apontado para o ``sys.stdout`` daquele instante), o nível e a propagação dele,
# os loggers de ``cli.QUIET_LOGGERS``, um handler nulo na raiz e a lista de filtros de
# aviso — e não desfaz nada. A fixture autouse abaixo devolve esse estado ao fim de cada
# teste do módulo: sem ela o teste seguinte herda um handler apontado para um stream de
# captura já encerrado, e ``logging.handleError`` engole o erro de escrita.


@dataclass(frozen=True)
class LoggerState:
    """Estado de um logger: nível, handlers, propagação e desativação."""

    logger: logging.Logger
    level: int
    handlers: list[logging.Handler]
    propagate: bool
    disabled: bool


@dataclass(frozen=True)
class LoggingState:
    """Retrato do estado global que ``cli._configure_logging`` altera."""

    loggers: tuple[LoggerState, ...]
    warning_filters: list[Any]


def snapshot_logging_state() -> LoggingState:
    """Guarda o estado de logging e de filtros de aviso que o produto altera.

    Os alvos são o logger do produto, os loggers de ``cli.QUIET_LOGGERS`` — iterados a
    partir da constante do produto, para que o retrato acompanhe a lista se ela mudar — e
    o logger raiz, mais a lista de ``warnings.filters``. A lista de handlers é copiada, de
    modo que o retrato não acompanhe alterações posteriores no logger.
    """
    loggers = [logging.getLogger(cli.LOGGER_NAME)]
    loggers.extend(logging.getLogger(name) for name in cli.QUIET_LOGGERS)
    loggers.append(logging.getLogger())
    return LoggingState(
        loggers=tuple(
            LoggerState(
                logger=logger,
                level=logger.level,
                handlers=list(logger.handlers),
                propagate=logger.propagate,
                disabled=logger.disabled,
            )
            for logger in loggers
        ),
        warning_filters=list(warnings.filters),
    )


def restore_logging_state(state: LoggingState) -> None:
    """Devolve ao processo exatamente o estado guardado por ``state``.

    A lista de handlers e a de filtros de aviso são reescritas no lugar, e não trocadas:
    os objetos de handler que outras partes do processo guardam continuam válidos,
    inclusive os do plugin de logging do pytest, de modo que ``caplog`` e a captura da
    suíte seguem intactos. Restaurar duas vezes com o mesmo retrato reescreve o mesmo
    estado, então a função é segura para os testes que chamam ``_configure_logging`` mais
    de uma vez.
    """
    for saved in state.loggers:
        saved.logger.setLevel(saved.level)
        saved.logger.handlers[:] = saved.handlers
        saved.logger.propagate = saved.propagate
        saved.logger.disabled = saved.disabled
    warnings.filters[:] = state.warning_filters


@pytest.fixture(autouse=True)
def estado_de_logging_restaurado() -> Iterator[None]:
    """Devolve o estado global de logging ao fim de cada teste deste módulo.

    É autouse porque qualquer teste do módulo chega a ``cli._configure_logging``, seja
    por ``cli.main``, seja por chamada direta: o retrato é tirado antes do teste e
    devolvido no teardown, e nenhum módulo herda handler, nível, propagação ou filtro
    deixado por outro.
    """
    state = snapshot_logging_state()
    yield
    restore_logging_state(state)


def entradas_do_produto() -> list[tuple[Any, ...]]:
    """Devolve as entradas de ``warnings.filters`` cujo namespace é um dos do produto.

    A coleta é pelo padrão de módulo, e não pela posição: o que identifica um filtro do
    produto é o namespace que ele cala, de modo que o caso não depende da ordem em que
    ``warnings.filterwarnings`` inseriu as entradas.
    """
    return [
        entrada
        for entrada in warnings.filters
        if isinstance(getattr(entrada[3], "pattern", None), str)
        and entrada[3].pattern in cli._WARNING_MODULES
    ]


def pares_benignos() -> set[tuple[type[Warning], str]]:
    """Devolve cada par (categoria benigna, namespace) que o produto precisa instalar."""
    return {
        (categoria, modulo)
        for modulo in cli._WARNING_MODULES
        for categoria in cli._BENIGN_WARNING_CATEGORIES
    }


def aviso_do_urllib3(nome: str) -> type[Warning] | None:
    """Devolve a classe de aviso de ``urllib3.exceptions`` quando já está carregada.

    O processo da suíte pode ou não ter o ``urllib3`` em ``sys.modules``, e o caso não
    pode depender disso: a consulta é a mesma que o produto faz, e a prova funcional da
    classe real fica no subprocesso de ``TLS_WARNING_PROBE``.
    """
    modulo = sys.modules.get("urllib3.exceptions")
    if modulo is None:
        return None
    classe = getattr(modulo, nome, None)
    if isinstance(classe, type) and issubclass(classe, Warning):
        return classe
    return None


def test_os_helpers_de_retrato_devolvem_o_estado_do_processo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """O retrato desfeito devolve nível, handlers, propagação e filtros de cada logger.

    O caso não depende da ordem: ele mesmo tira o retrato, chama a função privada que
    altera o estado global e compara o antes com o depois. A comparação é por identidade
    de handler e de lista, que é o que prova que a restauração escreveu no lugar em vez
    de trocar os objetos que o resto do processo guarda. As opções de aviso do
    interpretador são fixadas porque ``_configure_warnings`` não instala filtro nenhum
    quando o ambiente pede avisos como erro.
    """
    # ``sys.warnoptions`` alimenta a decisão de instalar os filtros do produto, então o
    # caso fixa as opções em vez de herdar as do harness.
    monkeypatch.setattr(sys, "warnoptions", [])
    alvos = [logging.getLogger(cli.LOGGER_NAME)]
    alvos.extend(logging.getLogger(name) for name in cli.QUIET_LOGGERS)
    alvos.append(logging.getLogger())

    def retratos() -> dict[str, tuple[int, list[int], bool, bool]]:
        return {
            logger.name: (
                logger.level,
                [id(handler) for handler in logger.handlers],
                logger.propagate,
                logger.disabled,
            )
            for logger in alvos
        }

    listas = {logger.name: id(logger.handlers) for logger in alvos}
    filtros = list(warnings.filters)
    antes = retratos()

    primeiro = snapshot_logging_state()
    segundo = snapshot_logging_state()
    cli._configure_logging()
    cli._configure_logging()

    # A função privada mexeu no estado global de cada logger: sem esta metade do caso,
    # uma restauração vazia passaria como se tivesse devolvido alguma coisa.
    depois = retratos()
    for nome in (cli.LOGGER_NAME, *cli.QUIET_LOGGERS):
        assert depois[nome] != antes[nome]
    assert logging.getLogger(cli.LOGGER_NAME).level == logging.INFO
    assert logging.getLogger(cli.LOGGER_NAME).propagate is False
    for nome in cli.QUIET_LOGGERS:
        assert logging.getLogger(nome).level == logging.ERROR
        assert logging.getLogger(nome).propagate is False
        assert logging.getLogger(nome).handlers
    # Os filtros de aviso do produto: uma categoria benigna por namespace, no início da
    # lista, e nenhuma delas é ``Warning`` inteira.
    proprios = entradas_do_produto()
    assert warnings.filters[: len(proprios)] == proprios
    assert {entrada[3].pattern for entrada in proprios} == set(cli._WARNING_MODULES)
    assert {
        (entrada[2], entrada[3].pattern)
        for entrada in proprios
        if entrada[2] in cli._BENIGN_WARNING_CATEGORIES
    } == pares_benignos()
    assert not any(entrada[2] is Warning for entrada in proprios)

    # Duas restaurações seguidas: a segunda reescreve exatamente o mesmo estado.
    restore_logging_state(segundo)
    restore_logging_state(primeiro)

    assert retratos() == antes
    assert {logger.name: id(logger.handlers) for logger in alvos} == listas
    assert warnings.filters == filtros


# Disciplina de streams: um aviso de biblioteca não passa por logger —
# ``warnings.showwarning`` escreve direto em ``stderr``, que é reservado à linha única de
# diagnóstico fatal e às mensagens de uso (AAP 0.6.2, README).


def test_aviso_de_depreciacao_do_yfinance_nao_chega_ao_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """O produto cala as categorias benignas dos namespaces de biblioteca, e só elas.

    A prova é estrutural, sobre ``warnings.filters``, e não sobre o ``stderr``
    capturado: o pytest envolve cada teste em um contexto de avisos próprio, no qual a
    contagem de avisos depende da ordem e dos plugins — o caminho funcional é provado em
    subprocesso, por
    ``test_aviso_de_biblioteca_fora_do_namespace_do_yfinance_nao_chega_ao_stderr`` e por
    ``test_aviso_de_seguranca_do_urllib3_chega_ao_stderr``. A lista é copiada e o
    ``monkeypatch`` a restaura, e as opções de aviso do interpretador são fixadas, então
    o teste não deixa estado global nem depende da ordem dos demais.
    """
    monkeypatch.setattr(warnings, "filters", list(warnings.filters))
    monkeypatch.setattr(sys, "warnoptions", [])
    # O filtro que a biblioteca de cotações instala ao ser importada, reposto aqui porque
    # o contexto de avisos do pytest descarta o que ela inseriu durante a coleta: ficar à
    # frente dele é o que cala o aviso na execução real.
    warnings.filterwarnings("default", category=DeprecationWarning, module="^yfinance")

    cli._configure_logging()
    cli._configure_logging()

    proprios = entradas_do_produto()
    # Os filtros do produto ocupam o início da lista, imediatamente à frente do filtro da
    # biblioteca de cotações: é essa posição que cala o aviso na execução real.
    assert warnings.filters[: len(proprios)] == proprios
    # Uma única cópia de cada par (categoria, namespace), ainda que ``_configure_logging``
    # seja chamada mais de uma vez.
    assert len({(entrada[2], entrada[3].pattern) for entrada in proprios}) == len(proprios)
    # Exatamente as categorias benignas em cada namespace: ``FutureWarning``,
    # ``RuntimeWarning`` e as de depreciação continuam caladas, não só uma delas.
    assert {
        (entrada[2], entrada[3].pattern)
        for entrada in proprios
        if entrada[2] in cli._BENIGN_WARNING_CATEGORIES
    } == pares_benignos()
    # Nenhum filtro do produto é ``Warning`` inteira: sem categoria ele calaria também o
    # aviso de segurança do ``urllib3``, que precisa continuar chegando a ``stderr``.
    assert not any(entrada[2] is Warning for entrada in proprios)
    inseguro = aviso_do_urllib3("InsecureRequestWarning")
    if inseguro is not None:
        assert not any(issubclass(inseguro, entrada[2]) for entrada in proprios)
    # O aviso benigno de biblioteca SSL não suportada tem filtro próprio, instalado só
    # quando a classe já está no processo; as demais categorias são sempre as benignas.
    not_openssl = aviso_do_urllib3("NotOpenSSLWarning")
    categorias = {entrada[2] for entrada in proprios}
    if not_openssl is None:
        assert categorias == set(cli._BENIGN_WARNING_CATEGORIES)
    else:
        assert categorias == set(cli._BENIGN_WARNING_CATEGORIES) | {not_openssl}

    da_biblioteca = [
        posicao
        for posicao, entrada in enumerate(warnings.filters)
        if entrada[0] == "default" and getattr(entrada[3], "pattern", "") == "^yfinance"
    ]
    assert da_biblioteca and da_biblioteca[0] == len(proprios)

    padroes = {entrada[3].pattern: entrada[3] for entrada in proprios}
    das_bibliotecas = padroes[cli._THIRD_PARTY_WARNING_MODULES]
    do_produto = padroes[cli._PRODUCT_WARNING_MODULES]
    for nome in ("yfinance.utils", "pandas.core.frame", "numpy.core", "curl_cffi.requests"):
        assert das_bibliotecas.match(nome)
    assert do_produto.match("finmail.quotes")
    # Um namespace apenas parecido não casa: a âncora impede o prefixo solto.
    assert not das_bibliotecas.match("yfinance_extra")
    assert not do_produto.match("finmailx")


# O caminho funcional do aviso, em subprocesso: ``warnings.showwarning`` escreve no
# ``stderr`` do processo e os filtros do pytest interferem na contagem.
WARNINGS_PROBE = (
    "import warnings\n"
    "import finmail.cli as cli\n"
    "cli._configure_warnings()\n"
    "warnings.warn_explicit('futuro', FutureWarning, 'x.py', 1, module='pandas.core.frame')\n"
    "warnings.warn_explicit('runtime', RuntimeWarning, 'y.py', 2, module='numpy.core')\n"
    "warnings.warn_explicit('atribuido', FutureWarning, 'z.py', 3, module='finmail.quotes')\n"
    "print('PROBE_DONE')\n"
)


def test_aviso_de_biblioteca_fora_do_namespace_do_yfinance_nao_chega_ao_stderr(
    run_python: Any,
) -> None:
    """Nenhuma categoria de aviso de biblioteca chega a ``stderr`` (AAP 0.6.2).

    Um ``FutureWarning`` de ``pandas``, um ``RuntimeWarning`` de ``numpy`` e um aviso que
    ``stacklevel`` atribuiu a ``finmail.quotes`` não podem escrever em ``stderr``, o
    stream reservado à linha única de diagnóstico fatal e às mensagens de uso. O marcador
    impresso no fim prova que a sonda chegou até lá: sem ele, um erro de importação
    deixaria ``stderr`` vazio e o teste passaria sem provar nada.
    """
    resultado = run_python(WARNINGS_PROBE)

    assert resultado.returncode == 0
    assert resultado.stderr == ""
    assert resultado.stdout.strip() == "PROBE_DONE"


# O aviso de segurança do ``urllib3`` na classe real, em subprocesso: o stream reservado à
# linha única de diagnóstico fatal tem de recebê-lo, e o ruído das bibliotecas de dados
# tem de continuar calado no mesmo processo.
TLS_WARNING_PROBE = (
    "import warnings\n"
    "import urllib3.exceptions\n"
    "import finmail.cli as cli\n"
    "cli._configure_warnings()\n"
    "warnings.warn_explicit('futuro', FutureWarning, 'x.py', 1, module='pandas.core.frame')\n"
    "warnings.warn_explicit('runtime', RuntimeWarning, 'y.py', 2, module='numpy.core')\n"
    "warnings.warn_explicit(\n"
    "    'FALHA_NA_VERIFICACAO', urllib3.exceptions.InsecureRequestWarning, 'tls.py', 3,\n"
    "    module='urllib3.connectionpool',\n"
    ")\n"
    "print('PROBE_DONE')\n"
)


def test_aviso_de_seguranca_do_urllib3_chega_ao_stderr(run_python: Any) -> None:
    """A perda de verificação de TLS continua visível com os filtros do produto.

    O filtro por namespace, sem categoria, calava também o ``InsecureRequestWarning`` do
    ``urllib3``: uma futura perda de verificação de TLS não apareceria em lugar nenhum.
    Com o conjunto de categorias benignas, o aviso de segurança atravessa os filtros e
    chega a ``stderr``, enquanto o ``FutureWarning`` de ``pandas`` e o ``RuntimeWarning``
    de ``numpy`` continuam calados no mesmo processo — a redução não reabriu ``stderr``
    para o ruído comum. O marcador impresso no fim prova que a sonda chegou até lá.
    """
    resultado = run_python(TLS_WARNING_PROBE)

    assert resultado.returncode == 0
    assert resultado.stdout.strip() == "PROBE_DONE"
    assert "FALHA_NA_VERIFICACAO" in resultado.stderr
    assert "InsecureRequestWarning" in resultado.stderr
    assert "futuro" not in resultado.stderr
    assert "runtime" not in resultado.stderr


# O operador que pede avisos como erro continua sendo obedecido: a escolha dele vem antes
# do filtro do produto, que só existe para calar ruído de biblioteca em ``stderr``.
ERROR_WARNINGS_PROBE = (
    "import warnings\n"
    "import finmail.cli as cli\n"
    "cli._configure_warnings()\n"
    "warnings.warn_explicit(\n"
    "    'DEPRECIACAO_COMO_ERRO', DeprecationWarning, 'd.py', 1,\n"
    "    module='pandas.core.frame',\n"
    ")\n"
    "print('PROBE_NAO_FALHOU')\n"
)


def test_pythonwarnings_error_vence_os_filtros_do_produto(run_python: Any) -> None:
    """``PYTHONWARNINGS=error`` é honrado: o aviso de biblioteca derruba o processo.

    Os filtros do produto entram no início de ``warnings.filters``, à frente dos que o
    interpretador monta a partir de ``-W``/``PYTHONWARNINGS``: sem o recuo de
    ``_configure_warnings``, o ``DeprecationWarning`` de ``pandas`` seria ignorado e a
    escolha do operador não teria efeito. O código de saída diferente de zero, com a
    categoria nomeada em ``stderr``, é o que prova que a exceção do aviso chegou ao fim
    do processo.
    """
    resultado = run_python(ERROR_WARNINGS_PROBE, env={"PYTHONWARNINGS": "error"})

    assert resultado.returncode != 0
    assert "DEPRECIACAO_COMO_ERRO" in resultado.stderr
    assert "DeprecationWarning" in resultado.stderr
    assert "PROBE_NAO_FALHOU" not in resultado.stdout


# Um registro de biblioteca em ``ERROR`` também não pode alcançar ``stderr``: o nível
# sozinho não impede ``logging.lastResort`` de escrever ali (AAP 0.6.2).


def test_registro_de_logger_de_terceiro_nao_chega_ao_stderr(
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    """``ERROR`` de logger de biblioteca não aparece em ``stderr`` nem em ``stdout``.

    A raiz é esvaziada antes da chamada para que o caso exercitado seja o de um
    processo real, sem handler nenhum na cadeia: é nele que ``logging.lastResort``
    escreveria o registro em ``stderr``, o stream reservado à linha única de
    diagnóstico fatal e às mensagens de uso (AAP 0.6.2). O ``monkeypatch`` devolve os
    handlers da raiz no teardown, e ``propagate = False`` mais o destino nulo de cada
    logger de ``QUIET_LOGGERS`` fecham o caminho independentemente da raiz.
    """
    root = logging.getLogger()
    monkeypatch.setattr(root, "handlers", list(root.handlers))
    root.handlers = []

    # Duas chamadas, como em cada execução: nada pode se acumular na segunda.
    cli._configure_logging()
    cli._configure_logging()

    logging.getLogger("yfinance").error("HTTP Error 404: quoteSummary")
    logging.getLogger("bs4").warning("ruído de parsing")

    out, err = capture(capfd)
    assert out == ""
    assert err == ""

    for name in cli.QUIET_LOGGERS:
        quiet = logging.getLogger(name)
        assert quiet.level == logging.ERROR
        assert quiet.propagate is False
        nulos = [
            handler for handler in quiet.handlers if isinstance(handler, logging.NullHandler)
        ]
        assert len(nulos) == 1
    # Um logger fora de ``QUIET_LOGGERS`` propaga até a raiz; o destino nulo dela é o
    # que impede o ``lastResort`` de escrever em ``stderr``.
    nulos_na_raiz = [
        handler for handler in root.handlers if isinstance(handler, logging.NullHandler)
    ]
    assert len(nulos_na_raiz) == 1


# Um valor hostil interpolado em um registro não pode dividir o evento em duas linhas nem
# levar um escape ANSI ao terminal do operador (AAP 0.6.2: uma linha por evento).


def test_valor_hostil_mantem_uma_linha_por_evento_de_log(
    monkeypatch: pytest.MonkeyPatch,
    config_writer: Any,
    capfd: pytest.CaptureFixture[str],
) -> None:
    """Ticker com ``\\n`` e com escape ANSI sai escapado, um evento por linha física.

    O ``\\n`` é o que dividiria o ``WARNING`` do ativo em duas linhas, deixando a segunda
    sem data, nível e logger; o escape ANSI é o que mudaria o estado do terminal. A região
    verificada é a que antecede o bloco de ``--dry-run``: o corpo do e-mail é saída de
    dados, impressa como o relatório a montou.
    """
    com_quebra = "AA\nPL"
    com_escape = "\x1b[31mFAKE\x1b[0m"
    falhas = [(com_quebra, MOTIVO_DA_FALHA), (com_escape, MOTIVO_DA_FALHA)]
    path = config_writer(tickers=["AAPL", com_quebra, com_escape])
    patch_quotes(monkeypatch, good=1, bad=falhas)

    assert cli.main(["run", "--dry-run", "--config", str(path)]) == cli.EXIT_OK

    out, err = capture(capfd)
    assert err == ""
    regiao = out.split("Assunto: ", 1)[0]
    # Um ``WARNING`` por ativo com falha e mais as duas linhas ``INFO`` da execução: a
    # contagem dos eventos vem do caminho do código.
    assert len(log_lines(regiao)) == 2 + len(falhas)
    # Toda linha física da região é um evento completo: nenhuma continuação órfã.
    assert all(LOG_LINE.match(linha) for linha in regiao.splitlines())
    assert "\x1b" not in regiao
    avisos = lines_with_level(regiao, "WARNING")
    assert len(avisos) == len(falhas)
    assert f"Ativo AA\\x0aPL sem dados: {MOTIVO_DA_FALHA}." in avisos[0]
    assert f"Ativo \\x1b[31mFAKE\\x1b[0m sem dados: {MOTIVO_DA_FALHA}." in avisos[1]


# Codificação dos streams: a saída pt-BR não depende do locale do host (AAP 0.6.2).
# ``PYTHONIOENCODING`` define o codec do processo antes de o produto rodar; o resumo
# acentuado de ``check-config`` e o travessão do assunto só saem porque ``main`` fixa
# os dois streams em UTF-8, e o código de saída continua sendo o do desfecho.


def test_check_config_imprime_o_resumo_acentuado_com_stdout_fora_de_utf8(
    run_finmail: Any,
    config_writer: Any,
) -> None:
    """Com ``PYTHONIOENCODING=ascii``, o resumo acentuado sai e o código é ``0``."""
    path = config_writer()

    result = run_finmail(
        "check-config",
        "--config",
        str(path),
        env={"PYTHONIOENCODING": "ascii"},
    )

    assert result.returncode == cli.EXIT_OK
    assert result.stderr == ""
    assert "Configuração válida." in result.stdout
    assert "SMTP_PASSWORD: não definido" in result.stdout
    # A linha de log de início também é acentuada, o que prova o mesmo caminho
    # pelo handler que escreve em ``sys.stdout``.
    assert any("configuração" in line for line in log_lines(result.stdout))


def test_dry_run_imprime_o_travessao_do_assunto_com_stdout_fora_de_utf8(
    run_finmail: Any,
    config_writer: Any,
) -> None:
    """O travessão do assunto (U+2013) sai mesmo com ``PYTHONIOENCODING=ascii``."""
    path = config_writer()

    result = run_finmail(
        "run",
        "--dry-run",
        "--config",
        str(path),
        env={"PYTHONIOENCODING": "ascii"},
    )

    assert result.returncode == cli.EXIT_OK
    assert result.stderr == ""
    assert "--- corpo texto ---" in result.stdout
    subject_line = next(
        line for line in result.stdout.splitlines() if line.startswith("Assunto: ")
    )
    assert subject_line.startswith("Assunto: Resumo de mercado – ")


# Interrupção do operador e ``stdout`` que não pode ser escrito — pipe fechado ou
# destino que recusa a escrita: os desfechos que o mapa de códigos não classifica por
# si entram no código de falha não classificada, e ``stderr`` continua com uma única
# linha (AAP 0.6.2, disciplina de streams).

INTERRUPT_PROBE = (
    "import sys\n"
    "import finmail.cli\n"
    "import finmail.quotes\n"
    "def interrupt(ticker):\n"
    "    raise KeyboardInterrupt()\n"
    "finmail.quotes.fetch = interrupt\n"
    "raise SystemExit(finmail.cli.main(['run', '--dry-run', '--config', sys.argv[1]]))\n"
)

CLOSED_STDOUT_PROBE = (
    "import os\n"
    "import sys\n"
    "import finmail.cli\n"
    "read_fd, write_fd = os.pipe()\n"
    "os.close(read_fd)\n"
    "os.dup2(write_fd, 1)\n"
    "os.close(write_fd)\n"
    "raise SystemExit(finmail.cli.main(['check-config', '--config', sys.argv[1]]))\n"
)

FULL_DEVICE_STDOUT_PROBE = (
    "import os\n"
    "import sys\n"
    "import finmail.cli\n"
    "full_fd = os.open('/dev/full', os.O_WRONLY)\n"
    "os.dup2(full_fd, 1)\n"
    "os.close(full_fd)\n"
    "raise SystemExit(finmail.cli.main(['check-config', '--config', sys.argv[1]]))\n"
)


def test_interrupcao_do_operador_sai_com_1_e_um_diagnostico_unico(
    run_python: Any,
    config_writer: Any,
) -> None:
    """``KeyboardInterrupt`` vira ``1`` com uma linha em ``stderr``, sem traceback."""
    path = config_writer()

    result = run_python(INTERRUPT_PROBE, str(path))

    assert result.returncode == cli.EXIT_UNEXPECTED
    assert len(result.stderr.splitlines()) == 1
    assert "interrompida" in result.stderr
    assert "Traceback" not in result.stderr


def test_pipe_de_stdout_fechado_sai_com_1_sem_ruido_em_stderr(
    run_python: Any,
    config_writer: Any,
) -> None:
    """Com o leitor do pipe fechado, a execução termina em ``1`` e sem traceback."""
    path = config_writer()

    result = run_python(CLOSED_STDOUT_PROBE, str(path))

    assert result.returncode == cli.EXIT_UNEXPECTED
    assert len(result.stderr.splitlines()) == 1
    assert "--- Logging error ---" not in result.stderr
    assert "Traceback" not in result.stderr
    assert "Exception ignored" not in result.stderr


@pytest.mark.skipif(
    not os.path.exists("/dev/full"),
    reason="o dispositivo /dev/full só existe em Linux, e o produto também roda em macOS",
)
def test_stdout_que_recusa_a_escrita_sai_com_1_e_um_diagnostico_unico(
    run_python: Any,
    config_writer: Any,
) -> None:
    """Com ``stdout`` em ``/dev/full``, a execução termina em ``1`` com uma linha só."""
    path = config_writer()

    # O redirecionamento do operador deixa ``stdout`` com buffer: a escrita só falha no
    # descarrego do fim da execução, que é onde ``_flush_stdout`` e ``_fatal`` relatam a
    # falha. O ``PYTHONUNBUFFERED`` do harness faria a primeira escrita falhar antes e
    # levaria o erro ao classificador de defeito inesperado.
    result = run_python(FULL_DEVICE_STDOUT_PROBE, str(path), env={"PYTHONUNBUFFERED": None})

    assert result.returncode == cli.EXIT_UNEXPECTED
    assert result.stderr.splitlines() == [
        "Saída padrão interrompida: a escrita em stdout falhou e a saída do "
        "comando ficou incompleta."
    ]
    # A linha de log perdida não pode chegar a ``stderr`` como erro do ``logging``.
    assert "--- Logging error ---" not in result.stderr
    assert "Traceback" not in result.stderr
    assert "Exception ignored" not in result.stderr


# ``run --dry-run``: assunto e corpo texto, sem HTML e sem SMTP (AAP 0.6.2, critério 4).


def test_dry_run_imprime_assunto_e_corpo_texto_sem_html(
    monkeypatch: pytest.MonkeyPatch,
    config_writer: Any,
    capfd: pytest.CaptureFixture[str],
) -> None:
    """O bloco impresso repete o e-mail: assunto, delimitador e corpo texto completo."""
    path = config_writer()
    configured = config_tickers(path)
    order = patch_quotes(monkeypatch, good=len(configured))
    sent = patch_mailer(monkeypatch)

    assert cli.main(["run", "--dry-run", "--config", str(path)]) == cli.EXIT_OK

    assert order == configured
    assert sent == []
    out, err = capture(capfd)
    assert err == ""
    assert "<html" not in out
    assert "<table" not in out
    assert out.index("Assunto: ") < out.index("--- corpo texto ---")

    subject_line = next(line for line in out.splitlines() if line.startswith("Assunto: "))
    subject = subject_line.removeprefix("Assunto: ")
    assert subject.startswith("Resumo de mercado – ")
    body = out.split("--- corpo texto ---\n", 1)[1]
    # As linhas de log vêm antes do bloco: nada de log depois do corpo do e-mail.
    lines = [line for line in body.splitlines() if not LOG_LINE.match(line)]
    # Uma linha por ativo, na ordem da configuração, entre o cabeçalho e o rodapé.
    assert lines[0] == subject
    assert lines[1].startswith("Ativo | ")
    for position, ticker in enumerate(configured, start=2):
        assert lines[position].startswith(f"{ticker} | ")
    assert lines[len(configured) + 2] == report_module.FOOTER
    assert len(lines) == len(configured) + 3
    # O bloco delimitado fecha a saída: o rodapé é a última coisa em ``stdout``.
    assert out.rstrip("\n").endswith(report_module.FOOTER)


def test_dry_run_mostra_dados_indisponiveis_e_sai_com_0(
    monkeypatch: pytest.MonkeyPatch,
    config_writer: Any,
    capfd: pytest.CaptureFixture[str],
) -> None:
    """Um ativo sem dados vira a linha ``dados indisponíveis`` também no terminal."""
    path = config_writer(tickers=TICKERS_COM_FALHA)
    patch_quotes(monkeypatch, good=2, bad=[("BTC-USD", MOTIVO_DA_FALHA)])
    sent = patch_mailer(monkeypatch)

    assert cli.main(["run", "--dry-run", "--config", str(path)]) == cli.EXIT_OK

    out, err = capture(capfd)
    assert err == ""
    assert "dados indisponíveis" in out
    assert MOTIVO_DA_FALHA in out
    assert sent == []


# O assunto data no fuso configurado (AAP 0.5.4, AAP 0.7.1).


@pytest.mark.parametrize(
    ("zone_name", "offset"),
    [
        pytest.param("America/Sao_Paulo", timedelta(hours=-3), id="sao-paulo"),
        pytest.param("Pacific/Kiritimati", timedelta(hours=14), id="kiritimati"),
    ],
)
def test_o_assunto_usa_o_fuso_configurado(
    monkeypatch: pytest.MonkeyPatch,
    credentials_env: Any,
    zone_name: str,
    offset: timedelta,
    capfd: pytest.CaptureFixture[str],
) -> None:
    """``report.build`` recebe o instante já no fuso do operador.

    Os dois fusos diferem em 17 horas, então nenhum relógio local explica os dois
    deslocamentos: o que decide é o campo ``timezone`` da configuração.
    """
    path = credentials_env(timezone=zone_name)
    moments = patch_build(monkeypatch)
    patch_quotes(monkeypatch, good=3)
    patch_mailer(monkeypatch)

    before = datetime.now(ZoneInfo(zone_name)).date()
    assert cli.main(["run", "--config", str(path)]) == cli.EXIT_OK
    after = datetime.now(ZoneInfo(zone_name)).date()

    assert len(moments) == 1
    now = moments[0]
    assert now.tzinfo is not None
    assert now.utcoffset() == offset
    assert now.date() in {before, after}

    # O mesmo fuso aparece no campo de data das linhas de log da execução.
    log_offset = f"{int(offset.total_seconds()) // 3600:+03d}00"
    out, err = capture(capfd)
    assert err == ""
    assert f"{log_offset} INFO finmail:" in out


# ``check-config``: resumo validado, sem rede e sem exigir credencial (AAP 0.6.1,
# AAP 0.7.1, critério 5).


def test_check_config_imprime_o_resumo_validado_sem_credenciais(
    config_writer: Any,
    capfd: pytest.CaptureFixture[str],
) -> None:
    """O resumo traz contagens, remetente, SMTP, fuso e o estado de cada variável."""
    path = config_writer()
    configuration: Config = config_module.load_config(path)

    assert cli.main(["check-config", "--config", str(path)]) == cli.EXIT_OK

    out, err = capture(capfd)
    assert err == ""
    assert f"tickers: {len(configuration.tickers)}" in out
    assert f"recipients: {len(configuration.recipients)}" in out
    for ticker in configuration.tickers:
        assert ticker not in out
    for recipient in configuration.recipients:
        assert recipient not in out
    assert configuration.sender in out
    assert f"smtp.host: {configuration.smtp.host}" in out
    assert f"smtp.port: {configuration.smtp.port}" in out
    assert f"timezone: {configuration.timezone_name}" in out
    assert "SMTP_USERNAME: não definido" in out
    assert "SMTP_PASSWORD: não definido" in out


def test_check_config_relata_credenciais_definidas_sem_mostrar_os_valores(
    credentials_env: Any,
    sentinel_username: str,
    sentinel_password: str,
    capfd: pytest.CaptureFixture[str],
) -> None:
    """Com as variáveis definidas, o resumo diz "definido" e não ecoa os valores."""
    path = credentials_env()

    assert cli.main(["check-config", "--config", str(path)]) == cli.EXIT_OK

    out, err = capture(capfd)
    assert "SMTP_USERNAME: definido" in out
    assert "SMTP_PASSWORD: definido" in out
    assert_absent({"stdout": out, "stderr": err}, sentinel_password)
    assert sentinel_username not in out
    assert sentinel_username not in err


def test_check_config_e_run_concordam_sobre_credencial_em_branco(
    monkeypatch: pytest.MonkeyPatch,
    config_writer: Any,
    capfd: pytest.CaptureFixture[str],
) -> None:
    """A variável só com espaços é "não definido" aqui e recusada pelo ``run``.

    É a costura entre os dois comandos: o resumo não pode anunciar uma credencial
    que a execução seguinte recusa, e o valor em branco não aparece em saída alguma.
    """
    blank = "   "
    monkeypatch.setenv("SMTP_USERNAME", blank)
    monkeypatch.setenv("SMTP_PASSWORD", blank)
    path = config_writer()

    assert cli.main(["check-config", "--config", str(path)]) == cli.EXIT_OK
    out, err = capture(capfd)
    assert err == ""
    presence = [line for line in out.splitlines() if line.startswith("SMTP_")]
    assert presence == ["SMTP_USERNAME: não definido", "SMTP_PASSWORD: não definido"]
    assert blank not in out

    assert cli.main(["run", "--config", str(path)]) == cli.EXIT_CONFIG
    out, err = capture(capfd)
    assert "SMTP_USERNAME" in err
    assert "vazia" in err
    assert blank not in out
    assert blank not in err

    # A mesma recusa vale para a senha: com o usuário aceitável, o ``run`` cita a
    # variável em branco que o resumo acima classificou como "não definido".
    monkeypatch.setenv("SMTP_USERNAME", "usuario-de-teste")
    assert cli.main(["run", "--config", str(path)]) == cli.EXIT_CONFIG
    out, err = capture(capfd)
    assert err == "Credencial ausente: variável SMTP_PASSWORD vazia.\n"
    assert blank not in out


def test_check_config_consulta_apenas_a_presenca_das_credenciais(
    monkeypatch: pytest.MonkeyPatch,
    config_writer: Any,
    capfd: pytest.CaptureFixture[str],
) -> None:
    """``check-config`` não chama ``load_credentials``: presença não é exigência."""

    def forbidden(*args: Any, **kwargs: Any) -> Credentials:
        raise AssertionError("load_credentials não pode ser chamado por check-config")

    monkeypatch.setattr(config_module, "load_credentials", forbidden)

    assert cli.main(["check-config", "--config", str(config_writer())]) == cli.EXIT_OK

    out, err = capture(capfd)
    assert err == ""
    assert "SMTP_PASSWORD: não definido" in out


# Sonda de isolamento: um interpretador separado, porque o ``sys.modules`` do
# processo do pytest já carregou ``yfinance`` por causa dos outros arquivos da suíte.
CHECK_CONFIG_PROBE = (
    "import sys\n"
    "import finmail.cli\n"
    "rc = finmail.cli.main(['check-config', '--config', sys.argv[1]])\n"
    "print('RC', rc)\n"
    "print('MOD_QUOTES', 'finmail.quotes' in sys.modules)\n"
    "print('MOD_YFINANCE', 'yfinance' in sys.modules)\n"
    "print('MOD_MAILER', 'finmail.mailer' in sys.modules)\n"
)


def test_check_config_nao_importa_yfinance(
    run_python: Any,
    config_writer: Any,
) -> None:
    """Critério 5: validar a configuração não carrega ``yfinance`` nem os adaptadores."""
    path = config_writer()

    result = run_python(CHECK_CONFIG_PROBE, str(path))

    assert result.returncode == cli.EXIT_OK
    assert subprocess_field(result.stdout, "RC") == str(cli.EXIT_OK)
    assert subprocess_field(result.stdout, "MOD_QUOTES") == "False"
    assert subprocess_field(result.stdout, "MOD_YFINANCE") == "False"
    assert subprocess_field(result.stdout, "MOD_MAILER") == "False"


def test_check_config_sem_recipients_sai_com_3(
    run_finmail: Any,
    config_writer: Any,
) -> None:
    """Critério 5 no nível do processo: código ``3`` e ``recipients`` na mensagem.

    O subprocesso roda com a guarda de rede carregada por ``sitecustomize``; uma
    tentativa de conexão externa mudaria o código de saída e derrubaria a asserção.
    """
    path = config_writer(recipients=[])

    result = run_finmail("check-config", "--config", str(path))

    assert result.returncode == cli.EXIT_CONFIG
    assert "recipients" in result.stderr
    assert "Traceback" not in result.stderr


def test_handle_check_config_devolve_0_para_uma_configuracao_valida(
    config_writer: Any,
    capfd: pytest.CaptureFixture[str],
) -> None:
    """O handler público devolve ``EXIT_OK`` e imprime o resumo."""
    configuration: Config = config_module.load_config(config_writer())

    assert cli.handle_check_config(configuration) == cli.EXIT_OK

    out, err = capture(capfd)
    assert err == ""
    assert "Configuração válida" in out


# Critério 6: o valor sentinela de ``SMTP_PASSWORD`` não aparece em saída alguma.


def test_o_localizador_de_segredo_encontra_um_vazamento_de_verdade(
    sentinel_password: str,
) -> None:
    """Uma mensagem que carrega o sentinela precisa ser acusada pelo mesmo localizador."""
    leaky = {"stdout": f"2026-11-13T00:00:00-0300 INFO finmail: {sentinel_password}\n"}

    with pytest.raises(AssertionError):
        assert_absent(leaky, sentinel_password)

    assert_absent({"stdout": "linha sem segredo", "stderr": ""}, sentinel_password)


def test_sentinela_de_senha_ausente_de_todas_as_saidas(
    monkeypatch: pytest.MonkeyPatch,
    credentials_env: Any,
    sentinel_username: str,
    sentinel_password: str,
    capfd: pytest.CaptureFixture[str],
) -> None:
    """As três invocações reais, com a senha definida, não a ecoam em lugar algum.

    Cada uma das três varreduras vem precedida da prova de que a captura daquela
    invocação não está vazia; sem ela, uma captura que devolvesse "" satisfaria a busca
    de ausência sem examinar saída alguma. As duas credenciais vêm da fixture
    ``credentials_env``, que já as define no ambiente e devolve o escritor de
    configuração.
    """
    path = credentials_env()

    def send(*args: Any, **kwargs: Any) -> None:
        raise mailer_module.MailError(
            "smtp.exemplo.com",
            587,
            "autenticação recusada (código 535)",
        )

    monkeypatch.setattr(mailer_module, "send", send)

    # Cada invocação recebe um dublê novo: o orçamento de cotações boas é por execução.
    patch_quotes(monkeypatch, good=3)
    assert cli.main(["run", "--config", str(path)]) == cli.EXIT_SMTP
    out, err = capture(capfd)
    # A vivacidade da captura é o que impede a varredura de ausência de passar sobre uma
    # saída vazia: o início da execução, o ``ERROR`` da falha de entrega e a linha única
    # de diagnóstico em ``stderr`` precisam estar aqui antes da busca.
    falha_de_entrega = "autenticação recusada (código 535)"
    assert lines_with_level(out, "INFO"), "a execução não registrou a linha de início"
    assert any(falha_de_entrega in linha for linha in lines_with_level(out, "ERROR")), (
        "o ERROR da falha de entrega não foi registrado"
    )
    assert any(falha_de_entrega in linha for linha in err.splitlines()), (
        "a linha única de diagnóstico não chegou a stderr"
    )
    assert_absent({"stdout": out, "stderr": err}, sentinel_password)
    assert sentinel_username not in out
    assert sentinel_username not in err

    patch_quotes(monkeypatch, good=3)
    assert cli.main(["run", "--dry-run", "--config", str(path)]) == cli.EXIT_OK
    out, err = capture(capfd)
    # Vivacidade: as duas linhas ``INFO`` da execução e a linha do assunto impressa.
    assert len(lines_with_level(out, "INFO")) >= 2, "os dois INFO da execução faltaram"
    assert any(linha.startswith("Assunto: ") for linha in out.splitlines()), (
        "o bloco impresso não traz a linha do assunto"
    )
    assert_absent({"stdout": out, "stderr": err}, sentinel_password)
    assert sentinel_username not in out
    assert sentinel_username not in err

    assert cli.main(["check-config", "--config", str(path)]) == cli.EXIT_OK
    out, err = capture(capfd)
    # Vivacidade: o resumo validado e a linha ``INFO`` de início antes da busca.
    assert any(linha == "Configuração válida." for linha in out.splitlines()), (
        "o resumo validado não foi impresso"
    )
    assert lines_with_level(out, "INFO"), "a execução não registrou a linha de início"
    assert_absent({"stdout": out, "stderr": err}, sentinel_password)
    assert sentinel_username not in out
    assert sentinel_username not in err


# Importações por ramo: ``run --dry-run`` não carrega o ``mailer`` (AAP 0.5.1).


DRY_RUN_PROBE = (
    "import sys\n"
    "import finmail.cli\n"
    "rc = finmail.cli.main(['run', '--dry-run', '--config', sys.argv[1]])\n"
    "print('RC', rc)\n"
    "print('MOD_MAILER', 'finmail.mailer' in sys.modules)\n"
    "print('MOD_SMTPLIB', 'smtplib' in sys.modules)\n"
)


def test_dry_run_nao_carrega_o_mailer_e_nao_abre_conexao_smtp(
    run_python: Any,
    config_writer: Any,
    closed_port: int,
) -> None:
    """Com o SMTP apontado para uma porta fechada, ``--dry-run`` ainda sai com ``0``.

    Qualquer tentativa de conexão dentro do ``--dry-run`` seria recusada pelo SO e
    terminaria em ``4``; o ``0`` prova que o ramo do ``mailer`` não foi percorrido.
    """
    path = config_writer(smtp={"host": "localhost", "port": closed_port})

    result = run_python(DRY_RUN_PROBE, str(path))

    assert result.returncode == cli.EXIT_OK
    assert subprocess_field(result.stdout, "RC") == str(cli.EXIT_OK)
    assert subprocess_field(result.stdout, "MOD_MAILER") == "False"
    # Sinal secundário: nenhuma importação transitiva trouxe o ``smtplib`` junto.
    assert subprocess_field(result.stdout, "MOD_SMTPLIB") == "False"
    assert "--- corpo texto ---" in result.stdout
    assert "<html" not in result.stdout


# Restrição proibitiva "Somente as dependências listadas" (AAP 0.9, tabela de
# restrições): a verificação designada é esta inspeção do manifesto, que confere os
# pins exatos da AAP 0.4.1 e a ausência de ``[project.scripts]`` da AAP 0.6.3.

MANIFEST_PATH = Path(__file__).resolve().parents[1] / "pyproject.toml"

# Pins exatos por grupo de declaração do manifesto. A comparação por igualdade de
# conjunto é o que exclui faixa de versão, ``latest`` e pacote acrescentado.
RUNTIME_PINS = frozenset({"yfinance==0.2.66", "pandas==2.3.3", "PyYAML==6.0.3", "Jinja2==3.1.6"})
DEV_PINS = frozenset({"pytest==8.4.2", "aiosmtpd==1.4.6"})
BUILD_PINS = frozenset({"setuptools==84.2.1"})


def test_manifesto_declara_apenas_as_dependencias_listadas() -> None:
    """O manifesto declara só as dependências listadas, em versão exata.

    O ``tomllib`` da biblioteca padrão lê o ``pyproject.toml`` sem dependência nova.
    Cada conjunto de declarações é comparado por igualdade com os pins da AAP 0.4.1,
    e o manifesto não pode trazer ``[project.scripts]``.
    """
    manifest = tomllib.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    project: Any = manifest["project"]
    build_system: Any = manifest["build-system"]

    assert set(project["dependencies"]) == RUNTIME_PINS
    assert set(project["optional-dependencies"]) == {"dev"}
    assert set(project["optional-dependencies"]["dev"]) == DEV_PINS
    assert set(build_system["requires"]) == BUILD_PINS
    # Um console script criaria uma segunda forma de invocação ao lado de
    # ``python -m finmail``, que é a única que a AAP 0.6.3 fixa.
    assert "scripts" not in project
