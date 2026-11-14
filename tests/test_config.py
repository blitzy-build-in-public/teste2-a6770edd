"""Contrato de ``finmail/config.py``: esquema do YAML e credenciais do processo.

Cobre as seis chaves da AAP 0.7.1 com seus limites, a ordem do primeiro erro
(``tickers``, ``recipients``, ``sender``, ``smtp.host``, ``smtp.port``,
``timezone``), a rejeição de chaves desconhecidas, as falhas do arquivo inteiro,
a leitura de credenciais da AAP 0.5.3 e a imutabilidade de ``Config``. Que o
esquema não tenha chave de credencial (AAP 0.7.2) é provado pelos casos de
``password`` escritos no YAML.

Nenhum teste aqui toca a rede, lê credencial real ou importa ``yfinance``: as
fixtures autouse do harness bloqueiam destinos fora de loopback e removem
``SMTP_USERNAME``/``SMTP_PASSWORD`` do ambiente.
"""

import dataclasses
import time
from collections.abc import Callable
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import yaml

from finmail.config import (
    Config,
    ConfigError,
    Credentials,
    SmtpConfig,
    load_config,
    load_credentials,
)

# Limites documentados: 1 a 50 ativos e 1 a 20 destinatários.
TICKERS_NO_LIMITE = [f"ATIVO{indice}" for indice in range(50)]
TICKERS_ACIMA_DO_LIMITE = [f"ATIVO{indice}" for indice in range(51)]
DESTINATARIOS_NO_LIMITE = [f"destinatario{indice}@exemplo.com" for indice in range(20)]
DESTINATARIOS_ACIMA_DO_LIMITE = [f"destinatario{indice}@exemplo.com" for indice in range(21)]


def _without(config_writer: Callable[..., Path], *keys: str) -> Path:
    """Escreve o ``config.yaml`` do harness sem as chaves indicadas.

    Aceita chave de primeiro nível (``"tickers"``) e caminho pontuado
    (``"smtp.port"``). A remoção parte do documento válido que o ``config_writer``
    já produz, então só a chave citada fica ausente: um caso de "chave ausente"
    não pode ser confundido com uma configuração quebrada em outro campo.
    """
    document = yaml.safe_load(config_writer().read_text(encoding="utf-8"))
    for key in keys:
        section, _, leaf = key.partition(".")
        if leaf:
            document.get(section, {}).pop(leaf, None)
        else:
            document.pop(section, None)
    return config_writer(raw=yaml.safe_dump(document, sort_keys=False, allow_unicode=True))


# --- Configuração válida ---------------------------------------------------


def test_configuracao_valida_devolve_os_campos_do_arquivo(
    config_writer: Callable[..., Path]
) -> None:
    config = load_config(config_writer())

    assert isinstance(config, Config)
    assert config.tickers == ("AAPL", "PETR4.SA", "MSFT")
    assert config.recipients == ("destinatario1@exemplo.com", "destinatario2@exemplo.com")
    assert config.sender == "remetente@exemplo.com"
    assert isinstance(config.smtp, SmtpConfig)
    assert config.smtp.host == "localhost"
    assert config.smtp.port == 587
    assert config.timezone_name == "America/Sao_Paulo"
    assert isinstance(config.timezone, ZoneInfo)


def test_ordem_dos_tickers_do_arquivo_e_preservada(config_writer: Callable[..., Path]) -> None:
    config = load_config(config_writer(tickers=["MSFT", "PETR4.SA", "AAPL"]))

    assert isinstance(config.tickers, tuple)
    assert config.tickers == ("MSFT", "PETR4.SA", "AAPL")


def test_ordem_dos_destinatarios_do_arquivo_e_preservada(
    config_writer: Callable[..., Path]
) -> None:
    config = load_config(
        config_writer(
            recipients=["segundo@exemplo.com", "primeiro@exemplo.com"],
        )
    )

    assert isinstance(config.recipients, tuple)
    assert config.recipients == ("segundo@exemplo.com", "primeiro@exemplo.com")


