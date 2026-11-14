"""Fim a fim do finmail: critérios 1 a 4 da AAP 0.9 pelo caminho real da CLI.

Cada teste executa ``python -m finmail ...`` em um subprocesso, exatamente como o
operador e o ``cron`` o executam, e afirma apenas o que um observador externo
recebe: o código de saída, os streams do processo e os envelopes que o servidor
SMTP local coletou. Nenhum símbolo de ``finmail`` é importado e nenhuma função do
produto é chamada aqui — é isso que torna o critério 1 uma prova de ponta a ponta,
e não um teste de funções internas com o caminho real por fora.

Os dois contatos com o mundo externo são substituídos pelo harness de
``tests/conftest.py``, sem costura alguma no produto: o dublê ``yfinance`` de
``tests/fixtures/stub`` vai à frente do ``site-packages`` no ``PYTHONPATH`` do
subprocesso, e o ``aiosmtpd`` escuta em ``localhost`` com STARTTLS e AUTH. Nenhum
teste deste arquivo alcança a rede externa (a guarda de ``tests/fixtures/guard``
recusa qualquer destino fora de loopback), e nenhum lê credencial real: o par
usado é o sentinela fictício do harness, passado explicitamente em ``env``.

Fora do escopo deste arquivo, por decisão da AAP 0.9: o critério 7 (uma execução
real com ``AAPL`` e ``PETR4.SA``) e a medição de 120 s no p95 dependem da rede e do
Yahoo Finance e ficam manuais, no ``README.md``.
"""

import re
import socket
from collections.abc import Callable, Sequence
from email.header import decode_header, make_header
from email.message import Message
from pathlib import Path
from typing import Any

import pytest

# Endereços da configuração padrão do harness: remetente do envelope e destinatários
# que o servidor local precisa aceitar, todos no mesmo e único envelope.
SENDER = "remetente@exemplo.com"
RECIPIENTS = ("destinatario1@exemplo.com", "destinatario2@exemplo.com")

# Ativos que o dublê serve com dados reais em forma (AAP 0.9, tabela do dublê).
TICKERS_OK = ("AAPL", "PETR4.SA", "MSFT")
# O terceiro falha de forma determinística: ``YFPricesMissingError`` no dublê.
TICKERS_WITH_FAILURE = ("AAPL", "PETR4.SA", "BTC-USD")
# Os dois falham: preços ausentes e símbolo inexistente, sem qualquer nova tentativa.
TICKERS_ALL_FAILING = ("BTC-USD", "INVALID")

# Literais do relatório que a AAP 0.5.4 fixa e que este arquivo confere no corpo
# recebido, nunca em código do produto.
FOOTER = "Dados fornecidos pelo Yahoo Finance. Os valores podem apresentar atraso."
COLUMN_HEADER = (
    "Ativo | Nome | Moeda | Data | Fechamento | Variação | Variação % | "
    "Volume | Mín. 52s | Máx. 52s | Estado"
)
COLUMN_COUNT = 11
UNAVAILABLE = "dados indisponíveis:"
NOT_AVAILABLE = "n/d"
OK = "ok"
COLUMN_SEPARATOR = " | "

# Texto que o dublê registra no logger da biblioteca real, ``yfinance``, para um símbolo
# sem cotação — o mesmo que a biblioteca de verdade escreve. É o ruído de terceiro que o
# contrato do AAP 0.6.2 mantém fora de ``stdout`` e de ``stderr``: sem o destino nulo que
# o produto instala nesse logger, o registro chegaria a ``stderr`` por
# ``logging.lastResort``, e a asserção de ``stderr`` vazio abaixo passaria a falhar.
LIBRARY_NOISE = "HTTP Error 404"

# Bloco impresso por ``run --dry-run``: rótulo do assunto e delimitador do corpo.
SUBJECT_LABEL = "Assunto: "
DRY_RUN_DELIMITER = "--- corpo texto ---"

# Assunto com travessão EN (U+2013) e a data corrente; a data é o único valor não
# determinístico, então só o formato é afirmado.
SUBJECT_PATTERN = re.compile("^Resumo de mercado \u2013 \\d{4}-\\d{2}-\\d{2}$")


