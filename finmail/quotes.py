"""Única porta para o Yahoo Finance.

Todo o contato com o ``yfinance`` vive aqui: nenhum outro módulo do pacote importa a
biblioteca, e nenhuma suposição sobre o formato da resposta (colunas, índices, fusos ou
metadados) sai deste arquivo. Os valores são convertidos para tipos primitivos na
fronteira, de modo que um ``DataFrame``, uma ``Series`` ou um ``Timestamp`` nunca
atravessa o contrato público.

Uma requisição de barras por ativo: o metadado do gráfico vem do acessor público do mesmo
instrumento, e nenhuma superfície privada da biblioteca é tocada.
"""

import logging
import math
import time
from dataclasses import dataclass
from datetime import date, datetime

import yfinance as yf
from yfinance.exceptions import (
    YFPricesMissingError,
    YFRateLimitError,
    YFTickerMissingError,
)

_LOGGER = logging.getLogger("finmail.quotes")

# Timeout por requisição; cotações sequenciais não podem travar uma execução agendada.
REQUEST_TIMEOUT = 10
# Total de tentativas (1 chamada + 2 novas tentativas) para falhas transitórias.
MAX_ATTEMPTS = 3
RETRY_WAIT_SECONDS = 2
HISTORY_PERIOD = "1y"
# Falha de verificação do certificado do servidor é permanente e não é repetida. A
# checagem é pelo nome da classe porque a biblioteca de transporte que o ``yfinance``
# usa reporta o problema com a própria ``CertificateVerifyError``, que não é uma
# ``ssl.SSLError`` — importá-la acrescentaria uma dependência que o projeto não declara.
_CERTIFICATE_ERROR_NAMES = frozenset({"CertificateVerifyError", "SSLCertVerificationError"})


@dataclass(frozen=True)
class Quote:
    """Cotação de fechamento de um ativo, já convertida em tipos primitivos.

    ``close`` é o único campo numérico obrigatório; os demais aceitam ``None`` quando o
    Yahoo Finance não os informa e são apresentados como ``n/d`` no relatório.
    """

    ticker: str
    name: str | None
    currency: str | None
    last_trading_date: date | None
    close: float
    change: float | None
    change_pct: float | None
    volume: int | None
    week52_low: float | None
    week52_high: float | None


class QuoteError(Exception):
    """Falha esperada na consulta de um único ativo.

    Não é um erro fatal: o relatório consome a instância levantada como a linha do ativo,
    exibindo ``dados indisponíveis: <motivo>``.
    """

    def __init__(self, ticker: str, reason: str) -> None:
        super().__init__(reason)
        self.ticker = ticker
        self.reason = reason

    def __str__(self) -> str:
        return self.reason


def fetch(ticker: str) -> Quote:
    """Consulta o fechamento diário de ``ticker`` no Yahoo Finance.

    Usa uma única requisição de barras diárias de um ano, da qual deriva todos os campos
    de :class:`Quote`. Falhas transitórias (rede, tempo esgotado e limite de requisições)
    são repetidas até ``MAX_ATTEMPTS`` tentativas, com ``RETRY_WAIT_SECONDS`` entre elas;
    falhas determinísticas (ativo inexistente, ausência de preços, quadro vazio, sem
    ``Close`` ou certificado TLS inválido) não são repetidas.

    Args:
        ticker: símbolo do ativo, como ``PETR4.SA`` ou ``AAPL``.

    Returns:
        A cotação do último pregão disponível.

    Raises:
        QuoteError: quando o ativo não pode ser cotado, com ``ticker`` e ``reason`` (texto
            curto em português) preenchidos. Nenhuma outra exceção escapa desta função
            para um problema de um único ativo.
    """
    try:
        instrument = yf.Ticker(ticker)
    except Exception as exc:
        raise QuoteError(ticker, "falha inesperada ao consultar o Yahoo Finance") from exc

    frame = _history_frame(instrument, ticker)
    try:
        return _build_quote(ticker, instrument, frame)
    except QuoteError:
        raise
    except Exception as exc:
        raise QuoteError(ticker, "falha inesperada ao consultar o Yahoo Finance") from exc


def _history_frame(instrument: "yf.Ticker", ticker: str) -> object:
    """Obtém as barras diárias de um ano, repetindo apenas as falhas transitórias."""
    attempt = 1
    while True:
        try:
            # auto_adjust=False mantém os preços fechados como reportados pelo Yahoo, de
            # modo que Close, Low e High sejam os valores de mercado; raise_errors=True
            # faz a biblioteca levantar em vez de devolver um quadro vazio e esconder a
            # causa real da falha.
            frame = instrument.history(
                period=HISTORY_PERIOD,
                interval="1d",
                auto_adjust=False,
                timeout=REQUEST_TIMEOUT,
                raise_errors=True,
            )
        except YFPricesMissingError as exc:
            # YFPricesMissingError herda de YFTickerMissingError e precisa vir primeiro.
            raise QuoteError(ticker, "sem preços disponíveis no período") from exc
        except YFTickerMissingError as exc:
            raise QuoteError(ticker, "ativo não encontrado no Yahoo Finance") from exc
        except (YFRateLimitError, OSError) as exc:
            if _is_certificate_failure(exc):
                # Certificado que não verifica não passa a verificar na segunda tentativa:
                # repetir custaria duas requisições e 4 s por ativo antes do mesmo desfecho.
                raise QuoteError(
                    ticker, "certificado TLS do servidor não pôde ser verificado"
                ) from exc
            if attempt >= MAX_ATTEMPTS:
                if isinstance(exc, YFRateLimitError):
                    raise QuoteError(
                        ticker,
                        f"limite de requisições do Yahoo Finance após "
                        f"{MAX_ATTEMPTS} tentativas",
                    ) from exc
                raise QuoteError(ticker, f"falha de rede após {MAX_ATTEMPTS} tentativas") from exc
            _LOGGER.debug(
                "Nova tentativa para %s em %d s (tentativa %d de %d) após %s.",
                ticker,
                RETRY_WAIT_SECONDS,
                attempt,
                MAX_ATTEMPTS,
                type(exc).__name__,
            )
            time.sleep(RETRY_WAIT_SECONDS)
            attempt += 1
        except Exception as exc:
            raise QuoteError(ticker, "falha inesperada ao consultar o Yahoo Finance") from exc
        else:
            break

    if getattr(frame, "empty", True):
        # Sem barras não há o que reportar, e repetir a consulta não mudaria o resultado.
        raise QuoteError(ticker, "sem dados no período")
    return frame


