"""Assunto, corpo texto e corpo HTML do resumo diário de mercado.

Uma única passagem por :func:`build` percorre a lista ordenada de ``Quote`` e
``QuoteError``, formata cada campo no padrão pt-BR e entrega o modelo de visão aos
dois templates empacotados em ``finmail/templates``. O módulo é puro: não lê
arquivo, rede nem ambiente, não importa outro adaptador e não consulta o relógio —
a data do assunto vem do ``datetime`` injetado pelo chamador, de modo que a mesma
entrada produz sempre os mesmos bytes.

A formatação pt-BR (vírgula decimal, ponto de milhar, sentinela ``n/d``) é feita
aqui sem ``locale``, e nenhum valor sai pré-escapado: o escape de HTML é
responsabilidade do autoescape do template ``report.html.j2``, enquanto o corpo
texto mantém os nomes literais. Todo valor de texto atravessa um único
normalizador, que o deixa em uma linha física e sem o ``|`` do separador de
colunas; por isso os dois corpos trazem exatamente a mesma célula.
"""

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import TYPE_CHECKING, Any

from jinja2 import Environment, PackageLoader, select_autoescape

if TYPE_CHECKING:
    # Importado apenas para anotação: em tempo de execução este adaptador não
    # depende de outro adaptador, e a lista de dependências fica só com o jinja2.
    from finmail.quotes import Quote, QuoteError

# Rodapé fixo, idêntico nos dois corpos: origem dos dados e possível atraso.
FOOTER = "Dados fornecidos pelo Yahoo Finance. Os valores podem apresentar atraso."
# Travessão EN (U+2013) entre o título e a data, como o pedido fixa.
SUBJECT_TEMPLATE = "Resumo de mercado – {date}"

# Sentinela de valor ausente: as nove colunas de mercado de uma linha com falha e
# todo campo opcional que o Yahoo Finance não informou.
NOT_AVAILABLE = "n/d"

POSITIVO = "positivo"
NEGATIVO = "negativo"
NEUTRO = "neutro"

# Os dois literais da coluna ``Estado``: entregues aos templates pelo contexto de
# :func:`build`, para que a célula seja composta de ``falha`` e ``motivo`` sem que o
# texto fixo apareça duas vezes.
_UNAVAILABLE_PREFIX = "dados indisponíveis"
_OK = "ok"
_TEXT_TEMPLATE = "report.txt.j2"
_HTML_TEMPLATE = "report.html.j2"
# Caractere intermediário da troca de separadores, fora de qualquer número formatado.
_GROUP_MARK = "\x00"
# ``\s`` cobre também as quebras que ``str.splitlines`` honra além de CR e LF — ``\x85``,
# ``\u2028`` e ``\u2029`` —, então colapsá-lo mantém uma linha física por ativo.
_WHITESPACE = re.compile(r"\s+")
# O corpo texto separa as colunas com ``" | "``; um ``|`` trazido por um valor se
# confunde com o separador e parte a linha em campos a mais, então todo ``|`` vira
# barra quebrada (``¦``), que mantém o valor legível e a célula livre do separador.
_PIPE_SUBSTITUTE = "¦"

# O autoescape é habilitado por extensão: só o template HTML escapa ``&``, ``<``,
# ``>``, ``'`` e ``"``; o corpo texto preserva ``AT&T`` e ``Lowe's`` literais.
_ENVIRONMENT = Environment(
    loader=PackageLoader("finmail", "templates"),
    autoescape=select_autoescape(
        enabled_extensions=("html.j2",),
        default_for_string=False,
    ),
)


@dataclass(frozen=True)
class Report:
    """Relatório pronto para envio, resultado puro da renderização.

    ``subject`` alimenta o cabeçalho ``Subject`` da mensagem, ``text`` e ``html``
    são as duas partes do ``multipart/alternative`` montado por ``finmail.mailer``
    e o mesmo ``text`` é impresso por ``run --dry-run``.
    """

    subject: str
    text: str
    html: str


def build(entries: Sequence["Quote | QuoteError"], now: datetime) -> Report:
    """Monta assunto, corpo texto e corpo HTML a partir das entradas ordenadas.

    Uma linha por entrada, na ordem recebida, que é a ordem de ``Config.tickers``
    garantida por ``cli.handle_run``. Uma entrada ``QuoteError`` vira a linha de
    falha do próprio ticker, com ``n/d`` nas nove colunas de mercado; a coluna
    ``Estado`` é composta pelos dois templates a partir de ``falha`` e ``motivo``,
    de modo que o motivo tem uma representação única no modelo de visão e nenhum
    dado ausente levanta exceção.

    Args:
        entries: Cotações e falhas por ativo, na ordem da configuração.
        now: Instante da execução, já no fuso configurado pelo operador; a data
            do assunto sai dele, nunca de um relógio interno.

    Returns:
        O trio ``(subject, text, html)`` consumido por ``finmail.mailer`` e
        impresso por ``run --dry-run``.
    """
    rows = [_row_for_entry(entry) for entry in entries]
    subject = SUBJECT_TEMPLATE.format(date=now.strftime("%Y-%m-%d"))
    context = {
        "subject": subject,
        "rows": rows,
        "footer": FOOTER,
        "estado_ok": _OK,
        "estado_falha": _UNAVAILABLE_PREFIX,
    }
    return Report(
        subject=subject,
        text=_ENVIRONMENT.get_template(_TEXT_TEMPLATE).render(**context),
        html=_ENVIRONMENT.get_template(_HTML_TEMPLATE).render(**context),
    )