@pytest.fixture
def credentials_env(sentinel_username: str, sentinel_password: str) -> dict[str, str]:
    """Par de credenciais fictício do harness, para o ``env=`` de ``run_finmail``.

    Vive aqui, e não em ``tests/conftest.py``, porque só o caminho real de ``run``
    precisa de credencial: ``--dry-run`` e ``check-config`` não podem receber
    nenhuma. O valor é o sentinela do harness, nunca uma credencial real.
    """
    return {"SMTP_USERNAME": sentinel_username, "SMTP_PASSWORD": sentinel_password}


def smtp_config(
    writer: Callable[..., Path],
    harness: Any,
    **overrides: Any,
) -> Path:
    """Escreve uma configuração apontando ``smtp`` para o servidor local do teste.

    Os demais campos vêm do padrão do harness e podem ser trocados por ``overrides``
    — ``tickers`` e ``recipients``, nos casos deste arquivo.
    """
    settings: dict[str, Any] = {"host": harness.host, "port": harness.port}
    settings.update(overrides.pop("smtp", {}))
    return writer(smtp=settings, **overrides)


def text_and_html(message: Message) -> tuple[Message, Message]:
    """Devolve as partes ``text/plain`` e ``text/html`` da mensagem recebida.

    As duas precisam existir: um corpo ``multipart/alternative`` com uma única parte
    não atende ao critério 1, e a falha tem de aparecer aqui, nomeando a parte que
    falta, em vez de virar um ``None`` adiante.
    """
    parts = list(message.walk())
    text_part = next(
        (part for part in parts if part.get_content_type() == "text/plain"), None
    )
    html_part = next(
        (part for part in parts if part.get_content_type() == "text/html"), None
    )
    assert text_part is not None, "a mensagem recebida não tem parte text/plain"
    assert html_part is not None, "a mensagem recebida não tem parte text/html"
    return text_part, html_part


def body_of(part: Message) -> str:
    """Conteúdo textual de uma parte MIME, decodificado pelo charset declarado."""
    charset = part.get_content_charset() or "utf-8"
    payload = part.get_payload(decode=True)
    assert isinstance(payload, bytes), "a parte MIME não tem conteúdo decodificável"
    return payload.decode(charset)


def bodies(envelope: Any) -> tuple[str, str]:
    """Corpo texto e corpo HTML de um envelope coletado pelo servidor local."""
    text_part, html_part = text_and_html(envelope.message)
    return body_of(text_part), body_of(html_part)


def assert_utf8(part: Message) -> None:
    """Afirma que a parte declara UTF-8, sem o qual acentos e travessão não sobrevivem."""
    assert part.get_content_charset() == "utf-8", (
        f"a parte {part.get_content_type()} declara charset "
        f"{part.get_content_charset()!r}, esperado 'utf-8'"
    )


def decoded_header(message: Message, name: str) -> str:
    """Valor legível de um cabeçalho, desfazendo o encoded-word RFC 2047."""
    raw = message.get(name)
    assert isinstance(raw, str), f"a mensagem recebida não tem o cabeçalho {name}"
    return str(make_header(decode_header(raw)))


def split_line(line: str) -> list[str]:
    """As onze colunas de uma linha do corpo texto, na ordem da AAP 0.5.4."""
    fields = line.split(COLUMN_SEPARATOR)
    assert len(fields) == COLUMN_COUNT, (
        f"linha com {len(fields)} colunas, esperado {COLUMN_COUNT}: {line!r}"
    )
    return fields


def asset_lines(text: str) -> list[str]:
    """Linhas de ativo do corpo texto, entre o cabeçalho de colunas e o rodapé.

    A primeira linha é o assunto e a segunda o cabeçalho, como a AAP 0.5.4 fixa; o
    rodapé de origem e atraso fecha o corpo. Tudo o que está entre eles é uma linha
    por ativo configurado.
    """
    lines = text.splitlines()
    assert len(lines) >= 3, f"corpo texto curto demais: {text!r}"
    assert lines[1] == COLUMN_HEADER, f"cabeçalho de colunas inesperado: {lines[1]!r}"
    assert lines[-1] == FOOTER, f"rodapé inesperado: {lines[-1]!r}"
    return lines[2:-1]


