"""Exceções do ``yfinance`` que ``finmail/quotes.py`` importa.

São definidas localmente, sem qualquer dependência, para que a suíte fim a fim rode offline sem
carregar a biblioteca real do Yahoo Finance.
"""


class YFRateLimitError(Exception):
    """Limite de requisições do Yahoo (HTTP 429); falha repetível."""


class YFTickerMissingError(Exception):
    """O símbolo não resolve no Yahoo; erro determinístico, não repetido."""


# Herda de YFTickerMissingError como na biblioteca real: a ordem das cláusulas ``except`` de
# ``finmail/quotes.py`` depende dessa hierarquia.
class YFPricesMissingError(YFTickerMissingError):
    """Sem barras de preço utilizáveis para o símbolo; erro determinístico, não repetido."""
