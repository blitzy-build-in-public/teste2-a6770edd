"""Leitura de ``config.yaml`` e das credenciais do processo.

Este é o único módulo que interpreta o arquivo de configuração e o único que
converte o ambiente em :class:`Credentials`. Toda a validação termina antes de
qualquer I/O de rede: uma configuração inválida vira :class:`ConfigError`, a
mensagem nomeia o campo e o motivo, e nenhum socket é aberto.

Nada é gravado em disco e nenhum segredo entra em log, exceção ou ``repr``: o
esquema do YAML não tem chave de credencial e o usuário e a senha de
:class:`Credentials` ficam fora da representação do objeto.
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from email.utils import parseaddr
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

DEFAULT_CONFIG_PATH = Path("./config.yaml")

_SMTP_USERNAME_VAR = "SMTP_USERNAME"
_SMTP_PASSWORD_VAR = "SMTP_PASSWORD"

_DEFAULT_SMTP_PORT = 587
_DEFAULT_TIMEZONE = "America/Sao_Paulo"
# Marcador de chave ausente: ``Mapping.get`` devolve ``None`` tanto para a chave
# que não existe quanto para a que existe sem valor, e só a primeira usa o
# padrão — ``port:`` e ``timezone:`` sem valor estão presentes e são recusados.
_AUSENTE = object()
_MAX_TICKERS = 50
_MAX_RECIPIENTS = 20
_TOP_LEVEL_KEYS = frozenset({"tickers", "recipients", "sender", "smtp", "timezone"})
_SMTP_KEYS = frozenset({"host", "port"})
_ROOT_KEYS_HINT = (
    "conteúdo deve ser um mapeamento com as chaves "
    "tickers, recipients, sender, smtp e timezone"
)
# A chave desconhecida vem do documento e a mensagem precisa nomeá-la: só
# alfanuméricos, ``_``, ``-`` e ``.`` são reproduzidos, e o resultado é limitado
# a este tamanho, para que um arquivo alheio apontado por ``--config`` não
# escreva uma linha de si mesmo no log do operador.
_MAX_RENDERED_NAME = 40
_RENDERED_NAME_ELLIPSIS = "…"


def _render_document_name(value: object) -> str:
    """Reproduz um nome lido do documento como uma linha curta e imprimível.

    Só alfanuméricos de qualquer alfabeto, ``_``, ``-`` e ``.`` são copiados; todo
    outro caractere vira ``\\xNN`` (abaixo de 0x100) ou ``\\uNNNN``, o que impede
    que um nome do documento divida a linha do diagnóstico ou mova o cursor do
    terminal. O resultado para em :data:`_MAX_RENDERED_NAME` caracteres, com
    ``…`` no fim, para que um arquivo alheio apontado por ``--config`` não seja
    copiado para o log do operador.

    Args:
        value: nome vindo do documento, que pode não ser ``str``.

    Returns:
        O nome reescrito, sempre com um único caractere imprimível por posição.
    """
    text = value if isinstance(value, str) else str(value)
    rendered: list[str] = []
    length = 0
    for character in text:
        if character.isalnum() or character in "-_.":
            piece = character
        elif ord(character) < 0x100:
            piece = f"\\x{ord(character):02x}"
        else:
            piece = f"\\u{ord(character):04x}"
        if length + len(piece) > _MAX_RENDERED_NAME:
            rendered.append(_RENDERED_NAME_ELLIPSIS)
            break
        rendered.append(piece)
        length += len(piece)
    return "".join(rendered)


class ConfigError(Exception):
    """Configuração inválida ou credencial de SMTP ausente.

    ``field`` nomeia a chave de configuração (ou a variável de ambiente) que
    falhou e ``reason`` diz o que está errado, em português. ``str(exc)`` é a
    única linha que o operador vê, e a forma é escolhida pela origem da falha,
    nunca pelo nome do campo: uma chave de YAML pode ter qualquer nome.

    - ``field is None``: ``Erro de configuração: <reason>``, para falhas do
      arquivo inteiro (ausente, YAML inválido, raiz que não é mapeamento).
    - ``credencial=True``: ``Credencial ausente: variável <field> <reason>.``,
      a linha de credencial de ambiente ausente, que com ``reason="não
      definida"`` produz o texto documentado para o operador. Só
      :func:`load_credentials` levanta desta forma.
    - caso contrário: ``Erro de configuração em '<field>': <reason>``. Vale
      também para a chave desconhecida que por acaso se chame
      ``SMTP_USERNAME`` ou ``SMTP_PASSWORD``: ela é rejeitada como chave
      desconhecida, e a mensagem não pode anunciar credencial ausente. O nome do
      campo é reproduzido por :func:`_render_document_name`, de modo que uma
      chave estranha não copia uma linha do arquivo para o diagnóstico; ``field``
      continua guardando o nome original.
    """

    def __init__(
        self,
        field: str | None,
        reason: str,
        *,
        credencial: bool = False,
    ) -> None:
        self.field = field
        self.reason = reason
        self._credencial = credencial
        super().__init__(self._message())

    def _message(self) -> str:
        if self.field is None:
            return f"Erro de configuração: {self.reason}"
        if self._credencial:
            return f"Credencial ausente: variável {self.field} {self.reason}."
        name = _render_document_name(self.field)
        return f"Erro de configuração em '{name}': {self.reason}"


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

    ``username`` e ``password`` ficam fora de ``repr`` para que nenhuma
    representação do objeto carregue os valores: o usuário é um endereço de
    e-mail e a senha é a credencial de autenticação.
    """

    username: str = field(repr=False)
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
        ConfigError: arquivo ausente, ilegível ou fora do UTF-8, YAML inválido com
            a linha, chave repetida no mesmo mapeamento, conteúdo que não é
            mapeamento, chave desconhecida ou campo fora das regras.
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
        As credenciais, com o usuário e a senha fora de ``repr``.

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
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ConfigError(None, f"arquivo '{path}' não encontrado.") from exc
    except UnicodeDecodeError as exc:
        # ``exc.reason`` e ``str(exc)`` são textos do interpretador, em inglês: a
        # mensagem cita só a posição, que é um inteiro.
        raise ConfigError(
            None,
            f"não foi possível ler o arquivo '{path}': o conteúdo não está em UTF-8 "
            f"(primeiro byte inválido na posição {exc.start}).",
        ) from exc
    except IsADirectoryError as exc:
        raise ConfigError(
            None,
            f"não foi possível ler o arquivo '{path}': o caminho é um diretório.",
        ) from exc
    except NotADirectoryError as exc:
        raise ConfigError(
            None,
            f"não foi possível ler o arquivo '{path}': parte do caminho não é um diretório.",
        ) from exc
    except PermissionError as exc:
        raise ConfigError(
            None,
            f"não foi possível ler o arquivo '{path}': permissão negada.",
        ) from exc
    except OSError as exc:
        # O texto do sistema chega em inglês (``exc.strerror``): a mensagem cita
        # apenas o código numérico do erro.
        code = "desconhecido" if exc.errno is None else str(exc.errno)
        raise ConfigError(
            None,
            f"não foi possível ler o arquivo '{path}': falha de leitura do sistema "
            f"de arquivos (código {code}).",
        ) from exc
    data = _load_document(text)
    if data is None:
        raise ConfigError(None, _ROOT_KEYS_HINT)
    if not isinstance(data, Mapping):
        raise ConfigError(None, f"{_ROOT_KEYS_HINT}, recebido {_type_label(data)}")
    return data


