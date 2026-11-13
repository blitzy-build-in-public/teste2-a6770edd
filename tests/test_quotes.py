"""Prova a fronteira com o Yahoo Finance: derivação dos campos e retentativa.

AAP 0.7.3 fixa a origem de cada campo de ``Quote`` e a política de retentativa; AAP 0.2.1
fixa a superfície da requisição — uma única chamada ``history`` por ativo, com
``raise_errors=True``, nunca ``yf.download`` e nunca ``Ticker.info``; AAP 0.5.2 fixa o
contrato de tipos, em que só ``close`` é obrigatório e nenhum tipo do ``pandas``
atravessa a fronteira do adaptador.

Nada aqui toca a rede nem espera de verdade: antes de cada ``fetch``, o módulo
``yfinance`` que o adaptador importa é substituído por um dublê local e a função de
espera entre tentativas é trocada por um registrador. Nenhuma costura foi acrescentada
ao produto — a substituição usa apenas o atributo de módulo ``quotes.yf`` e o ``time``
que o próprio adaptador chama.
"""

import ast
import dataclasses
import datetime
import math
import pathlib
from collections.abc import Callable
from types import SimpleNamespace

import pandas as pd
import pytest
from yfinance import exceptions as yf_exceptions
from yfinance.exceptions import YFPricesMissingError, YFRateLimitError

import finmail.quotes as quotes
from finmail.quotes import Quote, QuoteError, fetch

TICKER = "AAPL"
# Símbolo usado nas falhas: o mesmo que AAP 0.5.4 mostra na linha ``dados
# indisponíveis`` e que ``QuoteError`` precisa carregar até o relatório.
MISSING_TICKER = "BTC-USD"

# Pregões fixos: nenhum teste depende do relógio nem do calendário de mercado.
DEFAULT_DATES = ("2026-11-10", "2026-11-11", "2026-11-12")
LAST_TRADING_DATE = datetime.date(2026, 11, 12)

# Série de referência usada na derivação dos campos (AAP 0.7.3).
CLOSES = [10.0, 12.0, 13.0]
LOWS = [9.0, 11.0, 12.5]
HIGHS = [11.0, 12.5, 14.0]
VOLUMES = [100, 200, 300]

# Metadado do gráfico da mesma requisição: ``longName`` tem precedência.
DEFAULT_METADATA = {"longName": "Apple Inc.", "currency": "USD"}

# Espera entre tentativas: duas novas tentativas, 2 s cada (AAP 0.7.3).
RETRY_WAITS = [2, 2]
TOTAL_ATTEMPTS = 3

# Únicos tipos que podem sair do adaptador (AAP 0.5.1, 0.5.2).
PRIMITIVE_TYPES = {float, int, str, datetime.date, type(None)}


class RecordingTicker:
    """Dublê do ``yfinance.Ticker``: registra a requisição e segue um roteiro.

    O roteiro é uma lista de resultados: um ``BaseException`` é levantado, qualquer
    outro valor é devolvido como o quadro de barras. Com um único item na lista ele é
    reutilizado, de modo que "sempre falha" e "sempre devolve o mesmo quadro" não
    precisam de listas do tamanho das tentativas.
    """

    def __init__(
        self,
        symbol: str,
        outcomes: list[object],
        metadata: dict[str, object] | None = None,
        cached_metadata: dict[str, object] | None = None,
    ) -> None:
        self.symbol = symbol
        self.history_calls: list[dict[str, object]] = []
        self.metadata_calls = 0
        self.info_accessed = False
        self._outcomes = list(outcomes)
        self._metadata = dict(metadata or {})
        if cached_metadata is not None:
            # Superfície que o adaptador lê para não repetir a requisição do metadado:
            # o objeto de preços já carregado guarda o metadado do gráfico.
            cached = dict(cached_metadata)
            self._lazy_load_price_history = lambda: SimpleNamespace(  # noqa: E731
                _history_metadata=cached
            )

    @property
    def info(self) -> object:
        """Proibido pela AAP 0.2.1: é ~6,5x mais caro do lado do Yahoo."""
        self.info_accessed = True
        raise AssertionError("Ticker.info é proibido (AAP 0.2.1)")

    def history(self, **kwargs: object) -> object:
        """Registra os argumentos da chamada e devolve — ou levanta — o próximo item."""
        self.history_calls.append(dict(kwargs))
        outcome = self._outcomes[0] if len(self._outcomes) == 1 else self._outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def get_history_metadata(self) -> dict[str, object]:
        """Metadado do gráfico que acompanha as barras; conta as chamadas."""
        self.metadata_calls += 1
        return dict(self._metadata)


