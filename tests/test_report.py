"""Prova o assunto e os dois corpos do resumo de mercado (AAP 0.5.1, 0.5.2, 0.5.4).

``report.build`` é puro: recebe a lista ordenada de ``Quote``/``QuoteError`` e o
instante **já no fuso configurado**, e devolve ``Report(subject, text, html)``.
Cada teste injeta ``now`` explicitamente — nada aqui depende de relógio, rede,
credencial ou ambiente.

O que este arquivo fixa, caractere por caractere: assunto ``Resumo de mercado –
AAAA-MM-DD`` com travessão EN (U+2013), uma linha por ativo na ordem da
configuração, formatação pt-BR (vírgula decimal, ponto de milhar, sentinela
``n/d``), sinal explícito na variação, linha de falha com ``dados indisponíveis:
<motivo>``, classes ``positivo``/``negativo``/``neutro``/``falha`` no HTML, escape
de HTML apenas no HTML e rodapé fixo nos dois corpos.
"""

import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from finmail.quotes import Quote, QuoteError
from finmail.report import Report, build

SP = ZoneInfo("America/Sao_Paulo")
# Instante da execução já no fuso configurado, como ``cli.handle_run`` o entrega.
NOW = datetime(2026, 11, 13, 9, 30, tzinfo=SP)

# Separador literal dos campos do corpo texto e sentinela de valor ausente.
SEPARATOR = " | "
ND = "n/d"

HEADER = (
    "Ativo | Nome | Moeda | Data | Fechamento | Variação | Variação % | Volume | "
    "Mín. 52s | Máx. 52s | Estado"
)
FOOTER = "Dados fornecidos pelo Yahoo Finance. Os valores podem apresentar atraso."
HTML_HEADERS = (
    "Ticker",
    "Nome",
    "Moeda",
    "Data do último pregão",
    "Fechamento",
    "Variação",
    "Variação %",
    "Volume",
    "Mín. 52s",
    "Máx. 52s",
    "Estado",
)

_ROW_TAG = re.compile(r"<tr[^>]*>")
_CELL_TAG = re.compile(r"<td[^>]*>")
_CELL = re.compile(r"<td[^>]*>(.*?)</td>", re.DOTALL)
# Bloco ``<style>`` embutido e as regras de classe com a cor que cada uma declara.
_STYLE = re.compile(r"<style>(.*?)</style>", re.DOTALL)
_CSS_RULE = re.compile(r"\.(?P<classe>[\w-]+)\s*\{(?P<declaracoes>[^}]*)\}")
_CSS_COLOR = re.compile(r"color\s*:\s*(?P<cor>#[0-9a-fA-F]{3,8})")


def good(ticker: str = "AAPL", **overrides: object) -> Quote:
    """Cotação completa e realista; qualquer campo pode ser trocado pelo teste."""
    fields: dict[str, object] = {
        "name": "Apple Inc.",
        "currency": "USD",
        "last_trading_date": date(2026, 11, 12),
        "close": 358.43,
        "change": 3.55,
        "change_pct": 1.0,
        "volume": 18843459,
        "week52_low": 243.42,
        "week52_high": 359.37,
    }
    fields.update(overrides)
    return Quote(ticker=ticker, **fields)


def missing(ticker: str = "AAPL") -> Quote:
    """Cotação com todos os campos opcionais ausentes e apenas ``close`` preenchido."""
    return good(
        ticker,
        name=None,
        currency=None,
        last_trading_date=None,
        change=None,
        change_pct=None,
        volume=None,
        week52_low=None,
        week52_high=None,
    )


def quote_aapl() -> Quote:
    """Ativo dos Estados Unidos com todos os campos preenchidos."""
    return good("AAPL")


def quote_petr4() -> Quote:
    """Ativo brasileiro com preço, volume e variação negativa realistas."""
    return good(
        "PETR4.SA",
        name="Petróleo Brasileiro S.A. - Petrobras",
        currency="BRL",
        close=45.19,
        change=-0.49,
        change_pct=-1.1,
        volume=14474200,
        week52_low=29.53,
        week52_high=59.79,
    )