def _load_document(text: str) -> object:
    # ``yaml.safe_load`` mantém apenas o último valor de uma chave declarada duas
    # vezes, então a repetição — o mesmo tipo de erro de digitação que a rejeição
    # de chave desconhecida existe para revelar — é detectada antes, sobre o
    # grafo de nós do ``yaml.compose``. O ``safe_load`` continua sendo o único
    # construtor de valores: um documento com tag Python é recusado por ele.
    try:
        _reject_repeated_keys(yaml.compose(text))
        return yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(None, _yaml_problem(exc)) from exc
    except RecursionError as exc:
        # Aninhamento além do limite do interpretador esgota a pilha do próprio
        # PyYAML: continua sendo configuração inválida, com uma linha, e não um
        # defeito do programa.
        raise ConfigError(
            None,
            "YAML inválido: aninhamento profundo demais para ser lido.",
        ) from exc


def _reject_repeated_keys(node: yaml.Node | None) -> None:
    # O ``yaml.compose`` devolve um grafo, não uma árvore: a âncora ``&x``
    # reaproveita o mesmo nó em cada apelido ``*x`` e um apelido pode voltar a um
    # nó que ainda está sendo percorrido. O conjunto de nós já visitados encerra
    # a busca no ciclo e impede que uma âncora repetida multiplique o trabalho;
    # a repetição de chave é propriedade do próprio mapeamento, então continua
    # sendo encontrada na primeira visita.
    _walk_repeated_keys(node, set())