class FakeYf:
    """Dublê do módulo ``yfinance``: só a superfície que ``quotes.py`` usa."""

    def __init__(
        self,
        outcomes: list[object],
        metadata: dict[str, object] | None = DEFAULT_METADATA,
        cached_metadata: dict[str, object] | None = None,
    ) -> None:
        self._outcomes = list(outcomes)
        self._metadata = metadata
        self._cached_metadata = cached_metadata
        self.created: list[RecordingTicker] = []

    def Ticker(self, symbol: str) -> RecordingTicker:  # noqa: N802 - nome da biblioteca
        """Cria o instrumento dublê e guarda a instância para as asserções."""
        instrument = RecordingTicker(
            symbol,
            self._outcomes,
            metadata=self._metadata,
            cached_metadata=self._cached_metadata,
        )
        self.created.append(instrument)
        return instrument

    def download(self, *args: object, **kwargs: object) -> object:
        """Proibido pela AAP 0.2.1: engole falhas por símbolo."""
        raise AssertionError("yf.download é proibido (AAP 0.2.1)")

    @property
    def history_calls(self) -> list[dict[str, object]]:
        """Todas as chamadas de ``history`` feitas por qualquer instrumento criado."""
        return [call for instrument in self.created for call in instrument.history_calls]


def frame(
    closes: list[float],
    *,
    highs: list[float] | None = None,
    lows: list[float] | None = None,
    volumes: list[float] | None = None,
    dates: tuple[str, ...] = DEFAULT_DATES,
    tz: str | None = None,
) -> pd.DataFrame:
    """Quadro de barras com a forma da resposta real: índice de datas e cinco colunas.

    ``Open``, ``High`` e ``Low`` recebem ``closes`` quando não são informados e o
    ``Volume`` padrão é constante. Se ``dates`` não tiver uma data por barra — o caso de
    uma série vazia ou de uma única barra —, as datas são fixas e terminam no último
    pregão de referência. É o ``pandas`` real do ambiente (dependência de runtime
    declarada), e não um dublê: o adaptador precisa converter tipos de verdade.
    """
    closes = list(closes)
    highs = closes if highs is None else list(highs)
    lows = closes if lows is None else list(lows)
    volumes = [100.0] * len(closes) if volumes is None else list(volumes)
    if len(dates) != len(closes):
        dates = tuple(
            (LAST_TRADING_DATE - datetime.timedelta(days=offset)).isoformat()
            for offset in range(len(closes) - 1, -1, -1)
        )
    index = pd.DatetimeIndex([pd.Timestamp(moment) for moment in dates])
    if tz is not None:
        index = index.tz_localize(tz)
    return pd.DataFrame(
        {
            "Open": closes,
            "High": highs,
            "Low": lows,
            "Close": closes,
            "Volume": volumes,
        },
        index=index,
    )


def install_fake_yf(
    monkeypatch: pytest.MonkeyPatch,
    outcomes: list[object],
    metadata: dict[str, object] | None = DEFAULT_METADATA,
    cached_metadata: dict[str, object] | None = None,
) -> FakeYf:
    """Substitui o ``yfinance`` do adaptador e devolve o dublê instalado."""
    fake = FakeYf(outcomes, metadata=metadata, cached_metadata=cached_metadata)
    monkeypatch.setattr(quotes, "yf", fake)
    return fake