def failure_btc() -> QuoteError:
    """Ativo que falhou, com o motivo que o relatório exibe verbatim."""
    return QuoteError("BTC-USD", "falha de rede após 3 tentativas")


def ordered_entries() -> list[Quote | QuoteError]:
    """Entradas na ordem da configuração: dois ativos cotados e um que falhou."""
    return [quote_aapl(), quote_petr4(), failure_btc()]


def failing_entries() -> list[Quote | QuoteError]:
    """As mesmas três entradas, com a falha no meio, para provar a posição da linha."""
    return [quote_aapl(), failure_btc(), quote_petr4()]


def variation_entries() -> list[Quote | QuoteError]:
    """Uma entrada por estado de variação, na ordem: positivo, negativo, os zeros e a falha."""
    return [
        good("ALTA.SA", change=3.55, change_pct=1.0),
        good("BAIXA.SA", change=-0.49, change_pct=-1.1),
        good("ZERO.SA", change=0.0, change_pct=0.0),
        good("ARREDONDA_ALTA.SA", change=0.001, change_pct=0.001),
        good("ARREDONDA_BAIXA.SA", change=-0.001, change_pct=-0.001),
        good("SEM_VARIACAO.SA", change=None, change_pct=None),
        failure_btc(),
    ]


def asset_lines(report: Report) -> list[str]:
    """As linhas de ativo do corpo texto: entre o cabeçalho e o rodapé."""
    return report.text.splitlines()[2:-1]


def asset_fields(report: Report) -> list[list[str]]:
    """Cada linha de ativo dividida nos onze campos do separador literal."""
    return [line.split(SEPARATOR) for line in asset_lines(report)]


def row_tags(html: str) -> list[str]:
    """As tags ``<tr>`` na ordem do documento, sem depender da ordem dos atributos."""
    return _ROW_TAG.findall(html)


def row_cells(html: str, index: int) -> list[str]:
    """Os ``<td>`` da linha ``index`` do documento; ``0`` é a linha do cabeçalho."""
    return [cell.strip() for cell in _CELL.findall(html.split("</tr>")[index])]


def cell_tags(html: str, index: int) -> list[str]:
    """As tags de abertura ``<td>`` das células da linha ``index``, sem os valores."""
    return _CELL_TAG.findall(html.split("</tr>")[index])


def declared_colors(html: str) -> dict[str, str]:
    """A cor que cada classe declara no ``<style>`` embutido, em minúsculas.

    O bloco é lido como texto e as declarações são procuradas por classe, de modo
    que a cor verificada é a que o cliente de e-mail realmente aplica — não a
    presença do nome da classe em algum lugar do documento.
    """
    style = _STYLE.search(html)
    assert style is not None, "o HTML precisa trazer um bloco <style> embutido"
    colors: dict[str, str] = {}
    for rule in _CSS_RULE.finditer(style.group(1)):
        color = _CSS_COLOR.search(rule.group("declaracoes"))
        if color is not None:
            colors[rule.group("classe")] = color.group("cor").lower()
    return colors


# --- Assunto (AAP 0.5.4) -------------------------------------------------------


def test_assunto_exato_com_travessao_en_e_data_do_instante_injetado() -> None:
    subject = build([good()], NOW).subject

    assert subject == "Resumo de mercado \u2013 2026-11-13"
    assert "\u2013" in subject
    assert " - " not in subject


def test_data_do_assunto_segue_o_calendario_do_fuso_recebido() -> None:
    # 23:55 em São Paulo (UTC-3) já é o dia 13 em UTC: uma conversão interna de fuso
    # mostraria "2026-11-13", portanto este instante prova que build usa o calendário
    # do ``now`` recebido e não converte nada.
    late_night = datetime(2026, 11, 12, 23, 55, tzinfo=SP)

    assert build([good()], late_night).subject == "Resumo de mercado \u2013 2026-11-12"


