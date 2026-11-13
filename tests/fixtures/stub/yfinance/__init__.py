"""Substituto offline do ``yfinance`` para as execuções fim a fim do finmail.

Este pacote é colocado à frente do ``site-packages`` no ``PYTHONPATH`` de cada subprocesso
de teste, sombreando a biblioteca real do Yahoo Finance, e oferece apenas a superfície que
``finmail/quotes.py`` usa, com dados fixos e determinísticos.
"""

import pandas as pd

from .exceptions import YFPricesMissingError, YFTickerMissingError

# Colunas na ordem que a biblioteca real devolve, com o tipo de cada uma.
_COLUMN_DTYPES: dict[str, str] = {
    "Open": "float64",
    "High": "float64",
    "Low": "float64",
    "Close": "float64",
    "Volume": "int64",
}

# (data, Open, High, Low, Close, Volume) — barras diárias fixas, sem qualquer requisição.
_Bar = tuple[str, float, float, float, float, int]

_BARS: dict[str, tuple[_Bar, ...]] = {
    "AAPL": (
        ("2026-11-10", 348.20, 352.10, 347.50, 350.10, 16200000),
        ("2026-11-11", 351.00, 356.00, 350.20, 354.88, 17450000),
        ("2026-11-12", 355.40, 359.37, 354.10, 358.43, 18843459),
    ),
    "PETR4.SA": (
        ("2026-11-10", 46.50, 46.90, 46.10, 46.20, 15120000),
        ("2026-11-11", 46.05, 46.30, 45.00, 45.19, 14980000),
        ("2026-11-12", 45.10, 45.40, 44.60, 44.70, 14474200),
    ),
    "MSFT": (
        ("2026-11-10", 501.20, 505.10, 500.40, 502.30, 19880000),
        ("2026-11-11", 502.60, 504.80, 501.10, 503.75, 20450000),
        ("2026-11-12", 503.40, 504.20, 502.90, 503.75, 20123456),
    ),
}

# MSFT não traz "longName" de propósito: é o caso de queda para "shortName" na fronteira.
_METADATA: dict[str, dict[str, str]] = {
    "AAPL": {"longName": "Apple Inc.", "shortName": "Apple Inc.", "currency": "USD"},
    "PETR4.SA": {
        "longName": "Petróleo Brasileiro S.A. - Petrobras",
        "shortName": "Petrobras",
        "currency": "BRL",
    },
    "MSFT": {"shortName": "Microsoft Corporation", "currency": "USD"},
}

# Símbolo que o Yahoo responde sem preços; os demais ausentes de _BARS não resolvem.
_NO_PRICES_SYMBOL = "BTC-USD"


class Ticker:
    """Instrumento simulado, com barras e metadado fixos e nenhum acesso à rede."""

    def __init__(self, symbol: str) -> None:
        """Guarda o símbolo recebido; nunca levanta e não faz I/O."""
        self.symbol = symbol

    def history(
        self,
        period: str = "1y",
        interval: str = "1d",
        auto_adjust: bool = False,
        timeout: int = 10,
        raise_errors: bool = True,
        **kwargs: object,
    ) -> pd.DataFrame:
        """Devolve as barras diárias simuladas do símbolo, sempre um quadro novo.

        ``period``, ``interval``, ``auto_adjust``, ``timeout`` e os demais argumentos são
        aceitos e ignorados: os dados são fixtures offline e nenhuma requisição sai daqui.
        Para símbolo sem dados, ``raise_errors`` decide entre levantar uma exceção
        determinística ou devolver um quadro vazio, como a biblioteca real faz.
        """
        bars = _BARS.get(self.symbol)
        if bars is None:
            if raise_errors:
                raise _failure(self.symbol)
            return _empty_frame()
        return _build_frame(bars)

    def get_history_metadata(self) -> dict[str, str]:
        """Devolve uma cópia nova do metadado fixo do símbolo simulado.

        Símbolo sem dados levanta a mesma exceção determinística de :meth:`history`.
        """
        metadata = _METADATA.get(self.symbol)
        if metadata is None:
            raise _failure(self.symbol)
        return dict(metadata)


def download(*args: object, **kwargs: object) -> None:
    """Sempre levanta ``RuntimeError``: ``yf.download`` não é usado por finmail.

    A biblioteca real engole a falha de cada símbolo nesse caminho, então uma chamada
    acidental esconderia a causa; levantar aqui transforma o defeito em diagnóstico claro
    em vez de um ``AttributeError`` ou de uma requisição de rede silenciosa.
    """
    raise RuntimeError("yf.download não é usado por finmail")


def _failure(symbol: str) -> Exception:
    """Falha determinística do símbolo: preços ausentes ou símbolo desconhecido."""
    if symbol == _NO_PRICES_SYMBOL:
        return YFPricesMissingError("no price data found, symbol may be delisted (period=1y)")
    return YFTickerMissingError(f"quote not found for symbol: {symbol}")


def _build_frame(bars: tuple[_Bar, ...]) -> pd.DataFrame:
    """Monta o quadro a partir das barras fixas a cada chamada.

    O quadro é reconstruído a cada chamada de propósito: um chamador que o altere não pode
    contaminar a próxima consulta, e nenhum datagrama mutável fica no nível do módulo.
    """
    index = pd.DatetimeIndex([bar[0] for bar in bars], name="Date")
    return pd.DataFrame(
        {
            "Open": [bar[1] for bar in bars],
            "High": [bar[2] for bar in bars],
            "Low": [bar[3] for bar in bars],
            "Close": [bar[4] for bar in bars],
            "Volume": [bar[5] for bar in bars],
        },
        index=index,
    )


def _empty_frame() -> pd.DataFrame:
    """Quadro vazio com as cinco colunas e um ``DatetimeIndex`` vazio chamado ``Date``."""
    index = pd.DatetimeIndex([], name="Date")
    # As séries precisam receber o índice: vazias e sem índice, o construtor do pandas as
    # descarta e o quadro sairia sem coluna alguma.
    data = {
        column: pd.Series(index=index, dtype=dtype) for column, dtype in _COLUMN_DTYPES.items()
    }
    return pd.DataFrame(data)