def patch_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Registra as esperas entre tentativas sem dormir de verdade.

    O ``sleep`` substituído é o que o adaptador alcança — ``quotes.time.sleep``. O
    ``monkeypatch`` restaura o original ao fim do teste, então nenhuma espera de 2 s
    chega a acontecer.
    """
    waits: list[float] = []
    monkeypatch.setattr(quotes.time, "sleep", waits.append)
    return waits


def fetch_quote(
    monkeypatch: pytest.MonkeyPatch,
    outcomes: list[object],
    metadata: dict[str, object] | None = DEFAULT_METADATA,
    cached_metadata: dict[str, object] | None = None,
) -> Quote:
    """Instala o dublê, neutraliza a espera e devolve a cotação de ``TICKER``."""
    fake = install_fake_yf(
        monkeypatch,
        outcomes,
        metadata=metadata,
        cached_metadata=cached_metadata,
    )
    patch_sleep(monkeypatch)
    quote = fetch(TICKER)
    # ``Ticker.info`` é vetado em todo caminho: o adaptador tem de viver da requisição
    # de barras e do metadado do gráfico que veio com ela.
    assert not any(instrument.info_accessed for instrument in fake.created)
    return quote


def forced_reason(monkeypatch: pytest.MonkeyPatch, outcomes: list[object]) -> str:
    """Força uma falha com o roteiro dado e devolve o motivo exibido ao operador."""
    install_fake_yf(monkeypatch, outcomes, metadata=DEFAULT_METADATA)
    patch_sleep(monkeypatch)
    with pytest.raises(QuoteError) as excinfo:
        fetch(MISSING_TICKER)
    return str(excinfo.value)


def assert_single_attempt(fake: FakeYf, waits: list[float]) -> None:
    """Falha determinística (ou de formato) custa uma chamada e nenhuma espera."""
    assert len(fake.created) == 1
    assert len(fake.history_calls) == 1
    assert waits == []


# --- Derivação dos campos a partir da série de barras (AAP 0.7.3) -----------------


def test_deriva_os_dez_campos_da_janela(monkeypatch: pytest.MonkeyPatch) -> None:
    """Todos os campos vêm da janela de um ano: fechamento, variação, faixa e metadado."""
    bars = frame(CLOSES, lows=LOWS, highs=HIGHS, volumes=VOLUMES)
    quote = fetch_quote(monkeypatch, [bars])

    assert quote.ticker == TICKER
    assert quote.name == "Apple Inc."
    assert quote.currency == "USD"
    assert quote.last_trading_date == LAST_TRADING_DATE
    # Fechamento é a última barra da janela, e a variação é contra a penúltima.
    assert quote.close == 13.0
    assert quote.change == pytest.approx(1.0)
    assert quote.change_pct == pytest.approx(1.0 / 12.0 * 100.0)
    assert quote.volume == 300
    # Faixa de 52 semanas: mínimo de Low e máximo de High da janela inteira.
    assert quote.week52_low == pytest.approx(9.0)
    assert quote.week52_high == pytest.approx(14.0)


def test_tipos_primitivos_na_fronteira(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cada campo chega como tipo primário: nem numpy, nem pandas, nem datetime."""
    bars = frame(CLOSES, lows=LOWS, highs=HIGHS, volumes=VOLUMES)
    quote = fetch_quote(monkeypatch, [bars])

    assert type(quote.close) is float
    assert type(quote.change) is float
    assert type(quote.change_pct) is float
    assert type(quote.volume) is int
    assert type(quote.week52_low) is float
    assert type(quote.week52_high) is float
    # ``pandas.Timestamp`` é subclasse de ``datetime``: só a data do pregão pode sair.
    assert type(quote.last_trading_date) is datetime.date
    assert not isinstance(quote.last_trading_date, datetime.datetime)


def test_indice_com_fuso_entrega_data_do_pregao(monkeypatch: pytest.MonkeyPatch) -> None:
    """A data é a do calendário da bolsa, não a de uma conversão para UTC.

    A última barra está datada às 20:00 em ``America/New_York``, que em UTC já é o dia
    seguinte: um adaptador que convertesse o fuso entregaria 2026-11-13.
    """
    dates = ("2026-11-11 20:00", "2026-11-12 20:00")
    bars = frame([10.0, 13.0], dates=dates, tz="America/New_York")
    quote = fetch_quote(monkeypatch, [bars])

    assert quote.last_trading_date == LAST_TRADING_DATE
    assert quote.close == 13.0


def test_faixa_52_semanas_vem_da_janela(monkeypatch: pytest.MonkeyPatch) -> None:
    """Máxima na primeira barra e mínima na última: só a janela inteira acerta."""
    bars = frame(
        [10.0, 12.0, 13.0],
        highs=[20.0, 12.5, 14.0],
        lows=[9.5, 11.0, 8.0],
    )
    quote = fetch_quote(monkeypatch, [bars])

    assert quote.week52_high == pytest.approx(20.0)
    assert quote.week52_low == pytest.approx(8.0)