def test_assunto_abre_o_corpo_texto_e_aparece_no_titulo_do_html() -> None:
    report = build(ordered_entries(), NOW)

    assert report.text.splitlines()[0] == report.subject
    assert f"<title>{report.subject}</title>" in report.html


# --- Corpo texto: estrutura (AAP 0.5.4, F9) ------------------------------------


def test_corpo_texto_tem_assunto_cabecalho_uma_linha_por_ativo_e_rodape() -> None:
    report = build(ordered_entries(), NOW)
    lines = report.text.splitlines()

    assert lines[0] == report.subject
    assert lines[1] == HEADER
    assert len(lines) == 2 + 3 + 1
    assert lines[-1] == FOOTER


def test_uma_linha_por_ativo_na_ordem_da_configuracao() -> None:
    report = build(ordered_entries(), NOW)

    assert [fields[0] for fields in asset_fields(report)] == ["AAPL", "PETR4.SA", "BTC-USD"]


def test_cada_linha_de_ativo_tem_exatamente_onze_campos() -> None:
    report = build(ordered_entries(), NOW)

    for line in asset_lines(report):
        assert len(line.split(SEPARATOR)) == 11
        assert not line.startswith(SEPARATOR)
        assert not line.endswith(SEPARATOR)


def test_corpo_texto_termina_com_uma_unica_quebra_de_linha() -> None:
    text = build(ordered_entries(), NOW).text

    assert text.endswith("\n")
    assert not text.endswith("\n\n")


def test_rodape_aparece_uma_unica_vez_no_corpo_texto() -> None:
    report = build(ordered_entries(), NOW)

    assert report.text.count(FOOTER) == 1


# --- Formatação pt-BR e sinais (AAP 0.5.4, AAP 0.1.1) --------------------------


@pytest.mark.parametrize(
    ("overrides", "field", "expected"),
    [
        ({"close": 358.43}, 4, "358,43"),
        ({"close": 1234.5}, 4, "1.234,50"),
        ({"volume": 18843459}, 7, "18.843.459"),
        ({"change": 3.55, "change_pct": 1.0}, 5, "+3,55"),
        ({"change": 3.55, "change_pct": 1.0}, 6, "+1,00%"),
        ({"change": -0.49, "change_pct": -1.1}, 5, "-0,49"),
        ({"change": -0.49, "change_pct": -1.1}, 6, "-1,10%"),
        ({"change": 0.0, "change_pct": 0.0}, 5, "0,00"),
        ({"change": 0.0, "change_pct": 0.0}, 6, "0,00%"),
        ({"week52_low": 243.42}, 8, "243,42"),
        ({"week52_high": 1399.9}, 9, "1.399,90"),
    ],
)
def test_celula_formatada_no_padrao_pt_br(
    overrides: dict[str, object], field: int, expected: str
) -> None:
    quote = good(**overrides)

    assert asset_fields(build([quote], NOW))[0][field] == expected


def test_zero_nao_recebe_sinal_de_mais() -> None:
    report = build([good(change=0.0, change_pct=0.0)], NOW)

    assert "+0,00" not in report.text
    assert "+0,00" not in report.html


def test_coluna_data_usa_iso_do_ultimo_pregao() -> None:
    quote = good(last_trading_date=date(2026, 11, 12))

    assert asset_fields(build([quote], NOW))[0][3] == "2026-11-12"


def test_campos_ausentes_viram_sentinela_e_close_permanece() -> None:
    fields = asset_fields(build([missing()], NOW))[0]

    assert fields[0] == "AAPL"
    # Nome, Moeda, Data, Variação, Variação %, Volume, Mín. 52s e Máx. 52s.
    assert [fields[index] for index in (1, 2, 3, 5, 6, 7, 8, 9)] == [ND] * 8
    assert fields[4] == "358,43"