def test_porta_padrao_e_587_quando_a_chave_esta_ausente(config_writer: Callable[..., Path]) -> None:
    config = load_config(_without(config_writer, "smtp.port"))

    assert config.smtp.port == 587
    assert config.smtp.host == "localhost"


def test_fuso_padrao_e_sao_paulo_quando_a_chave_esta_ausente(
    config_writer: Callable[..., Path]
) -> None:
    config = load_config(_without(config_writer, "timezone"))

    assert config.timezone_name == "America/Sao_Paulo"
    assert isinstance(config.timezone, ZoneInfo)
    assert str(config.timezone) == "America/Sao_Paulo"


def test_limites_de_50_ativos_e_20_destinatarios_sao_aceitos(
    config_writer: Callable[..., Path]
) -> None:
    path = config_writer(tickers=TICKERS_NO_LIMITE, recipients=DESTINATARIOS_NO_LIMITE)

    config = load_config(path)

    assert config.tickers == tuple(TICKERS_NO_LIMITE)
    assert config.recipients == tuple(DESTINATARIOS_NO_LIMITE)


# --- Regras de ``tickers`` -------------------------------------------------


def test_tickers_ausentes_levantam_config_error(config_writer: Callable[..., Path]) -> None:
    with pytest.raises(ConfigError) as excinfo:
        load_config(_without(config_writer, "tickers"))

    message = str(excinfo.value)
    assert "tickers" in message
    assert "ausente ou vazia" in message


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        pytest.param({"tickers": []}, "ausente ou vazia", id="lista-vazia"),
        pytest.param({"tickers": ["AAPL", "   "]}, "vazio", id="item-em-branco"),
        pytest.param({"tickers": ["AAPL", 123]}, "não é um texto", id="item-nao-texto"),
        pytest.param(
            {"tickers": TICKERS_ACIMA_DO_LIMITE},
            "acima do limite",
            id="51-ativos",
        ),
        pytest.param(
            {"tickers": "AAPL"},
            "deve ser uma lista de strings, recebido texto",
            id="tickers-nao-lista",
        ),
    ],
)
def test_tickers_invalidos_levantam_config_error(
    config_writer: Callable[..., Path], overrides: dict[str, object], reason: str
) -> None:
    with pytest.raises(ConfigError) as excinfo:
        load_config(config_writer(**overrides))

    message = str(excinfo.value)
    assert "tickers" in message
    assert reason in message


def test_ticker_duplicado_ignora_caixa_e_cita_o_valor(config_writer: Callable[..., Path]) -> None:
    with pytest.raises(ConfigError) as excinfo:
        load_config(config_writer(tickers=["AAPL", "aapl"]))

    message = str(excinfo.value)
    assert "tickers" in message
    assert "duplicado" in message
    assert "aapl" in message


# --- Regras de ``recipients`` ----------------------------------------------


def test_recipients_ausentes_levantam_config_error(config_writer: Callable[..., Path]) -> None:
    with pytest.raises(ConfigError) as excinfo:
        load_config(_without(config_writer, "recipients"))

    message = str(excinfo.value)
    assert "recipients" in message
    assert "ausente ou vazia" in message


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        pytest.param({"recipients": []}, "ausente ou vazia", id="lista-vazia"),
        pytest.param(
            {"recipients": DESTINATARIOS_ACIMA_DO_LIMITE},
            "acima do limite",
            id="21-destinatarios",
        ),
        pytest.param({"recipients": ["invalido"]}, "endereço inválido", id="sem-arroba"),
        pytest.param(
            {"recipients": ["Nome <a@b.com>"]},
            "sem nome de exibição",
            id="com-nome-de-exibicao",
        ),
        pytest.param(
            {"recipients": "a@b.com"},
            "deve ser uma lista de endereços, recebido texto",
            id="recipients-nao-lista",
        ),
    ],
)
def test_recipients_invalidos_levantam_config_error(
    config_writer: Callable[..., Path], overrides: dict[str, object], reason: str
) -> None:
    with pytest.raises(ConfigError) as excinfo:
        load_config(config_writer(**overrides))

    message = str(excinfo.value)
    assert "recipients" in message
    assert reason in message