# --- Metadado e campos opcionais (AAP 0.5.2, 0.7.3) ------------------------------


def test_nome_prefere_long_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """``longName`` vence ``shortName`` quando o Yahoo informa os dois."""
    metadata = {"longName": "Apple Inc.", "shortName": "Apple", "currency": "USD"}
    quote = fetch_quote(monkeypatch, [frame(CLOSES)], metadata=metadata)

    assert quote.name == "Apple Inc."


def test_nome_cai_para_short_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sem ``longName`` — listagens fora dos EUA, por exemplo — vale ``shortName``."""
    metadata = {"shortName": "Apple", "currency": "USD"}
    quote = fetch_quote(monkeypatch, [frame(CLOSES)], metadata=metadata)

    assert quote.name == "Apple"
    assert quote.currency == "USD"


def test_metadado_ausente_nao_invalida_o_ativo(monkeypatch: pytest.MonkeyPatch) -> None:
    """Só ``close`` é essencial: sem metadado a cotação continua válida, com n/d."""
    quote = fetch_quote(monkeypatch, [frame(CLOSES)], metadata={})

    assert quote.name is None
    assert quote.currency is None
    assert quote.close == 13.0
    assert quote.last_trading_date == LAST_TRADING_DATE


def test_serie_de_uma_barra_nao_tem_variacao(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sem pregão anterior não há variação, e a linha continua no relatório."""
    bars = frame([13.0], volumes=[300])
    quote = fetch_quote(monkeypatch, [bars])

    assert quote.close == 13.0
    assert quote.volume == 300
    assert quote.change is None
    assert quote.change_pct is None
    assert quote.last_trading_date == LAST_TRADING_DATE


def test_preco_anterior_zero_nao_inventa_variacao(monkeypatch: pytest.MonkeyPatch) -> None:
    """Preço anterior zerado não gera divisão por zero nem variação fabricada."""
    quote = fetch_quote(monkeypatch, [frame([0.0, 5.0])])

    assert quote.close == 5.0
    assert quote.change is None
    assert quote.change_pct is None


def test_volume_nan_vira_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """``NaN`` é valor ausente: vira ``None``, nunca ``nan`` no relatório."""
    bars = frame(CLOSES, volumes=[100.0, 200.0, float("nan")])
    quote = fetch_quote(monkeypatch, [bars])

    assert quote.volume is None
    assert quote.close == 13.0


def test_high_nan_isolado_nao_contamina_a_maxima(monkeypatch: pytest.MonkeyPatch) -> None:
    """Uma barra sem ``High`` não apaga a faixa das demais.

    AAP 0.7.3 manda devolver ``None`` apenas para o campo opcional ausente ou ``NaN``:
    o máximo da janela é calculado sobre as barras que têm valor (o ``max`` do
    ``pandas`` ignora ``NaN``), então aqui a máxima continua sendo 12,5 e é finita.
    """
    bars = frame(CLOSES, highs=[11.0, 12.5, float("nan")])
    quote = fetch_quote(monkeypatch, [bars])

    assert quote.week52_high == pytest.approx(12.5)
    assert math.isfinite(quote.week52_high)


def test_faixa_52_semanas_sem_nenhum_valor_vira_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """Janela inteira sem ``High``/``Low``: a faixa é ``None``, nunca ``nan``."""
    bars = frame(
        CLOSES,
        highs=[float("nan")] * len(CLOSES),
        lows=[float("nan")] * len(CLOSES),
    )
    quote = fetch_quote(monkeypatch, [bars])

    assert quote.week52_low is None
    assert quote.week52_high is None
    assert quote.close == 13.0


# --- Superfície da requisição (AAP 0.2.1, 0.5.1, 0.8) ----------------------------