def test_nenhum_nan_ou_none_chega_as_celulas() -> None:
    report = build([missing()], NOW)
    # O rodapé contém a palavra "Finance", que por acaso abriga as letras "nan";
    # por isso a busca vale para as células de dados, nunca para o corpo inteiro.
    cells = asset_lines(report) + row_cells(report.html, 1)

    for cell in cells:
        assert "nan" not in cell.lower()
        assert "None" not in cell


# --- Linha de falha (AAP 0.5.4 "Coluna Estado e linha com falha", AAP 0.8) ------


def test_linha_com_falha_mantem_o_ticker_na_posicao_configurada() -> None:
    fields = asset_fields(build(failing_entries(), NOW))[1]

    assert fields[0] == "BTC-USD"


def test_linha_com_falha_mostra_nd_nas_nove_colunas_de_mercado() -> None:
    fields = asset_fields(build(failing_entries(), NOW))[1]

    assert fields[1:10] == [ND] * 9


def test_linha_com_falha_traz_o_motivo_verbatim_na_coluna_estado() -> None:
    fields = asset_fields(build(failing_entries(), NOW))[1]

    assert fields[10] == "dados indisponíveis: falha de rede após 3 tentativas"


def test_ativos_cotados_ao_redor_da_falha_permanecem_intactos() -> None:
    report = build(failing_entries(), NOW)
    fields = asset_fields(report)

    assert [row[0] for row in fields] == ["AAPL", "BTC-USD", "PETR4.SA"]
    assert fields[0][4] == "358,43"
    assert fields[0][10] == "ok"
    assert fields[2][4] == "45,19"
    assert fields[2][5] == "-0,49"
    assert fields[2][10] == "ok"
    assert len(asset_lines(report)) == len(failing_entries())


def test_linha_com_falha_recebe_a_classe_falha_e_nd_nas_celulas() -> None:
    report = build(failing_entries(), NOW)

    assert 'class="falha"' in row_tags(report.html)[2]
    cells = row_cells(report.html, 2)
    assert cells[0] == "BTC-USD"
    assert cells[1:10] == [ND] * 9
    assert cells[10] == "dados indisponíveis: falha de rede após 3 tentativas"


# --- Corpo HTML: estrutura (AAP 0.5.4 "Corpo HTML") ---------------------------


def test_html_tem_uma_tabela_com_thead_e_tbody() -> None:
    html = build(ordered_entries(), NOW).html

    assert html.count("<thead>") == 1
    assert html.count("<tbody>") == 1
    assert html.count('scope="col"') == 11


def test_cabecalho_html_traz_as_onze_colunas_na_ordem() -> None:
    html = build(ordered_entries(), NOW).html
    positions = [html.index(label) for label in HTML_HEADERS]

    assert len(HTML_HEADERS) == 11
    assert positions == sorted(positions)
    assert len(set(positions)) == len(HTML_HEADERS)


def test_html_tem_uma_linha_de_cabecalho_e_uma_por_ativo() -> None:
    report = build(ordered_entries(), NOW)

    assert len(row_tags(report.html)) == 1 + len(ordered_entries())


def test_html_e_autocontido_e_identificado_em_pt_br() -> None:
    html = build(ordered_entries(), NOW).html
    lowered = html.lower()

    assert '<html lang="pt-BR">' in html
    assert "<style>" in html
    assert html.count(FOOTER) == 1
    for external in ("<img", "<link", "<script", "url("):
        assert external not in lowered


# --- Direção da variação e cor (AAP 0.5.4 "Semântica de variação") ------------


def test_variacao_ausente_vira_nd() -> None:
    # AAP 0.8 "Preço anterior ausente": sem sessão anterior não há variação, as duas
    # células mostram ``n/d`` e a linha segue na posição configurada, sem exceção.
    sem_variacao = good(change=None, change_pct=None)
    report = build([quote_petr4(), sem_variacao], NOW)
    fields = asset_fields(report)[1]
    cells = row_cells(report.html, 2)
    tags = cell_tags(report.html, 2)

    # As demais colunas continuam intactas nos dois corpos e a linha não se esconde.
    assert fields[0] == "AAPL"
    assert fields[4] == "358,43"
    assert fields[10] == "ok"
    assert cells[4] == "358,43"
    assert cells[10] == "ok"
    # A ausência não vira zero: o ``n/d`` substitui a variação nos dois corpos.
    assert fields[5] == ND
    assert fields[6] == ND
    assert cells[5] == ND
    assert cells[6] == ND
    # O cinza de 0.5.4 fica nas duas células de variação; a linha não recebe classe.
    assert tags[5] == '<td class="num neutro">'
    assert tags[6] == '<td class="num neutro">'
    assert row_tags(report.html)[2] == "<tr>"