# --- Regras de ``sender`` --------------------------------------------------


def test_sender_ausente_levanta_config_error(config_writer: Callable[..., Path]) -> None:
    with pytest.raises(ConfigError) as excinfo:
        load_config(_without(config_writer, "sender"))

    message = str(excinfo.value)
    assert "sender" in message
    assert "remetente ausente" in message


@pytest.mark.parametrize(
    ("sender", "reason"),
    [
        pytest.param("invalido", "endereço inválido", id="sem-arroba"),
        pytest.param("Nome <a@b.com>", "sem nome de exibição", id="com-nome-de-exibicao"),
        pytest.param("", "remetente ausente", id="em-branco"),
    ],
)
def test_sender_invalido_levanta_config_error(
    config_writer: Callable[..., Path], sender: str, reason: str
) -> None:
    with pytest.raises(ConfigError) as excinfo:
        load_config(config_writer(sender=sender))

    message = str(excinfo.value)
    assert "sender" in message
    assert reason in message


# --- Regras de ``smtp.host`` e ``smtp.port`` -------------------------------


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        pytest.param({"smtp": {"host": ""}}, "host vazio", id="host-vazio"),
        pytest.param({"smtp": {"host": "   "}}, "host vazio", id="host-em-branco"),
    ],
)
def test_smtp_host_invalido_levanta_config_error(
    config_writer: Callable[..., Path], overrides: dict[str, object], reason: str
) -> None:
    with pytest.raises(ConfigError) as excinfo:
        load_config(config_writer(**overrides))

    message = str(excinfo.value)
    assert "smtp.host" in message
    assert reason in message


def test_smtp_ausente_levanta_config_error_citando_o_host(
    config_writer: Callable[..., Path]
) -> None:
    with pytest.raises(ConfigError) as excinfo:
        load_config(_without(config_writer, "smtp"))

    message = str(excinfo.value)
    assert "smtp.host" in message
    assert "ausente" in message


@pytest.mark.parametrize("port", ["abc", 0, -1, 65536, pytest.param(True, id="booleano")])
def test_smtp_port_invalido_levanta_config_error(
    config_writer: Callable[..., Path], port: object
) -> None:
    with pytest.raises(ConfigError) as excinfo:
        load_config(config_writer(smtp={"port": port}))

    message = str(excinfo.value)
    assert "smtp.port" in message
    assert "inteiro" in message
    assert f"'{port}'" in message


def test_smtp_port_presente_sem_valor_levanta_config_error(
    config_writer: Callable[..., Path]
) -> None:
    """``port:`` sem valor está presente (AAP 0.7.1): não vale o padrão 587."""
    with pytest.raises(ConfigError) as excinfo:
        load_config(config_writer(smtp={"port": None}))

    message = str(excinfo.value)
    assert excinfo.value.field == "smtp.port"
    assert message.startswith("Erro de configuração em 'smtp.port':")
    assert "sem valor" in message
    assert "inteiro" in message


# --- Regras de ``timezone`` ------------------------------------------------


def test_timezone_invalido_vira_config_error_citando_o_fuso(
    config_writer: Callable[..., Path]
) -> None:
    """Um nome IANA inválido não pode escapar como ``ZoneInfoNotFoundError``."""
    with pytest.raises(ConfigError) as excinfo:
        load_config(config_writer(timezone="Nao/Existe"))

    message = str(excinfo.value)
    assert "timezone" in message
    assert "Nao/Existe" in message