def describe(proc: Any) -> str:
    """Saída completa do subprocesso, anexada às asserções de diagnóstico."""
    return (
        f"código de saída {proc.returncode}\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )


def row_positions(html: str, tickers: Sequence[str]) -> list[int]:
    """Posição de cada ``<td>ticker</td>`` no HTML, na ordem em que aparecem."""
    return [html.index(f"<td>{ticker}</td>") for ticker in tickers]


# Critério 1 (AAP 0.9): a prova mais importante do produto, pelo caminho real da CLI.


def test_run_entrega_uma_mensagem_com_uma_linha_por_ativo(
    run_finmail: Callable[..., Any],
    smtp_server: Any,
    config_writer: Callable[..., Path],
    credentials_env: dict[str, str],
) -> None:
    """``run`` entrega exatamente 1 mensagem com todos os destinatários e 1 linha por ativo.

    A mensagem é observada no servidor SMTP local: um único envelope, com os dois
    destinatários configurados no mesmo cabeçalho, as partes ``text/plain`` e
    ``text/html`` em UTF-8 e uma linha por ativo na ordem da configuração. Um envio
    por destinatário, uma repetição ou uma linha faltando derrubam o teste.
    """
    path = smtp_config(config_writer, smtp_server, tickers=list(TICKERS_OK))

    proc = run_finmail("run", "--config", str(path), env=credentials_env)

    assert proc.returncode == 0, describe(proc)

    # Exatamente um envelope: nem duplicata, nem uma mensagem por destinatário.
    assert len(smtp_server.envelopes) == 1, describe(proc)
    envelope = smtp_server.envelopes[0]
    assert envelope.mail_from == SENDER
    assert len(envelope.rcpt_tos) == len(RECIPIENTS), envelope.rcpt_tos
    assert set(envelope.rcpt_tos) == set(RECIPIENTS), envelope.rcpt_tos

    message = envelope.message
    assert message.get_content_type() == "multipart/alternative"
    part_types = [part.get_content_type() for part in message.walk()]
    # RFC 2046 §5.1.4: o cliente escolhe a última alternativa que sabe exibir, então
    # o texto vem primeiro e o HTML por último.
    assert part_types.index("text/plain") < part_types.index("text/html"), part_types

    text_part, html_part = text_and_html(message)
    assert_utf8(text_part)
    assert_utf8(html_part)
    text, html = bodies(envelope)

    rows = asset_lines(text)
    assert len(rows) == len(TICKERS_OK), text
    assert [split_line(row)[0] for row in rows] == list(TICKERS_OK), text
    assert [split_line(row)[-1] for row in rows] == [OK] * len(TICKERS_OK), text

    # Uma linha de tabela por ativo, mais a do cabeçalho em <thead>.
    assert html.count("<tr") == 1 + len(TICKERS_OK), html
    positions = row_positions(html, TICKERS_OK)
    assert positions == sorted(positions), positions

    subject = decoded_header(message, "Subject")
    assert SUBJECT_PATTERN.match(subject), subject
    # O assunto do cabeçalho é o mesmo da primeira linha do corpo texto.
    assert text.splitlines()[0] == subject
    assert FOOTER in text
    assert FOOTER in html

    # Prova de §0.7.2 para o corpo: as credenciais são passadas em ``env`` e só
    # podem chegar a ``smtp.login``, nunca ao corpo entregue. A varredura repete
    # as conferências que fazem de cada parte o relatório de verdade, para que a
    # ausência só valha sobre um corpo que de fato chegou; a falha nomeia a parte
    # e a variável, jamais o valor procurado.
    delivered = {"texto": text, "html": html}
    for part_name, part_body in delivered.items():
        assert part_body.strip(), f"corpo {part_name} vazio na mensagem recebida"
        assert FOOTER in part_body, f"corpo {part_name} sem o rodapé de origem"
    assert rows, "corpo texto recebido sem nenhuma linha de ativo"
    assert html.count("<tr") > 1, "corpo html recebido sem nenhuma linha de ativo"

    for variable in ("SMTP_USERNAME", "SMTP_PASSWORD"):
        secret = credentials_env[variable]
        for part_name, part_body in delivered.items():
            # O booleano intermediário evita que a introspecção do ``pytest``
            # imprima o valor procurado junto da mensagem de falha.
            leaked = secret in part_body
            assert not leaked, (
                f"o valor de {variable} aparece no corpo {part_name} recebido"
            )

    # Os valores vêm do dublê, não de uma consulta real ao Yahoo Finance.
    assert "Apple Inc." in text
    assert "Petróleo Brasileiro S.A. - Petrobras" in text
    assert "Microsoft Corporation" in text

    assert proc.stderr == ""
    # O dublê registra no logger da biblioteca real, mas só no caminho de falha: numa
    # execução em que todos os ativos retornam dados, o texto da biblioteca não pode
    # aparecer em nenhum dos dois streams.
    assert LIBRARY_NOISE not in proc.stdout, describe(proc)
    assert LIBRARY_NOISE not in proc.stderr, describe(proc)
    # A linha INFO de progresso nomeia o caminho da configuração usada.
    assert str(path) in proc.stdout, describe(proc)
    assert "INFO" in proc.stdout


# Critério 2 (AAP 0.9): uma falha de ativo degrada a linha, nunca a execução.


def test_run_parcial_envia_com_tres_linhas(
    run_finmail: Callable[..., Any],
    smtp_server: Any,
    config_writer: Callable[..., Path],
    credentials_env: dict[str, str],
) -> None:
    """Dois ativos com dados e um com erro produzem 3 linhas, e a saída é ``0``.

    A linha do ativo que falhou mantém o ticker na posição configurada, mostra
    ``n/d`` nas nove colunas de mercado e ``dados indisponíveis: <motivo>`` na
    coluna ``Estado``; as duas linhas boas seguem intactas e na ordem, o e-mail é
    enviado e a falha aparece como ``WARNING`` no log.
    """
    path = smtp_config(config_writer, smtp_server, tickers=list(TICKERS_WITH_FAILURE))

    proc = run_finmail("run", "--config", str(path), env=credentials_env)

    # Uma falha de ativo não pode derrubar a execução nem mudar o código de saída.
    assert proc.returncode == 0, describe(proc)
    assert len(smtp_server.envelopes) == 1, describe(proc)
    envelope = smtp_server.envelopes[0]
    assert set(envelope.rcpt_tos) == set(RECIPIENTS), envelope.rcpt_tos

    text, html = bodies(envelope)
    rows = asset_lines(text)
    assert len(rows) == len(TICKERS_WITH_FAILURE), text
    assert [split_line(row)[0] for row in rows] == list(TICKERS_WITH_FAILURE), text

    # As duas linhas boas permanecem completas e na ordem da configuração.
    good_aapl = split_line(rows[0])
    good_petr = split_line(rows[1])
    assert good_aapl[-1] == OK
    assert good_aapl[1] == "Apple Inc."
    assert good_petr[-1] == OK
    assert good_petr[1] == "Petróleo Brasileiro S.A. - Petrobras"

    # A linha do ativo que falhou: ticker na posição, nove ``n/d`` e o motivo no fim.
    failed = split_line(rows[2])
    assert failed[0] == "BTC-USD"
    assert failed[1:-1] == [NOT_AVAILABLE] * 9, failed
    assert failed[-1].startswith(UNAVAILABLE), failed[-1]

    assert html.count("<tr") == 1 + len(TICKERS_WITH_FAILURE), html
    # O motivo vem de ``QuoteError.reason``; aqui só o literal comum é afirmado.
    assert "dados indisponíveis" in html

    assert "WARNING" in proc.stdout, describe(proc)
    assert "BTC-USD" in proc.stdout, describe(proc)
    # O WARNING do produto para o ativo que falhou continua em ``stdout``, e o registro
    # que a biblioteca de cotações escreve para o mesmo símbolo fica fora dos dois
    # streams: é o destino nulo do logger dela que sustenta o contrato do AAP 0.6.2.
    warning_lines = [
        line for line in proc.stdout.splitlines() if " WARNING " in line and "BTC-USD" in line
    ]
    assert warning_lines, describe(proc)
    assert LIBRARY_NOISE not in proc.stdout, describe(proc)
    assert LIBRARY_NOISE not in proc.stderr, describe(proc)
    assert proc.stderr == ""


# Critério 3 (AAP 0.9): sem nenhum ativo cotado, nenhuma mensagem é montada.


def test_run_todos_falham_nao_envia(
    run_finmail: Callable[..., Any],
    smtp_server: Any,
    config_writer: Callable[..., Path],
    credentials_env: dict[str, str],
) -> None:
    """Com todos os ativos falhando, nada chega ao servidor SMTP e a saída é ``2``.

    O servidor está de pé e aceitaria um envelope: a lista vazia prova que o produto
    não montou relatório algum, e não que a porta estava fechada. O ``2`` é exclusivo
    desta situação, e é o que o ``cron`` do operador distingue.
    """
    path = smtp_config(config_writer, smtp_server, tickers=list(TICKERS_ALL_FAILING))

    proc = run_finmail("run", "--config", str(path), env=credentials_env)

    assert proc.returncode == 2, describe(proc)
    assert smtp_server.envelopes == [], describe(proc)

    # O ERROR nomeia a contagem de falhas e o diagnóstico vai também para stderr.
    assert "ERROR" in proc.stdout, describe(proc)
    assert f"{len(TICKERS_ALL_FAILING)} ativos com falha" in proc.stdout, describe(proc)
    assert "nenhum e-mail foi enviado" in proc.stderr, describe(proc)
    # ``stderr`` recebe exatamente uma linha, a de diagnóstico fatal, e nem ela nem
    # ``stdout`` trazem o registro que a biblioteca escreve para cada símbolo que falhou.
    assert len(proc.stderr.splitlines()) == 1, describe(proc)
    assert LIBRARY_NOISE not in proc.stdout, describe(proc)
    assert LIBRARY_NOISE not in proc.stderr, describe(proc)

    # Nenhum corpo foi impresso: o caminho para antes de montar a mensagem.
    assert SUBJECT_LABEL not in proc.stdout, describe(proc)
    assert DRY_RUN_DELIMITER not in proc.stdout, describe(proc)
    assert COLUMN_HEADER not in proc.stdout, describe(proc)

    assert "Traceback" not in proc.stdout + proc.stderr, describe(proc)


# Critério 4 (AAP 0.9): ``run --dry-run`` imprime o e-mail e não abre conexão SMTP.


def test_dry_run_nao_abre_conexao_smtp(
    run_finmail: Callable[..., Any],
    config_writer: Callable[..., Path],
    closed_port: int,
) -> None:
    """``--dry-run`` imprime assunto e corpo texto, sem exigir credencial e sem SMTP.

    A configuração aponta ``smtp.port`` para uma porta onde nada escuta: qualquer
    tentativa de conexão seria recusada e terminaria em ``MailError`` com código
    ``4``, então o ``0`` prova que nenhuma tentativa foi feita. As credenciais não
    são passadas em ``env`` e as variáveis estão ausentes do ambiente do
    subprocesso, de modo que o ``0`` também prova que o caminho não as exige.
    """
    path = config_writer(
        smtp={"host": "localhost", "port": closed_port},
        tickers=list(TICKERS_OK),
    )

    # Sanidade: a porta está mesmo fechada, então um ``0`` adiante tem significado.
    with pytest.raises(ConnectionRefusedError):
        socket.create_connection(("127.0.0.1", closed_port), timeout=0.5)

    proc = run_finmail("run", "--dry-run", "--config", str(path))

    assert proc.returncode == 0, describe(proc)

    lines = proc.stdout.splitlines()
    subject_lines = [line for line in lines if line.startswith(SUBJECT_LABEL)]
    assert len(subject_lines) == 1, describe(proc)
    subject = subject_lines[0][len(SUBJECT_LABEL):]
    assert SUBJECT_PATTERN.match(subject), subject

    # O bloco delimitado: assunto, delimitador e o corpo texto completo.
    assert DRY_RUN_DELIMITER in lines, describe(proc)
    assert lines.index(DRY_RUN_DELIMITER) > lines.index(subject_lines[0])
    body = proc.stdout.split(DRY_RUN_DELIMITER + "\n", 1)[1]
    # O corpo termina no rodapé, que fecha o bloco e a saída do comando.
    body = body[: body.index(FOOTER) + len(FOOTER)]
    # A ordem do AAP 0.6.2 vale no processo real: o bloco delimitado é a última
    # coisa em ``stdout``, sem nenhuma linha de log depois dele.
    assert proc.stdout.rstrip("\n").endswith(FOOTER), describe(proc)

    assert body.splitlines()[0] == subject
    rows = asset_lines(body)
    assert len(rows) == len(TICKERS_OK), body
    assert [split_line(row)[0] for row in rows] == list(TICKERS_OK), body

    # Só o corpo texto é impresso: o HTML continua sendo exclusividade do e-mail.
    assert "<!DOCTYPE" not in proc.stdout, describe(proc)
    assert "<table" not in proc.stdout, describe(proc)

    # Nenhuma credencial foi exigida nem mencionada.
    assert "SMTP_PASSWORD" not in proc.stdout, describe(proc)
    assert "SMTP_USERNAME" not in proc.stdout, describe(proc)
    assert proc.stderr == "", describe(proc)