def _walk_repeated_keys(node: yaml.Node | None, visited: set[int]) -> None:
    # Chave composta (``[a, b]: x``) chega como ``SequenceNode`` e não é
    # comparada; o documento vazio compõe para ``None``.
    if node is None or id(node) in visited:
        return
    visited.add(id(node))
    if isinstance(node, yaml.MappingNode):
        seen: set[str] = set()
        for key_node, value_node in node.value:
            if isinstance(key_node, yaml.ScalarNode):
                if key_node.value in seen:
                    line = key_node.start_mark.line + 1
                    name = _render_document_name(key_node.value)
                    raise ConfigError(
                        None,
                        f"chave '{name}' repetida na linha {line}.",
                    )
                seen.add(key_node.value)
            _walk_repeated_keys(value_node, visited)
    elif isinstance(node, yaml.SequenceNode):
        for item in node.value:
            _walk_repeated_keys(item, visited)


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


def _has_control_character(value: str) -> bool:
    """Diz se o texto traz C0 (0x00-0x1f), DEL (0x7f) ou C1 (0x80-0x9f).

    Esses caracteres não são espaço: passariam pela validação e seriam escritos
    crus nos cabeçalhos MIME e no resumo de ``check-config``, onde um ``\\n``
    forja uma linha nova. Todo caractere imprimível, acentuado ou não, passa.

    Args:
        value: texto que veio do documento de configuração.

    Returns:
        ``True`` quando há ao menos um caractere de controle.
    """
    return any(
        ord(character) < 0x20 or 0x7F <= ord(character) <= 0x9F
        for character in value
    )


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
    # Um C0/DEL/C1 que não é espaço passa por ``parseaddr`` e chegaria cru ao
    # cabeçalho MIME e ao resumo do ``check-config``, que não escapam controle.
    if _has_control_character(candidate):
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
    # Um espaço em volta é removido pelo ``strip`` acima e não chega ao resumo do
    # ``check-config``; um caractere de controle no meio dividiria a linha dele.
    if _has_control_character(host):
        raise ConfigError(
            "smtp.host",
            "host inválido (esperado o nome do servidor SMTP, sem caracteres "
            "de controle).",
        )
    return SmtpConfig(host=host, port=_parse_port(raw.get("port", _AUSENTE)))


def _parse_port(value: object) -> int:
    if value is _AUSENTE:
        return _DEFAULT_SMTP_PORT
    if value is None:
        raise ConfigError(
            "smtp.port",
            "chave presente sem valor (esperado um inteiro entre 1 e 65535; "
            f"remova a chave para usar o padrão {_DEFAULT_SMTP_PORT}).",
        )
    # bool é subclasse de int: `port: true` não pode virar a porta 1.
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
        raise ConfigError(
            "smtp.port",
            f"valor '{value}' não é um inteiro entre 1 e 65535.",
        )
    return value


def _parse_timezone(root: Mapping[str, object]) -> tuple[str, ZoneInfo]:
    raw = root.get("timezone", _AUSENTE)
    if raw is _AUSENTE:
        name = _DEFAULT_TIMEZONE
    elif raw is None:
        raise ConfigError(
            "timezone",
            "chave presente sem valor (esperado um nome IANA, como "
            f"'{_DEFAULT_TIMEZONE}'; remova a chave para usar o padrão).",
        )
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
    except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
        raise ConfigError(
            "timezone",
            f"fuso horário '{name}' não reconhecido "
            "(esperado um nome IANA, como 'America/Sao_Paulo').",
        ) from exc
    return name, zone


def _required_variable(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name)
    if value is None or value == "":
        raise ConfigError(name, "não definida", credencial=True)
    if not value.strip():
        raise ConfigError(name, "vazia", credencial=True)
    return value