def test_timezone_presente_sem_valor_levanta_config_error(
    config_writer: Callable[..., Path]
) -> None:
    """``timezone:`` sem valor está presente: não vale o padrão do fuso."""
    with pytest.raises(ConfigError) as excinfo:
        load_config(config_writer(timezone=None))

    message = str(excinfo.value)
    assert excinfo.value.field == "timezone"
    assert message.startswith("Erro de configuração em 'timezone':")
    assert "sem valor" in message
    assert "nome IANA" in message


def test_leitura_do_fuso_com_tzdata_ilegivel_vira_config_error(
    config_writer: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Um ``OSError`` do ``ZoneInfo`` (tzdata quebrado) não escapa como traceback."""

    def zoneinfo_com_falha(name: str) -> ZoneInfo:
        raise OSError(f"tzdata indisponível para o fuso {name}")

    monkeypatch.setattr("finmail.config.ZoneInfo", zoneinfo_com_falha)

    with pytest.raises(ConfigError) as excinfo:
        load_config(config_writer())

    assert excinfo.value.field == "timezone"
    assert "America/Sao_Paulo" in str(excinfo.value)


# --- Chaves desconhecidas e credenciais no arquivo -------------------------


def test_chave_desconhecida_no_topo_e_rejeitada(config_writer: Callable[..., Path]) -> None:
    with pytest.raises(ConfigError) as excinfo:
        load_config(config_writer(foo=1))

    assert "foo" in str(excinfo.value)


def test_chave_desconhecida_no_bloco_smtp_e_rejeitada(config_writer: Callable[..., Path]) -> None:
    with pytest.raises(ConfigError) as excinfo:
        load_config(config_writer(smtp={"user": "x"}))

    assert "user" in str(excinfo.value)


def test_credencial_no_topo_e_rejeitada_como_chave_desconhecida(
    config_writer: Callable[..., Path], sentinel_password: str
) -> None:
    with pytest.raises(ConfigError) as excinfo:
        load_config(config_writer(password=sentinel_password))

    message = str(excinfo.value)
    assert "password" in message
    assert sentinel_password not in message


def test_credencial_no_bloco_smtp_e_rejeitada_como_chave_desconhecida(
    config_writer: Callable[..., Path], sentinel_password: str
) -> None:
    with pytest.raises(ConfigError) as excinfo:
        load_config(config_writer(smtp={"password": sentinel_password}))

    message = str(excinfo.value)
    assert "password" in message
    assert sentinel_password not in message


@pytest.mark.parametrize("variable", ["SMTP_USERNAME", "SMTP_PASSWORD"])
@pytest.mark.parametrize("definida", [False, True])
def test_variavel_de_credencial_no_topo_vira_erro_de_configuracao(
    config_writer: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    sentinel_username: str,
    sentinel_password: str,
    variable: str,
    definida: bool,
) -> None:
    """A forma da mensagem vem da origem da falha, não do nome do campo.

    A chave desconhecida existe no YAML, então o diagnóstico é de configuração
    mesmo quando a variável de ambiente está definida (AAP 0.7.1 e 0.7.2):
    anunciar credencial ausente seria um diagnóstico falso.
    """
    valor = sentinel_password if variable == "SMTP_PASSWORD" else sentinel_username
    if definida:
        monkeypatch.setenv(variable, valor)

    with pytest.raises(ConfigError) as excinfo:
        load_config(config_writer(**{variable: valor}))

    message = str(excinfo.value)
    assert message.startswith(f"Erro de configuração em '{variable}'")
    assert "chave desconhecida" in message
    assert "Credencial ausente" not in message
    assert valor not in message


# --- Ordem do primeiro erro ------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "earliest", "later_fields"),
    [
        pytest.param(
            {
                "tickers": [],
                "recipients": [],
                "sender": "x",
                "smtp": {"host": "", "port": "abc"},
                "timezone": "Nao/Existe",
            },
            "tickers",
            ("recipients", "sender", "smtp.host", "smtp.port", "timezone"),
            id="tickers",
        ),
        pytest.param(
            {
                "recipients": [],
                "sender": "x",
                "smtp": {"host": "", "port": "abc"},
                "timezone": "Nao/Existe",
            },
            "recipients",
            ("sender", "smtp.host", "smtp.port", "timezone"),
            id="recipients",
        ),
        pytest.param(
            {
                "sender": "x",
                "smtp": {"host": "", "port": "abc"},
                "timezone": "Nao/Existe",
            },
            "sender",
            ("smtp.host", "smtp.port", "timezone"),
            id="sender",
        ),
        pytest.param(
            {"smtp": {"host": "", "port": "abc"}, "timezone": "Nao/Existe"},
            "smtp.host",
            ("smtp.port", "timezone"),
            id="smtp-host",
        ),
        pytest.param(
            {"smtp": {"port": "abc"}, "timezone": "Nao/Existe"},
            "smtp.port",
            ("timezone",),
            id="smtp-port",
        ),
        pytest.param({"timezone": "Nao/Existe"}, "timezone", (), id="timezone"),
    ],
)
def test_primeiro_campo_invalido_e_o_reportado(
    config_writer: Callable[..., Path],
    overrides: dict[str, object],
    earliest: str,
    later_fields: tuple[str, ...],
) -> None:
    """Com todos os campos quebrados, a mensagem nomeia só o primeiro da ordem."""
    with pytest.raises(ConfigError) as excinfo:
        load_config(config_writer(**overrides))

    message = str(excinfo.value)
    assert earliest in message
    for field in later_fields:
        assert field not in message


# --- Falhas do arquivo inteiro ---------------------------------------------


def test_arquivo_ausente_cita_o_caminho_e_nao_encontrado(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as excinfo:
        load_config(tmp_path / "nao-existe.yaml")

    message = str(excinfo.value)
    assert "nao-existe.yaml" in message
    assert "não encontrado" in message


def test_arquivo_que_e_diretorio_vira_config_error_em_portugues(tmp_path: Path) -> None:
    """Um diretório em ``--config`` não pode devolver o texto do sistema em inglês."""
    directory = tmp_path / "config.yaml"
    directory.mkdir()

    with pytest.raises(ConfigError) as excinfo:
        load_config(directory)

    message = str(excinfo.value)
    assert excinfo.value.field is None
    assert str(directory) in message
    assert message.endswith("o caminho é um diretório.")
    assert "Is a directory" not in message


def test_falha_de_leitura_generica_cita_o_codigo_do_erro(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """O ramo genérico de ``OSError`` também não interpola o ``strerror``."""
    path = tmp_path / "config.yaml"

    def read_text_com_falha(self: Path, **kwargs: object) -> str:
        raise OSError(40, "Too many levels of symbolic links")

    monkeypatch.setattr(Path, "read_text", read_text_com_falha)

    with pytest.raises(ConfigError) as excinfo:
        load_config(path)

    message = str(excinfo.value)
    assert "código 40" in message
    assert "Too many levels" not in message


def test_yaml_invalido_cita_a_linha(config_writer: Callable[..., Path]) -> None:
    path = config_writer(raw="tickers: [AAPL\n")

    with pytest.raises(ConfigError) as excinfo:
        load_config(path)

    message = str(excinfo.value)
    assert "YAML inválido" in message
    assert "linha" in message


def test_aninhamento_profundo_vira_config_error(config_writer: Callable[..., Path]) -> None:
    """Aninhamento além do limite do interpretador não pode escapar como traceback."""
    path = config_writer(raw="a: " + "[" * 900 + "]" * 900 + "\n")

    with pytest.raises(ConfigError) as excinfo:
        load_config(path)

    assert excinfo.value.field is None
    assert "aninhamento" in str(excinfo.value)


def test_conteudo_que_nao_e_mapeamento_levanta_config_error(
    config_writer: Callable[..., Path]
) -> None:
    path = config_writer(raw="- a\n- b\n")

    with pytest.raises(ConfigError) as excinfo:
        load_config(path)

    assert excinfo.value.field is None
    assert "mapeamento" in str(excinfo.value)


def test_arquivo_fora_do_utf8_vira_config_error_citando_o_caminho(tmp_path: Path) -> None:
    """Um arquivo em cp1252 não pode escapar como ``UnicodeDecodeError``."""
    path = tmp_path / "config.yaml"
    path.write_bytes(b"recipients:\n  - caf\xe9@exemplo.com\n")

    with pytest.raises(ConfigError) as excinfo:
        load_config(path)

    message = str(excinfo.value)
    assert excinfo.value.field is None
    assert str(path) in message
    assert "UTF-8" in message
    assert "posição 19" in message


def test_documento_vazio_levanta_config_error(config_writer: Callable[..., Path]) -> None:
    path = config_writer(raw="")

    with pytest.raises(ConfigError) as excinfo:
        load_config(path)

    assert excinfo.value.field is None
    assert "mapeamento" in str(excinfo.value)


# --- Chaves repetidas no mesmo mapeamento ----------------------------------


@pytest.mark.parametrize(
    ("raw", "key", "line"),
    [
        pytest.param(
            "tickers:\n  - AAPL\nrecipients:\n  - usuario1@exemplo.com\n"
            "recipients:\n  - outro@exemplo.com\n",
            "recipients",
            5,
            id="topo",
        ),
        pytest.param(
            "tickers:\n  - AAPL\nrecipients:\n  - usuario1@exemplo.com\n"
            "sender: finmail@exemplo.com\nsmtp:\n  host: smtp.exemplo.com\n"
            "  port: 587\n  host: outro.exemplo.com\n",
            "host",
            9,
            id="bloco-smtp",
        ),
    ],
)
def test_chave_duplicada_no_mesmo_mapeamento_e_rejeitada(
    config_writer: Callable[..., Path], raw: str, key: str, line: int
) -> None:
    """A chave declarada duas vezes não pode ser colapsada sem diagnóstico."""
    path = config_writer(raw=raw)

    with pytest.raises(ConfigError) as excinfo:
        load_config(path)

    message = str(excinfo.value)
    assert excinfo.value.field is None
    assert f"chave '{key}' repetida na linha {line}" in message


def test_tag_python_no_arquivo_continua_recusada(
    config_writer: Callable[..., Path]
) -> None:
    """A inspeção dos nós não pode deixar passar um documento com tag Python."""
    path = config_writer(raw="!!python/object/apply:os.system ['echo oi']\n")

    with pytest.raises(ConfigError) as excinfo:
        load_config(path)

    assert excinfo.value.field is None
    assert "YAML inválido" in str(excinfo.value)


# --- Âncoras e apelidos no grafo de nós ------------------------------------


@pytest.mark.parametrize(
    ("raw", "trecho"),
    [
        pytest.param(
            "a: &x [*x]\n",
            "chave desconhecida",
            id="alias-ciclico",
        ),
        pytest.param(
            "tickers: &x [AAPL, *x]\nrecipients:\n  - usuario1@exemplo.com\n"
            "sender: finmail@exemplo.com\nsmtp:\n  host: smtp.exemplo.com\n",
            "item 2 não é um texto",
            id="tickers-recorrente",
        ),
    ],
)
def test_apelido_ciclico_vira_config_error(
    config_writer: Callable[..., Path], raw: str, trecho: str
) -> None:
    """Apelido que aponta para o próprio nó não pode escapar como ``RecursionError``."""
    path = config_writer(raw=raw)

    with pytest.raises(ConfigError) as excinfo:
        load_config(path)

    assert trecho in str(excinfo.value)


def _documento_com_apelidos(niveis: int) -> str:
    """Documento em que cada nível cita dez vezes a âncora do nível anterior."""
    linhas = ["a0: &a0 [1, 2]"]
    for nivel in range(1, niveis + 1):
        apelidos = ", ".join([f"*a{nivel - 1}"] * 10)
        linhas.append(f"a{nivel}: &a{nivel} [{apelidos}]")
    return "\n".join(linhas) + "\n"


def test_apelidos_repetidos_nao_multiplicam_o_percurso(
    config_writer: Callable[..., Path]
) -> None:
    """A mesma âncora repetida em cada nível não pode custar dez vezes mais por nível.

    Sem o conjunto de nós visitados, oito níveis passam de vinte segundos de
    percurso para um arquivo de poucas linhas escrito pelo operador; o limite
    abaixo separa esse custo exponencial de uma leitura imediata.
    """
    path = config_writer(raw=_documento_com_apelidos(8))

    inicio = time.monotonic()
    with pytest.raises(ConfigError) as excinfo:
        load_config(path)

    assert "chave desconhecida" in str(excinfo.value)
    assert time.monotonic() - inicio < 5.0


# --- Credenciais do ambiente -----------------------------------------------


def test_load_credentials_devolve_o_par_do_mapeamento(
    sentinel_username: str, sentinel_password: str
) -> None:
    credentials = load_credentials(
        {"SMTP_USERNAME": sentinel_username, "SMTP_PASSWORD": sentinel_password}
    )

    assert isinstance(credentials, Credentials)
    assert credentials.username == sentinel_username
    assert credentials.password == sentinel_password


def test_load_credentials_sem_usuario_cita_a_variavel(sentinel_password: str) -> None:
    with pytest.raises(ConfigError) as excinfo:
        load_credentials({"SMTP_PASSWORD": sentinel_password})

    message = str(excinfo.value)
    assert "SMTP_USERNAME" in message
    assert sentinel_password not in message


def test_load_credentials_sem_senha_cita_a_variavel(sentinel_username: str) -> None:
    with pytest.raises(ConfigError) as excinfo:
        load_credentials({"SMTP_USERNAME": sentinel_username})

    message = str(excinfo.value)
    assert "SMTP_PASSWORD" in message
    assert sentinel_username not in message


@pytest.mark.parametrize(
    ("environ", "linha"),
    [
        pytest.param(
            {},
            "Credencial ausente: variável SMTP_USERNAME não definida.",
            id="sem-usuario",
        ),
        pytest.param(
            {"SMTP_USERNAME": "usuario@exemplo.com"},
            "Credencial ausente: variável SMTP_PASSWORD não definida.",
            id="sem-senha",
        ),
    ],
)
def test_load_credentials_ausente_produz_a_linha_documentada(
    environ: dict[str, str], linha: str
) -> None:
    """A linha de credencial ausente da AAP 0.6.2 vem só do ambiente."""
    with pytest.raises(ConfigError) as excinfo:
        load_credentials(environ)

    assert str(excinfo.value) == linha


def test_load_credentials_le_so_o_mapeamento_recebido(
    monkeypatch: pytest.MonkeyPatch, sentinel_username: str, sentinel_password: str
) -> None:
    """As variáveis definidas no ambiente não substituem o mapeamento recebido."""
    monkeypatch.setenv("SMTP_USERNAME", sentinel_username)
    monkeypatch.setenv("SMTP_PASSWORD", sentinel_password)

    with pytest.raises(ConfigError) as excinfo:
        load_credentials({})

    assert "SMTP_USERNAME" in str(excinfo.value)


def test_repr_e_str_de_credentials_nao_expoem_a_senha(
    sentinel_username: str, sentinel_password: str
) -> None:
    credentials = load_credentials(
        {"SMTP_USERNAME": sentinel_username, "SMTP_PASSWORD": sentinel_password}
    )

    assert sentinel_password not in repr(credentials)
    assert sentinel_password not in str(credentials)
    assert sentinel_username in repr(credentials)


# --- Imutabilidade ---------------------------------------------------------


def test_config_e_imutavel(config_writer: Callable[..., Path]) -> None:
    config = load_config(config_writer())

    with pytest.raises(dataclasses.FrozenInstanceError):
        config.sender = "outro@exemplo.com"
    with pytest.raises(dataclasses.FrozenInstanceError):
        config.smtp.port = 25