def _row_for_entry(entry: "Quote | QuoteError") -> dict[str, Any]:
    """Converte uma entrada em uma linha do relatório já formatada.

    ``QuoteError`` deriva de ``Exception`` e é a única falha esperada por ativo;
    por isso a discriminação usa ``isinstance(entry, BaseException)`` em vez de
    importar ``finmail.quotes`` em tempo de execução.
    """
    if isinstance(entry, BaseException):
        return _failure_row(entry)
    return _success_row(entry)


def _success_row(quote: "Quote") -> dict[str, Any]:
    """Linha de um ativo cotado, com os valores formatados e ``falha`` = ``False``.

    ``motivo`` sai vazio porque só a linha com falha o preenche; é de ``falha`` e
    ``motivo`` que os dois templates compõem a coluna ``Estado``, que aqui sai
    ``ok``.

    As duas colunas de variação são formatadas de forma independente: cada uma
    leva o texto e a direção do próprio valor, porque os dois números arredondam
    em pontos diferentes — um centavo sobre 358,42 é ``+0,01`` ao lado de
    ``0,00%``.
    """
    variacao, direcao = _variacao(quote.change)
    variacao_pct, direcao_pct = _variacao(quote.change_pct, suffix="%")
    return {
        "ticker": _format_text(quote.ticker),
        "nome": _format_text(quote.name),
        "moeda": _format_text(quote.currency),
        "data": _format_date(quote.last_trading_date),
        "fechamento": _format_decimal(quote.close),
        "variacao": variacao,
        "variacao_pct": variacao_pct,
        "volume": _format_integer(quote.volume),
        "min52": _format_decimal(quote.week52_low),
        "max52": _format_decimal(quote.week52_high),
        "direcao": direcao,
        "direcao_pct": direcao_pct,
        "falha": False,
        "motivo": "",
    }


def _failure_row(error: "QuoteError") -> dict[str, Any]:
    """Linha de um ativo sem dados, mantendo o ticker na posição configurada.

    ``QuoteError`` carrega apenas ``ticker`` e ``reason``, então as nove colunas
    de mercado mostram ``n/d`` e o motivo sai sozinho em ``motivo``, normalizado
    como as demais células de texto mas nunca trocado pela sentinela: a coluna
    ``Estado`` dos dois templates compõe ``dados indisponíveis: <motivo>`` a
    partir de ``falha`` e ``motivo``.
    """
    return {
        "ticker": _format_text(error.ticker),
        "nome": NOT_AVAILABLE,
        "moeda": NOT_AVAILABLE,
        "data": NOT_AVAILABLE,
        "fechamento": NOT_AVAILABLE,
        "variacao": NOT_AVAILABLE,
        "variacao_pct": NOT_AVAILABLE,
        "volume": NOT_AVAILABLE,
        "min52": NOT_AVAILABLE,
        "max52": NOT_AVAILABLE,
        "direcao": NEUTRO,
        "direcao_pct": NEUTRO,
        "falha": True,
        "motivo": _normalize_text(error.reason),
    }


def _normalize_text(value: str) -> str:
    """Valor em uma linha física e sem o separador de colunas do corpo texto."""
    return _WHITESPACE.sub(" ", value).strip().replace("|", _PIPE_SUBSTITUTE)


def _format_text(value: str | None) -> str:
    """Texto em uma linha, livre do separador, ou ``n/d`` quando ausente ou vazio."""
    if value is None:
        return NOT_AVAILABLE
    return _normalize_text(value) or NOT_AVAILABLE


def _format_date(value: date | None) -> str:
    """Data do último pregão em ``AAAA-MM-DD``, ou ``n/d`` quando ausente."""
    if value is None:
        return NOT_AVAILABLE
    return value.strftime("%Y-%m-%d")


def _format_decimal(value: float | None) -> str:
    """Valor com duas casas e separadores pt-BR (``358,43``), ou ``n/d``."""
    if value is None or not math.isfinite(value):
        return NOT_AVAILABLE
    return _to_pt_br(f"{value:,.2f}")


def _variacao(value: float | None, suffix: str = "") -> tuple[str, str]:
    """Texto com sinal e direção da variação, decididos pelo mesmo valor impresso.

    Devolver o par ``(texto, direção)`` impede que a cor discorde do texto: a
    magnitude formatada é a única fonte do sinal, então uma célula pintada de
    verde ou vermelho sempre traz ``+`` ou ``-``. Um valor que arredonda para
    ``0,00`` sai sem sinal e ``neutro``, como um zero exato, porque o relatório
    nunca mostra ``+0,00``; um valor ausente ou não finito sai ``n/d`` e
    ``neutro``.
    """
    if value is None or not math.isfinite(value):
        return NOT_AVAILABLE, NEUTRO
    magnitude = _to_pt_br(f"{abs(value):,.2f}")
    if magnitude == _ZERO:
        return f"{magnitude}{suffix}", NEUTRO
    if value < 0:
        return f"-{magnitude}{suffix}", NEGATIVO
    return f"+{magnitude}{suffix}", POSITIVO


def _format_integer(value: int | None) -> str:
    """Inteiro com ponto de milhar (``18.843.459``), ou ``n/d``."""
    if value is None:
        return NOT_AVAILABLE
    return _to_pt_br(f"{int(value):,d}")


def _to_pt_br(number: str) -> str:
    """Troca os separadores de ``1,234.56`` (en-US) por ``1.234,56`` (pt-BR)."""
    return number.replace(",", _GROUP_MARK).replace(".", ",").replace(_GROUP_MARK, ".")


# Representação de zero na mesma formatação dos valores, usada para decidir o sinal.
_ZERO = _to_pt_br(f"{0:,.2f}")
