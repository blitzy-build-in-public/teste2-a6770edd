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


@pytest.mark.parametrize("port", ["abc", 0, -1, 65536])
def test_smtp_port_invalido_levanta_config_error(
    config_writer: Callable[..., Path], port: object
) -> None:
    with pytest.raises(ConfigError) as excinfo:
        load_config(config_writer(smtp={"port": port}))

    message = str(excinfo.value)
    assert "smtp.port" in message
    assert "inteiro" in message
    assert f"'{port}'" in message


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


def test_yaml_invalido_cita_a_linha(config_writer: Callable[..., Path]) -> None:
    path = config_writer(raw="tickers: [AAPL\n")

    with pytest.raises(ConfigError) as excinfo:
        load_config(path)

    message = str(excinfo.value)
    assert "YAML inválido" in message
    assert "linha" in message


def test_conteudo_que_nao_e_mapeamento_levanta_config_error(
    config_writer: Callable[..., Path]
) -> None:
    path = config_writer(raw="- a\n- b\n")

    with pytest.raises(ConfigError) as excinfo:
        load_config(path)

    assert excinfo.value.field is None
    assert "mapeamento" in str(excinfo.value)


def test_documento_vazio_levanta_config_error(config_writer: Callable[..., Path]) -> None:
    path = config_writer(raw="")

    with pytest.raises(ConfigError) as excinfo:
        load_config(path)

    assert excinfo.value.field is None
    assert "mapeamento" in str(excinfo.value)


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