def test_uma_requisicao_por_ativo(monkeypatch: pytest.MonkeyPatch) -> None:
    """Um ``Ticker``, uma chamada de ``history`` e um metadado: orçamento de 120 s."""
    fake = install_fake_yf(monkeypatch, [frame(CLOSES)])
    patch_sleep(monkeypatch)

    fetch(TICKER)

    assert len(fake.created) == 1
    instrument = fake.created[0]
    assert instrument.symbol == TICKER
    assert len(instrument.history_calls) == 1
    # O metadado vem do mesmo instrumento, ou seja, da requisição já feita.
    assert instrument.metadata_calls == 1
    # ``Ticker.info`` custaria outra requisição e não pode ser tocado.
    assert instrument.info_accessed is False


def test_duas_consultas_criam_dois_instrumentos(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cada ``fetch`` constrói o seu instrumento: nada é compartilhado entre ativos."""
    fake = install_fake_yf(monkeypatch, [frame(CLOSES)])
    patch_sleep(monkeypatch)

    quotes_fetched = [fetch(TICKER), fetch("PETR4.SA")]

    assert len(fake.created) == 2
    assert [instrument.symbol for instrument in fake.created] == [TICKER, "PETR4.SA"]
    assert [len(instrument.history_calls) for instrument in fake.created] == [1, 1]
    # O ticker volta na cotação como veio da configuração: é a chave da linha no
    # relatório.
    assert [quote.ticker for quote in quotes_fetched] == [TICKER, "PETR4.SA"]


def test_parametros_da_requisicao_sao_os_documentados(monkeypatch: pytest.MonkeyPatch) -> None:
    """A janela de um ano é pedida exatamente como AAP 0.5.1 fixa.

    ``raise_errors=True`` é o que faz a biblioteca levantar em vez de devolver um
    quadro vazio e esconder a causa (AAP 0.2.1); ``auto_adjust=False`` mantém os
    preços como o Yahoo os reporta.
    """
    fake = install_fake_yf(monkeypatch, [frame(CLOSES)])
    patch_sleep(monkeypatch)

    fetch(TICKER)

    assert fake.history_calls == [
        {
            "period": "1y",
            "interval": "1d",
            "auto_adjust": False,
            "timeout": 10,
            "raise_errors": True,
        }
    ]


def test_metadado_ja_carregado_dispensa_segunda_requisicao(monkeypatch: pytest.MonkeyPatch) -> None:
    """O metadado que a própria consulta trouxe é usado antes de pedir de novo."""
    cached = {"longName": "Petróleo Brasileiro S.A. - Petrobras", "currency": "BRL"}
    fake = install_fake_yf(monkeypatch, [frame(CLOSES)], metadata={}, cached_metadata=cached)
    patch_sleep(monkeypatch)

    quote = fetch(TICKER)

    assert quote.name == "Petróleo Brasileiro S.A. - Petrobras"
    assert quote.currency == "BRL"
    assert len(fake.history_calls) == 1
    assert fake.created[0].metadata_calls == 0


# --- Política de retentativa: 3 tentativas, 2 s entre elas (AAP 0.7.3) -----------


@pytest.mark.parametrize("error_class", [ConnectionError, TimeoutError])
def test_falha_transitoria_esgota_tres_tentativas(
    monkeypatch: pytest.MonkeyPatch,
    error_class: type[OSError],
) -> None:
    """Rede fora e tempo esgotado são repetidos até 3 tentativas, com 2 s de espera."""
    fake = install_fake_yf(monkeypatch, [error_class("falha simulada")])
    waits = patch_sleep(monkeypatch)

    with pytest.raises(QuoteError) as excinfo:
        fetch(TICKER)

    assert len(fake.created) == 1
    assert len(fake.history_calls) == TOTAL_ATTEMPTS
    assert waits == RETRY_WAITS
    assert excinfo.value.ticker == TICKER
    # Mesmo texto que o relatório exibe na coluna Estado: ``falha de rede após 3
    # tentativas``.
    assert "rede" in excinfo.value.reason
    assert "3" in excinfo.value.reason


def test_limite_de_requisicoes_tambem_e_repetido(monkeypatch: pytest.MonkeyPatch) -> None:
    """``YFRateLimitError`` é o único erro que a biblioteca levanta sempre: é transitório."""
    fake = install_fake_yf(monkeypatch, [YFRateLimitError()])
    waits = patch_sleep(monkeypatch)

    with pytest.raises(QuoteError) as excinfo:
        fetch(TICKER)

    assert len(fake.history_calls) == TOTAL_ATTEMPTS
    assert waits == RETRY_WAITS
    assert excinfo.value.ticker == TICKER
    assert "3" in excinfo.value.reason


def test_sucesso_na_terceira_tentativa_devolve_a_cotacao(monkeypatch: pytest.MonkeyPatch) -> None:
    """Duas falhas transitórias não perdem o ativo: a terceira tentativa entrega tudo."""
    bars = frame(CLOSES, lows=LOWS, highs=HIGHS, volumes=VOLUMES)
    fake = install_fake_yf(
        monkeypatch,
        [ConnectionError("rede fora"), ConnectionError("rede fora"), bars],
    )
    waits = patch_sleep(monkeypatch)

    quote = fetch(TICKER)

    assert len(fake.created) == 1
    assert len(fake.created[0].history_calls) == TOTAL_ATTEMPTS
    assert waits == RETRY_WAITS
    assert quote.close == 13.0
    assert quote.change == pytest.approx(1.0)
    assert quote.week52_low == pytest.approx(9.0)
    assert quote.week52_high == pytest.approx(14.0)


def test_preco_ausente_nao_e_repetido(monkeypatch: pytest.MonkeyPatch) -> None:
    """Erro determinístico de dados não ganha nova tentativa: só gastaria requisição."""
    fake = install_fake_yf(monkeypatch, [YFPricesMissingError(TICKER, "no prices")])
    waits = patch_sleep(monkeypatch)

    with pytest.raises(QuoteError) as excinfo:
        fetch(TICKER)

    assert_single_attempt(fake, waits)
    assert excinfo.value.ticker == TICKER
    assert excinfo.value.reason


@pytest.mark.skipif(
    not hasattr(yf_exceptions, "YFTickerMissingError"),
    reason="a versão instalada do yfinance não expõe YFTickerMissingError",
)
def test_ativo_inexistente_nao_e_repetido(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ativo que o Yahoo não conhece é determinístico: uma chamada, nenhuma espera."""
    missing_ticker_error = yf_exceptions.YFTickerMissingError
    fake = install_fake_yf(monkeypatch, [missing_ticker_error(TICKER, "delisted")])
    waits = patch_sleep(monkeypatch)

    with pytest.raises(QuoteError) as excinfo:
        fetch(TICKER)

    assert_single_attempt(fake, waits)
    assert excinfo.value.reason


def test_quadro_vazio_nao_e_repetido(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sem barras não há o que reportar, e repetir devolveria o mesmo quadro vazio."""
    fake = install_fake_yf(monkeypatch, [frame([])])
    waits = patch_sleep(monkeypatch)

    with pytest.raises(QuoteError) as excinfo:
        fetch(TICKER)

    assert_single_attempt(fake, waits)
    assert excinfo.value.reason


def test_quadro_sem_close_nao_e_repetido(monkeypatch: pytest.MonkeyPatch) -> None:
    """Coluna essencial ausente é falha de formato: uma chamada, nenhuma espera."""
    fake = install_fake_yf(monkeypatch, [frame(CLOSES).drop(columns=["Close"])])
    waits = patch_sleep(monkeypatch)

    with pytest.raises(QuoteError) as excinfo:
        fetch(TICKER)

    assert_single_attempt(fake, waits)
    assert "campo essencial ausente" in excinfo.value.reason


def test_close_nan_e_falha_com_motivo_proprio(monkeypatch: pytest.MonkeyPatch) -> None:
    """``close`` é obrigatório: sem ele a linha falha, em vez de inventar um valor."""
    bars = frame([10.0, 12.0, float("nan")])
    fake = install_fake_yf(monkeypatch, [bars])
    waits = patch_sleep(monkeypatch)

    with pytest.raises(QuoteError) as excinfo:
        fetch(TICKER)

    assert_single_attempt(fake, waits)
    assert "campo essencial ausente" in excinfo.value.reason
    assert "close" in excinfo.value.reason


# --- O objeto de erro que o relatório consome (AAP 0.5.2, 0.5.4, 0.8) ------------


def test_erro_carrega_ticker_e_motivo(monkeypatch: pytest.MonkeyPatch) -> None:
    """A linha do relatório precisa do ticker e do motivo, sem nova consulta."""
    install_fake_yf(monkeypatch, [YFRateLimitError()])
    patch_sleep(monkeypatch)

    with pytest.raises(QuoteError) as excinfo:
        fetch(MISSING_TICKER)

    error = excinfo.value
    assert error.ticker == MISSING_TICKER
    assert isinstance(error.reason, str)
    assert error.reason
    # ``str`` do erro é o motivo: é o que a coluna Estado exibe.
    assert str(error) == error.reason


@pytest.mark.parametrize(
    "outcomes_factory",
    [
        pytest.param(lambda: [ConnectionError("rede fora")], id="falha-de-rede"),
        pytest.param(lambda: [YFRateLimitError()], id="limite-de-requisicoes"),
        pytest.param(
            lambda: [YFPricesMissingError(MISSING_TICKER, "no prices")],
            id="sem-precos",
        ),
        pytest.param(lambda: [frame([])], id="quadro-vazio"),
        pytest.param(
            lambda: [frame(CLOSES).drop(columns=["Close"])],
            id="sem-close",
        ),
    ],
)
def test_motivo_e_uma_unica_linha_curta(
    monkeypatch: pytest.MonkeyPatch,
    outcomes_factory: Callable[[], list[object]],
) -> None:
    """O motivo é renderizado dentro de uma linha separada por `` | ``: sem quebra."""
    reason = forced_reason(monkeypatch, outcomes_factory())

    assert "\n" not in reason
    assert "\r" not in reason
    assert 0 < len(reason) <= 80


# --- Nenhum tipo do pandas atravessa a fronteira (AAP 0.5.1, 0.5.2) --------------


def test_campos_de_quote_seguem_o_contrato() -> None:
    """Os dez campos, na ordem de AAP 0.5.2, dos quais só ``close`` é obrigatório."""
    assert [field.name for field in dataclasses.fields(Quote)] == [
        "ticker",
        "name",
        "currency",
        "last_trading_date",
        "close",
        "change",
        "change_pct",
        "volume",
        "week52_low",
        "week52_high",
    ]


def test_nenhum_tipo_do_pandas_escapa(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cada valor é ``float``, ``int``, ``str``, ``date`` ou ``None`` — nada mais.

    Este é o teste que proíbe um ``numpy.float64``, um ``pandas.Timestamp`` ou um
    ``DataFrame`` de sair do adaptador: a comparação é pelo tipo exato.
    """
    bars = frame(CLOSES, lows=LOWS, highs=HIGHS, volumes=VOLUMES)
    quote = fetch_quote(monkeypatch, [bars])

    for field in dataclasses.fields(Quote):
        value = getattr(quote, field.name)
        assert type(value) in PRIMITIVE_TYPES, (field.name, type(value))


# --- A fronteira: só ``quotes.py`` importa o yfinance (AAP 0.7.3) ----------------


def _imported_modules(module_path: pathlib.Path) -> list[str]:
    """Nomes de módulo importados pelo arquivo, lidos da árvore sintática.

    A leitura sintática evita o falso positivo de uma docstring ou de uma tupla de
    loggers que apenas citem ``yfinance`` como texto.
    """
    tree = ast.parse(module_path.read_text(encoding="utf-8"), filename=str(module_path))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.append(node.module)
    return names


def _imports_yfinance(module_path: pathlib.Path) -> bool:
    """Diz se o módulo importa ``yfinance`` ou um submódulo dele."""
    return any(
        name == "yfinance" or name.startswith("yfinance.")
        for name in _imported_modules(module_path)
    )


def test_somente_quotes_importa_o_yfinance() -> None:
    """A superfície volátil fica confinada: nenhum outro módulo importa a biblioteca."""
    # O diretório do módulo sob teste é o diretório do pacote: localizar a varredura
    # pelo ``__file__`` do próprio adaptador não depende de como o pacote foi importado.
    package_dir = pathlib.Path(quotes.__file__).parent
    modules = sorted(package_dir.glob("*.py"))

    offenders = [
        module_path.name
        for module_path in modules
        if module_path.name != "quotes.py" and _imports_yfinance(module_path)
    ]

    assert offenders == []
    # A varredura não é vazia: o próprio adaptador importa a biblioteca.
    assert _imports_yfinance(package_dir / "quotes.py")
