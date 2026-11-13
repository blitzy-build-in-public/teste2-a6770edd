"""Exceções do ``yfinance`` que ``finmail/quotes.py`` importa.

São definidas localmente, sem qualquer dependência, para que a suíte fim a fim rode offline sem
carregar a biblioteca real do Yahoo Finance.
"""


class YFRateLimitError(Exception):
    """Limite de requisições do Yahoo (HTTP 429); falha repetível."""


class YFPricesMissingError(Exception):
    """Sem barras de preço utilizáveis para o símbolo; erro determinístico, não repetido."""


class YFTickerMissingError(Exception):
    """O símbolo não resolve no Yahoo; erro determinístico, não repetido."""