def _is_certificate_failure(exc: BaseException) -> bool:
    """Diz se a falha é de verificação do certificado TLS, que não se resolve repetindo."""
    return any(cls.__name__ in _CERTIFICATE_ERROR_NAMES for cls in type(exc).__mro__)


def _graph_metadata(instrument: "yf.Ticker", ticker: str) -> dict[str, object]:
    """Metadado do gráfico do ativo, lido pelo acessor público da biblioteca.

    ``get_history_metadata()`` do mesmo instrumento é a única fonte do metadado: é a
    superfície que a fronteira declara, e nenhum atributo privado da biblioteca é lido. A
    própria biblioteca pode emitir uma requisição de acompanhamento quando o metadado
    diário não traz o calendário de negociação. Uma falha no acesso não invalida o ativo —
    apenas deixa ``name`` e ``currency`` ausentes, porque só ``close`` é essencial —, mas a
    causa vai para o log em ``DEBUG`` antes da degradação, em vez de desaparecer.
    """
    try:
        metadata = instrument.get_history_metadata()
    except Exception as exc:
        _LOGGER.debug("Metadado do gráfico indisponível para %s: %s.", ticker, exc)
        return {}
    if not isinstance(metadata, dict):
        _LOGGER.debug(
            "Metadado do gráfico de %s veio como %s, não como dicionário.",
            ticker,
            type(metadata).__name__,
        )
        return {}
    return metadata


def _build_quote(ticker: str, instrument: "yf.Ticker", frame: object) -> Quote:
    """Converte as barras e o metadado da requisição em uma :class:`Quote` primitiva."""
    columns = getattr(frame, "columns", ())
    close_series = frame["Close"] if "Close" in columns else None
    close = _to_float(close_series.iloc[-1]) if close_series is not None else None
    if close is None:
        raise QuoteError(ticker, "campo essencial ausente: close")

    # O fechamento anterior é a penúltima barra da própria janela: as chaves
    # previousClose/chartPreviousClose do metadado divergem entre si e já foram reportadas
    # incorretas, então não são usadas.
    previous_close = _to_float(close_series.iloc[-2]) if len(close_series) >= 2 else None
    if previous_close is None or previous_close == 0:
        change = None
        change_pct = None
    else:
        change = close - previous_close
        change_pct = change / previous_close * 100.0

    volume = _to_int(frame["Volume"].iloc[-1]) if "Volume" in columns else None
    if volume is not None and volume < 0:
        # Volume negativo não é uma contagem válida: é sentinela de valor ausente, então a
        # fronteira entrega None e o relatório mostra n/d em vez de um número negativo.
        volume = None
    week52_low = _to_float(frame["Low"].min()) if "Low" in columns else None
    week52_high = _to_float(frame["High"].max()) if "High" in columns else None

    metadata = _graph_metadata(instrument, ticker)
    name = _to_text(metadata.get("longName")) or _to_text(metadata.get("shortName"))
    currency = _to_text(metadata.get("currency"))

    return Quote(
        ticker=ticker,
        name=name,
        currency=currency,
        last_trading_date=_last_index_date(frame),
        close=close,
        change=change,
        change_pct=change_pct,
        volume=volume,
        week52_low=week52_low,
        week52_high=week52_high,
    )


def _last_index_date(frame: object) -> date | None:
    """Data da última barra: o último pregão disponível, sem cálculo de calendário."""
    index = getattr(frame, "index", None)
    if index is None or len(index) == 0:
        return None
    last = index[-1]
    if isinstance(last, datetime):
        return last.date()
    if isinstance(last, date):
        return last
    return None


def _to_float(value: object) -> float | None:
    """Converte em ``float``; devolve ``None`` para ausente, vazio, não numérico ou NaN."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def _to_int(value: object) -> int | None:
    """Converte em ``int``; devolve ``None`` quando não há valor numérico utilizável."""
    number = _to_float(value)
    return None if number is None else int(number)


def _to_text(value: object) -> str | None:
    """Devolve o texto sem espaços nas pontas, ou ``None`` quando vazio ou de outro tipo."""
    if not isinstance(value, str):
        return None
    return value.strip() or None