@pytest.mark.parametrize(
    ("change", "change_pct", "classe", "variacao", "variacao_pct"),
    [
        (3.55, 1.0, "positivo", "+3,55", "+1,00%"),
        (-0.49, -1.1, "negativo", "-0,49", "-1,10%"),
        (0.0, 0.0, "neutro", "0,00", "0,00%"),
    ],
)
def test_direcao_positiva_negativa_zero(
    change: float,
    change_pct: float,
    classe: str,
    variacao: str,
    variacao_pct: str,
) -> None:
    quote = good(change=change, change_pct=change_pct)
    report = build([quote], NOW)
    fields = asset_fields(report)[0]
    tags = cell_tags(report.html, 1)

    # A cor descreve a variação: fica nas duas células dela, nunca na linha, de modo
    # que Fechamento, Volume e Estado não herdem o verde/vermelho nem o cinza.
    assert tags[5] == f'<td class="num {classe}">'
    assert tags[6] == f'<td class="num {classe}">'
    assert row_tags(report.html)[1] == "<tr>"
    # O tipo da variação nunca depende só da cor: o texto carrega o mesmo sinal.
    assert fields[5] == variacao
    assert fields[6] == variacao_pct


@pytest.mark.parametrize(
    ("css_class", "cor"),
    [
        ("positivo", "#1a7f37"),
        ("negativo", "#c62828"),
        ("neutro", "#666666"),
        ("falha", "#666666"),
    ],
)
def test_css_declara_verde_vermelho_e_cinza_por_classe(css_class: str, cor: str) -> None:
    # AAP 0.5.4 "Semântica de variação": verde para positivo, vermelho para negativo,
    # cinza para zero, ausente e linha com falha. A cor declarada é o que o leitor vê,
    # então é ela que o teste fixa — não a presença do nome da classe.
    colors = declared_colors(build(failing_entries(), NOW).html)

    assert colors.get(css_class) == cor


def test_as_tres_cores_semanticas_nao_se_colapsam() -> None:
    # Uma regra única em cinza manteria os nomes ``positivo``/``negativo`` no HTML e
    # apagaria a direção: as três cores semânticas precisam ser distintas.
    colors = declared_colors(build(failing_entries(), NOW).html)

    assert len({colors["positivo"], colors["negativo"], colors["neutro"]}) == 3


@pytest.mark.parametrize(
    ("change", "change_pct", "classe", "variacao", "classe_pct", "variacao_pct"),
    [
        (0.001, 0.001, "neutro", "0,00", "neutro", "0,00%"),
        (-0.001, -0.001, "neutro", "0,00", "neutro", "0,00%"),
        (0.010000000000047748, 0.0027900000000013227, "positivo", "+0,01", "neutro", "0,00%"),
        (0.89, 0.000072, "positivo", "+0,89", "neutro", "0,00%"),
    ],
    ids=["positivo-que-arredonda-para-zero", "negativo-que-arredonda-para-zero",
         "um-centavo-sobre-358", "oitenta-e-nove-centavos"],
)
def test_variacao_que_arredonda_para_zero_fica_sem_sinal_e_neutra(
    change: float,
    change_pct: float,
    classe: str,
    variacao: str,
    classe_pct: str,
    variacao_pct: str,
) -> None:
    # AAP 0.8 "Variação positiva, negativa ou zero": nunca mostrar ``+0,00`` e nunca
    # depender só de cor. Cada célula segue o valor que ela imprime, então a que mostra
    # ``0,00%`` recebe o mesmo cinza de um zero exato enquanto a outra mantém o sinal.
    report = build([good(change=change, change_pct=change_pct)], NOW)
    fields = asset_fields(report)[0]
    cells = row_cells(report.html, 1)
    tags = cell_tags(report.html, 1)

    assert tags[5] == f'<td class="num {classe}">'
    assert cells[5] == variacao
    assert tags[6] == f'<td class="num {classe_pct}">'
    assert cells[6] == variacao_pct
    assert fields[5] == variacao
    assert fields[6] == variacao_pct
    # O sinal de mais (ou de menos) sobre zero não aparece em nenhum dos dois corpos.
    assert "+0,00" not in report.html
    assert "-0,00" not in report.html
    assert "+0,00" not in report.text
    assert "-0,00" not in report.text


