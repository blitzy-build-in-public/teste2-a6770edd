"""Leitura de ``config.yaml`` e das credenciais do processo.

Este é o único módulo que interpreta o arquivo de configuração e o único que
converte o ambiente em :class:`Credentials`. Toda a validação termina antes de
qualquer I/O de rede: uma configuração inválida vira :class:`ConfigError`, a
mensagem nomeia o campo e o motivo, e nenhum socket é aberto.

Nada é gravado em disco e nenhum segredo entra em log, exceção ou ``repr``: o
esquema do YAML não tem chave de credencial e a senha de :class:`Credentials`
fica fora da representação do objeto.
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from email.utils import parseaddr
from pathlib import Path

import yaml
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_CONFIG_PATH = Path("./config.yaml")

_SMTP_USERNAME_VAR = "SMTP_USERNAME"
_SMTP_PASSWORD_VAR = "SMTP_PASSWORD"
_CREDENTIAL_VARS = frozenset({_SMTP_USERNAME_VAR, _SMTP_PASSWORD_VAR})

_DEFAULT_SMTP_PORT = 587
_DEFAULT_TIMEZONE = "America/Sao_Paulo"
_MAX_TICKERS = 50
_MAX_RECIPIENTS = 20
_TOP_LEVEL_KEYS = frozenset({"tickers", "recipients", "sender", "smtp", "timezone"})
_SMTP_KEYS = frozenset({"host", "port"})
_ROOT_KEYS_HINT = (
    "conteúdo deve ser um mapeamento com as chaves "
    "tickers, recipients, sender, smtp e timezone"
)


class ConfigError(Exception):
    """Configuração inválida ou credencial de SMTP ausente.

    ``field`` nomeia a chave de configuração (ou a variável de ambiente) que
    falhou e ``reason`` diz o que está errado, em português. ``str(exc)`` é a
    única linha que o operador vê, e assume uma de três formas:

    - ``field is None``: ``Erro de configuração: <reason>``, para falhas do
      arquivo inteiro (ausente, YAML inválido, raiz que não é mapeamento).
    - ``field`` é chave de configuração: ``Erro de configuração em
      '<field>': <reason>``.
    - ``field`` é ``SMTP_USERNAME`` ou ``SMTP_PASSWORD``: ``Credencial ausente:
      variável <field> <reason>.``, que com ``reason="não definida"`` produz a
      linha de credencial ausente documentada para o operador. É esta a
      reconciliação entre o contrato ``ConfigError(<variável>, reason)`` e o
      texto de credencial: a variável é o próprio campo, sem atributo extra.
    """

    def __init__(self, field: str | None, reason: str) -> None:
        self.field = field
        self.reason = reason
        super().__init__(self._message())

    def _message(self) -> str:
        if self.field is None:
            return f"Erro de configuração: {self.reason}"
        if self.field in _CREDENTIAL_VARS:
            return f"Credencial ausente: variável {self.field} {self.reason}."
        return f"Erro de configuração em '{self.field}': {self.reason}"


@dataclass(frozen=True)
class SmtpConfig:
    """Servidor SMTP de saída, sempre usado com STARTTLS."""

    host: str
    port: int = _DEFAULT_SMTP_PORT


@dataclass(frozen=True)
class Config:
    """Configuração de uma execução, imutável e já validada por ``load_config``."""

    tickers: tuple[str, ...]
    recipients: tuple[str, ...]
    sender: str
    smtp: SmtpConfig
    timezone_name: str
    timezone: ZoneInfo


@dataclass(frozen=True)
class Credentials:
    """Par usuário/senha de SMTP, lido somente de variáveis de ambiente.

    ``password`` fica fora de ``repr`` para que nenhuma representação do objeto
    carregue o segredo.
    """

    username: str
    password: str = field(repr=False)


def load_config(path: Path) -> Config:
    """Carrega e valida o arquivo de configuração indicado.

    A validação termina antes de qualquer I/O de rede e para no primeiro campo
    inválido, na ordem ``tickers``, ``recipients``, ``sender``, ``smtp.host``,
    ``smtp.port`` e ``timezone``. Chaves desconhecidas são rejeitadas antes da
    validação dos campos.

    Args:
        path: Caminho do arquivo YAML, relativo ou absoluto.

    Returns:
        A configuração validada, com tuplas no lugar de listas.

    Raises:
        ConfigError: arquivo ausente ou ilegível, YAML inválido, conteúdo que
            não é mapeamento, chave desconhecida ou campo fora das regras.
    """
    # O --config chega da linha de comando como texto: normalizar aqui evita que
    # um caminho str vire AttributeError em vez de erro de configuração.
    path = Path(path)
    root = _read_mapping(path)
    _reject_unknown_keys(root, _TOP_LEVEL_KEYS, "")
    tickers = _parse_tickers(root)
    recipients = _parse_recipients(root)
    sender = _parse_sender(root)
    smtp = _parse_smtp(root)
    timezone_name, timezone = _parse_timezone(root)
    return Config(
        tickers=tickers,
        recipients=recipients,
        sender=sender,
        smtp=smtp,
        timezone_name=timezone_name,
        timezone=timezone,
    )


def load_credentials(environ: Mapping[str, str] | None = None) -> Credentials:
    """Monta as credenciais de SMTP a partir do ambiente.

    Nenhum valor é alterado nem ecoado: uma variável ausente ou vazia vira
    ``ConfigError`` cuja mensagem cita apenas o nome da variável, nunca o
    conteúdo da outra.

    Args:
        environ: Mapeamento a consultar. ``None`` usa ``os.environ``.

    Returns:
        As credenciais, com a senha fora de ``repr``.

    Raises:
        ConfigError: ``SMTP_USERNAME`` ou ``SMTP_PASSWORD`` ausente, vazia ou
            composta apenas de espaços.
    """
    source = os.environ if environ is None else environ
    return Credentials(
        username=_required_variable(source, _SMTP_USERNAME_VAR),
        password=_required_variable(source, _SMTP_PASSWORD_VAR),
    )


def _read_mapping(path: Path) -> Mapping[str, object]:
    try:
        with path.open(encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
    except FileNotFoundError as exc:
        raise ConfigError(None, f"arquivo '{path}' não encontrado.") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(None, _yaml_problem(exc)) from exc
    except OSError as exc:
        detail = exc.strerror or str(exc)
        raise ConfigError(None, f"não foi possível ler o arquivo '{path}': {detail}.") from exc
    if data is None:
        raise ConfigError(None, _ROOT_KEYS_HINT)
    if not isinstance(data, Mapping):
        raise ConfigError(None, f"{_ROOT_KEYS_HINT}, recebido {_type_label(data)}")
    return data


def _yaml_problem(exc: yaml.YAMLError) -> str:
    problem = getattr(exc, "problem", None) or "sintaxe inválida"
    mark = getattr(exc, "problem_mark", None)
    if mark is None:
        return f"YAML inválido: {problem}."
    return f"YAML inválido na linha {mark.line + 1}, coluna {mark.column + 1}: {problem}."


def _reject_unknown_keys(
    section: Mapping[str, object],
    allowed: frozenset[str],
    field_prefix: str,
) -> None:
    unknown = [item for item in section if item not in allowed]
    if not unknown:
        return
    key = unknown[0]
    name = key if isinstance(key, str) else str(key)
    valid = ", ".join(sorted(allowed))
    raise ConfigError(
        f"{field_prefix}{name}",
        f"chave desconhecida (válidas neste nível: {valid})",
    )


def _type_label(value: object) -> str:
    if value is None:
        return "nulo"
    if isinstance(value, bool):
        return "booleano"
    if isinstance(value, str):
        return "texto"
    if isinstance(value, (int, float)):
        return "número"
    if isinstance(value, Mapping):
        return "mapeamento"
    if isinstance(value, (list, tuple)):
        return "lista"
    return type(value).__name__


def _bare_address(value: str) -> str | None:
    candidate = value.strip()
    display_name, address = parseaddr(candidate)
    if display_name or address != candidate:
        return None
    local, separator, domain = candidate.partition("@")
    if not separator or "@" in domain or not local or not domain:
        return None
    if any(character.isspace() for character in candidate):
        return None
    return candidate


def _parse_tickers(root: Mapping[str, object]) -> tuple[str, ...]:
    raw = root.get("tickers")
    if raw is None or (isinstance(raw, list) and not raw):
        raise ConfigError(
            "tickers",
            f"lista ausente ou vazia (esperado de 1 a {_MAX_TICKERS} ativos).",
        )
    if not isinstance(raw, list):
        raise ConfigError(
            "tickers",
            f"deve ser uma lista de strings, recebido {_type_label(raw)}.",
        )
    if len(raw) > _MAX_TICKERS:
        raise ConfigError(
            "tickers",
            f"quantidade acima do limite: {len(raw)} ativos (máximo de {_MAX_TICKERS}).",
        )
    seen: dict[str, str] = {}
    tickers: list[str] = []
    for position, item in enumerate(raw, start=1):
        if not isinstance(item, str):
            raise ConfigError(
                "tickers",
                f"item {position} não é um texto, recebido {_type_label(item)}.",
            )
        ticker = item.strip()
        if not ticker:
            raise ConfigError("tickers", f"item {position} está vazio.")
        key = ticker.upper()
        if key in seen:
            raise ConfigError(
                "tickers",
                f"ticker duplicado '{ticker}' (já informado como '{seen[key]}').",
            )
        seen[key] = ticker
        tickers.append(ticker)
    return tuple(tickers)


def _parse_recipients(root: Mapping[str, object]) -> tuple[str, ...]:
    raw = root.get("recipients")
    if raw is None or (isinstance(raw, list) and not raw):
        raise ConfigError(
            "recipients",
            f"lista ausente ou vazia (esperado de 1 a {_MAX_RECIPIENTS} endereços).",
        )
    if not isinstance(raw, list):
        raise ConfigError(
            "recipients",
            f"deve ser uma lista de endereços, recebido {_type_label(raw)}.",
        )
    if len(raw) > _MAX_RECIPIENTS:
        raise ConfigError(
            "recipients",
            f"quantidade acima do limite: {len(raw)} destinatários "
            f"(máximo de {_MAX_RECIPIENTS}).",
        )
    recipients: list[str] = []
    for position, item in enumerate(raw, start=1):
        if not isinstance(item, str):
            raise ConfigError(
                "recipients",
                f"item {position} não é um texto, recebido {_type_label(item)}.",
            )
        address = _bare_address(item)
        if address is None:
            raise ConfigError(
                "recipients",
                f"endereço inválido no item {position}: '{item}' "
                "(esperado um endereço puro, sem nome de exibição).",
            )
        recipients.append(address)
    return tuple(recipients)


def _parse_sender(root: Mapping[str, object]) -> str:
    raw = root.get("sender")
    if raw is None:
        raise ConfigError(
            "sender",
            "remetente ausente (esperado um endereço de e-mail).",
        )
    if not isinstance(raw, str):
        raise ConfigError(
            "sender",
            f"deve ser um endereço de e-mail, recebido {_type_label(raw)}.",
        )
    if not raw.strip():
        raise ConfigError(
            "sender",
            "remetente ausente (esperado um endereço de e-mail).",
        )
    address = _bare_address(raw)
    if address is None:
        raise ConfigError(
            "sender",
            f"endereço inválido: '{raw}' "
            "(esperado um endereço puro, sem nome de exibição).",
        )
    return address


def _parse_smtp(root: Mapping[str, object]) -> SmtpConfig:
    raw = root.get("smtp")
    if raw is None:
        raise ConfigError(
            "smtp.host",
            "seção 'smtp' ausente (esperado as chaves host e port).",
        )
    if not isinstance(raw, Mapping):
        raise ConfigError(
            "smtp.host",
            f"seção 'smtp' deve ser um mapeamento com as chaves host e port, "
            f"recebido {_type_label(raw)}.",
        )
    _reject_unknown_keys(raw, _SMTP_KEYS, "smtp.")
    host = raw.get("host")
    if host is None:
        raise ConfigError(
            "smtp.host",
            "host ausente (esperado o nome do servidor SMTP).",
        )
    if not isinstance(host, str):
        raise ConfigError(
            "smtp.host",
            f"deve ser um texto, recebido {_type_label(host)}.",
        )
    host = host.strip()
    if not host:
        raise ConfigError("smtp.host", "host vazio.")
    return SmtpConfig(host=host, port=_parse_port(raw.get("port")))


def _parse_port(value: object) -> int:
    if value is None:
        return _DEFAULT_SMTP_PORT
    # bool é subclasse de int: `port: true` não pode virar a porta 1.
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
        raise ConfigError(
            "smtp.port",
            f"valor '{value}' não é um inteiro entre 1 e 65535.",
        )
    return value


def _parse_timezone(root: Mapping[str, object]) -> tuple[str, ZoneInfo]:
    raw = root.get("timezone")
    if raw is None:
        name = _DEFAULT_TIMEZONE
    elif not isinstance(raw, str):
        raise ConfigError(
            "timezone",
            f"deve ser um nome IANA, recebido {_type_label(raw)}.",
        )
    else:
        name = raw.strip()
        if not name:
            raise ConfigError(
                "timezone",
                "nome vazio (esperado um nome IANA, como 'America/Sao_Paulo').",
            )
    try:
        zone = ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError(
            "timezone",
            f"fuso horário '{name}' não reconhecido "
            "(esperado um nome IANA, como 'America/Sao_Paulo').",
        ) from exc
    return name, zone


def _required_variable(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name)
    if value is None or value == "":
        raise ConfigError(name, "não definida")
    if not value.strip():
        raise ConfigError(name, "vazia")
    return value