def test_cor_da_variacao_vem_sempre_acompanhada_do_sinal() -> None:
    # AAP 0.5.4: "Cores são acompanhadas de sinal, de modo que a informação não depende
    # só de cor". A varredura cobre positivo, negativo, zero exato, os dois arredondados,
    # a variação ausente e a linha com falha nas duas colunas de variação.
    entries = variation_entries()
    report = build(entries, NOW)
    classes: set[str] = set()
    for index in range(1, len(entries) + 1):
        tags = cell_tags(report.html, index)
        cells = row_cells(report.html, index)
        for column in (5, 6):
            tag, text = tags[column], cells[column]
            if text.startswith("+"):
                assert tag == '<td class="num positivo">', (index, column, tag, text)
            elif text.startswith("-"):
                assert tag == '<td class="num negativo">', (index, column, tag, text)
            else:
                # ``0,00``, ``0,00%`` e ``n/d`` não têm direção: cinza e sem sinal.
                assert tag == '<td class="num neutro">', (index, column, tag, text)
            classes.add(tag)
    # As três cores aparecem de verdade: a varredura não é vácua.
    assert classes == {
        '<td class="num positivo">',
        '<td class="num negativo">',
        '<td class="num neutro">',
    }


# --- Escape de HTML apenas no HTML (AAP 0.5.4, AAP 0.1.1) --------------------


@pytest.mark.parametrize(
    ("name", "escaped"),
    [
        ("AT&T", "AT&amp;T"),
        ("Lowe's", "Lowe&#39;s"),
        ("A < B", "A &lt; B"),
    ],
)
def test_nome_escapado_apenas_no_corpo_html(name: str, escaped: str) -> None:
    report = build([good(name=name)], NOW)

    assert escaped in report.html
    assert name not in report.html
    assert name in report.text


def test_nome_nao_e_escapado_duas_vezes() -> None:
    report = build([good(name="AT&T")], NOW)

    assert "AT&amp;amp;T" not in report.html
    assert asset_fields(report)[0][1] == "AT&T"


def test_ticker_e_motivo_escapados_apenas_no_html() -> None:
    report = build([QuoteError("A&B", "motivo <x>")], NOW)
    fields = asset_fields(report)[0]

    assert "A&amp;B" in report.html
    assert "motivo &lt;x&gt;" in report.html
    assert "A&B" not in report.html
    assert "motivo <x>" not in report.html
    assert fields[0] == "A&B"
    assert fields[10] == "dados indisponíveis: motivo <x>"


# --- Determinismo e formato do Report -----------------------------------------


def test_duas_construcoes_com_a_mesma_entrada_produzem_os_mesmos_bytes() -> None:
    first = build(ordered_entries(), NOW)
    second = build(ordered_entries(), NOW)

    assert (first.subject, first.text, first.html) == (second.subject, second.text, second.html)


def test_report_expoe_assunto_texto_e_html_como_str() -> None:
    report = build([good()], NOW)

    assert isinstance(report, Report)
    assert isinstance(report.subject, str)
    assert isinstance(report.text, str)
    assert isinstance(report.html, str)
