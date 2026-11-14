# finmail

`finmail` é um trabalho de linha de comando em Python 3.12 que consulta o Yahoo Finance, monta um resumo
diário de mercado em HTML e texto simples e o entrega por e-mail, via SMTP com STARTTLS, a todos os
destinatários configurados. O operador agenda uma execução por dia no `cron`: o programa não fica
residente, não mantém estado entre execuções e registra tudo em `stdout`, sinalizando o desfecho pelo
código de saída.

## O que ele faz

Uma execução de `python -m finmail run`:

1. lê e valida o arquivo de configuração (`config.yaml`), com 1 a 50 ativos e 1 a 20 destinatários;
2. consulta cada ativo **em sequência, na ordem da configuração**, com uma única requisição de barras
   diárias por ativo;
3. deriva de cada série o último fechamento, a variação contra o fechamento anterior, o volume, a mínima e a
   máxima de 52 semanas, a moeda, o nome e a data do último pregão;
4. monta um assunto, um corpo texto e um corpo HTML com uma linha por ativo;
5. envia **uma única mensagem**, endereçada a todos os destinatários, e termina com o código de saída
   correspondente ao desfecho.

Um ativo sem dados não interrompe a execução: ele mantém a posição na lista e vira uma linha com
`dados indisponíveis` (ver a seção **Execução parcial**).

## Requisitos

- **Python 3.12** em Linux ou macOS.
- **HTTPS de saída** para o Yahoo Finance (uma requisição por ativo).
- **SMTP de saída com STARTTLS** para o servidor configurado pelo operador.
- As variáveis de ambiente `SMTP_USERNAME` e `SMTP_PASSWORD` para o envio (ver
  seção **Credenciais**).

### Origem e atraso dos dados

O Yahoo Finance é uma fonte **não oficial**, sem API documentada nem garantia de estabilidade: o formato
dos dados pode mudar sem aviso e os valores podem estar atrasados. Todo o contato com essa fonte está
isolado em um único módulo do pacote (`finmail/quotes.py`), e os dois corpos do e-mail carregam sempre o
rodapé fixo:

```text
Dados fornecidos pelo Yahoo Finance. Os valores podem apresentar atraso.
```

## Instalação

```bash
python3.12 -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
```

O bloco acima cria o ambiente virtual, ativa-o e instala o pacote em modo editável com as dependências de
desenvolvimento. A instalação está concluída quando o pacote responde e o `--help` lista os dois
subcomandos, terminando com código de saída `0`:

```bash
python -m finmail --help
```

```text
usage: python -m finmail [-h] [--config CAMINHO] {run,check-config} ...

Resumo diário de mercado por e-mail, com cotações do Yahoo Finance.

options:
  -h, --help          show this help message and exit
  --config CAMINHO    caminho do arquivo de configuração (padrão:
                      ./config.yaml)

comandos:
  {run,check-config}
    run               consulta os ativos, monta o resumo e envia o e-mail
    check-config      valida a configuração sem acessar a rede
```

Se esse `--help` não listar `run` e `check-config`, a instalação não foi concluída; ver a seção
**Solução de problemas**.

As dependências são fixadas em versões exatas no `pyproject.toml`, e nenhuma outra é declarada:

| Grupo | Pacotes |
|---|---|
| Runtime | `yfinance` 0.2.66, `pandas` 2.3.3, `PyYAML` 6.0.3, `Jinja2` 3.1.6 |
| Desenvolvimento (`[dev]`) | `pytest` 8.4.2, `aiosmtpd` 1.4.6 |

`pandas` entra apenas para segurar a faixa `<3` de que o `yfinance` depende; nenhum módulo de `finmail` a
importa.

**Não existe console script.** O pacote não instala nenhum executável `finmail` no `PATH`: a única forma de
invocação é `python -m finmail`, e é ela que aparece em todos os exemplos deste documento.

## Configuração

Copie o modelo e ajuste os valores:

```bash
cp config.example.yaml config.yaml
```

O caminho do arquivo é escolhido com a opção global `--config`, cujo padrão é `./config.yaml`.

### Chaves

| Chave | Tipo e padrão | Obrigatória | Regra de validação |
|---|---|---|---|
| `tickers` | lista de strings, sem padrão | sim | 1 a 50 itens; cada item é um texto não vazio depois de remover os espaços das pontas; sem duplicatas, comparadas sem diferenciar maiúsculas de minúsculas (`aapl` e `AAPL` são o mesmo ativo); a ordem é preservada no relatório |
| `recipients` | lista de strings, sem padrão | sim | 1 a 20 endereços de e-mail puros, sem nome de exibição (`usuario@exemplo.com`, nunca `Nome <usuario@exemplo.com>`) |
| `sender` | string, sem padrão | sim | um endereço de e-mail puro e válido, sem nome de exibição |
| `smtp.host` | string, sem padrão | sim | servidor SMTP de saída, não vazio |
| `smtp.port` | inteiro, padrão `587` | não | inteiro de 1 a 65535 |
| `timezone` | string, padrão `America/Sao_Paulo` | não | nome IANA resolvível pelo `zoneinfo`; governa apenas a data do assunto, não a data de cada ativo |

### Exemplo mínimo válido

```yaml
tickers:
  - AAPL
  - PETR4.SA

recipients:
  - usuario1@exemplo.com
  - usuario2@exemplo.com

sender: finmail@exemplo.com

smtp:
  host: smtp.exemplo.com
  port: 587

timezone: America/Sao_Paulo
```

É a configuração efetiva de `config.example.yaml`, que traz ainda os comentários de cada chave.

### Como o arquivo é lido

- A leitura é feita com `yaml.safe_load`, em UTF-8.
- **Chaves desconhecidas são rejeitadas em qualquer nível**, com uma mensagem que nomeia a chave e lista as
  válidas naquele nível. É assim que uma credencial escrita por engano no `config.yaml` é recusada, em vez
  de virar uma "chave ausente" confusa.
- A validação termina **antes de qualquer acesso à rede** e para no primeiro campo inválido, na ordem
  `tickers` → `recipients` → `sender` → `smtp.host` → `smtp.port` → `timezone`.
- Arquivo ausente, YAML sintaticamente inválido ou conteúdo que não é um mapeamento produzem uma linha de
  erro que nomeia o problema, por exemplo
  `Erro de configuração: arquivo 'config.yaml' não encontrado.`, e código de saída `3`.

## Credenciais

Existem apenas dois segredos, e eles vêm **exclusivamente do ambiente do processo**: nunca do `config.yaml`,
nunca de um arquivo versionado no repositório.

```bash
export SMTP_USERNAME='usuario@exemplo.com'
export SMTP_PASSWORD='sua-senha-aqui'
```

Para uso interativo, o modelo `.env.example` pode ser copiado e carregado:

```bash
cp .env.example .env
set -a; . ./.env; set +a
```

O `.env` real é ignorado pelo `.gitignore` e **nunca deve ser versionado**; apenas o modelo, com valores
fictícios, é mantido no repositório.

O que a implementação garante:

- a senha é usada apenas como argumento do `login()` do SMTP;
- a senha não aparece em linha de log, em `stdout`, em mensagem de exceção nem no corpo do e-mail;
- a representação do objeto de credenciais não expõe o valor;
- a suíte de testes usa um par fictício, nunca uma credencial real.

`run` exige as duas variáveis; `run --dry-run` e `check-config` não as exigem (ver a seção
**Referência de comandos**).

## Referência de comandos

Existem três invocações, e nenhuma outra.

| Invocação | Lê | Rede (Yahoo Finance) | Conexão SMTP | Imprime |
|---|---|---|---|---|
| `python -m finmail run` | `config.yaml` e as duas variáveis de credencial | sim, uma requisição por ativo | sim, uma mensagem | linhas de log |
| `python -m finmail run --dry-run` | apenas `config.yaml` (não lê credencial alguma) | sim, uma requisição por ativo | **não** | linhas de log, o assunto e o corpo texto |
| `python -m finmail check-config` | apenas `config.yaml`, mais a **presença** das duas variáveis | não | não | o resumo da configuração validada |

- **`run`** valida o arquivo inteiro, exige `SMTP_USERNAME` e `SMTP_PASSWORD`, consulta todos os ativos e
  envia uma única mensagem a todos os destinatários. Faltando uma das variáveis, a execução termina com
  código `3` e a linha `Credencial ausente: variável SMTP_PASSWORD não definida.`
- **`run --dry-run`** valida o arquivo do mesmo modo, **não lê as credenciais** e **não abre conexão
  SMTP**: consulta os ativos, imprime o assunto e o corpo texto em `stdout` e termina. É a forma de conferir
  o relatório sem enviar nada.
- **`check-config`** valida o arquivo, não abre conexão de nenhum tipo e não exige credencial. Imprime o
  resumo, incluindo `definido` ou `não definido` para cada variável de credencial:

  ```text
  Configuração válida.
  tickers: 2
  ativos: AAPL, PETR4.SA
  recipients: 2
  destinatários: usuario1@exemplo.com, usuario2@exemplo.com
  sender: finmail@exemplo.com
  smtp.host: smtp.exemplo.com
  smtp.port: 587
  timezone: America/Sao_Paulo
  SMTP_USERNAME: não definido
  SMTP_PASSWORD: não definido
  ```

### Opção global `--config`

| Item | Comportamento |
|---|---|
| Valor | `--config CAMINHO`, com caminho relativo ou absoluto |
| Padrão | `./config.yaml` |
| Posição | aceito antes ou depois do nome do subcomando |
| Repetição | a ocorrência mais à direita é a que vale |

```bash
python -m finmail check-config --config ./config.yaml
python -m finmail --config /home/operador/finmail/config.yaml run
python -m finmail run --dry-run --config /home/operador/finmail/config.yaml
```

Não existem outras opções — nem `--verbose`, nem `--quiet`, nem seleção de ativos pela linha de comando — e
não há apelidos de comando. Um comando ou uma opção desconhecida produz a mensagem de uso em `stderr` e
código de saída `3`.

## Saída de `run --dry-run`

As linhas de log vêm primeiro; depois o bloco de dados, com o assunto, o delimitador `--- corpo texto ---` e
o corpo texto completo. O corpo HTML não é impresso, para manter a saída legível no terminal.

```text
2026-11-13T20:29:59-0300 INFO finmail: Comando 'run' com a configuração '/home/operador/finmail/config.yaml' (2 ativos).
2026-11-13T20:29:59-0300 INFO finmail: Imprimindo o resumo de 2 ativos no terminal (--dry-run); nenhum e-mail foi enviado.
Assunto: Resumo de mercado – 2026-11-13
--- corpo texto ---
Resumo de mercado – 2026-11-13
Ativo | Nome | Moeda | Data | Fechamento | Variação | Variação % | Volume | Mín. 52s | Máx. 52s | Estado
AAPL | Apple Inc. | USD | 2026-11-13 | 356,27 | +1,39 | +0,39% | 39.099.985 | 243,42 | 360,83 | ok
PETR4.SA | Petróleo Brasileiro S.A. - Petrobras | BRL | 2026-11-13 | 45,30 | +0,60 | +1,34% | 23.431.100 | 29,53 | 59,79 | ok
Dados fornecidos pelo Yahoo Finance. Os valores podem apresentar atraso.
```

O assunto é `Resumo de mercado – AAAA-MM-DD`, com travessão EN (U+2013), e a data é a **data da execução** no
fuso de `timezone` — não a data do último pregão, que aparece na coluna `Data` de cada linha.

## Conteúdo do e-mail

A mensagem sai como `multipart/alternative`, com a parte `text/plain` primeiro e a parte `text/html` por
último, e traz os cabeçalhos `From`, `To`, `Subject`, `Date` e `Message-ID`.

As duas partes têm as mesmas onze colunas, na mesma ordem:

```text
Ativo | Nome | Moeda | Data | Fechamento | Variação | Variação % | Volume | Mín. 52s | Máx. 52s | Estado
```

- **Formatação pt-BR**: vírgula decimal e ponto de milhar (`356,27`, `39.099.985`).
- **Valor ausente**: `n/d` em qualquer campo que o Yahoo Finance não tenha informado.
- **Sinal da variação**: `+` quando positiva, `-` quando negativa, sem sinal quando zero.
- **Cores no HTML**: verde para variação positiva, vermelho para negativa e cinza para zero ou ausente. A
  cor vem sempre acompanhada do sinal, de modo que a informação não depende só de cor.
- **Coluna `Estado`**: `ok` quando o ativo retornou dados; `dados indisponíveis: <motivo>` quando não
  retornou.
- **Rodapé** em ambos os corpos:
  `Dados fornecidos pelo Yahoo Finance. Os valores podem apresentar atraso.`

## Códigos de saída

| Código | Condição exata |
|---|---|
| `0` | Sucesso: relatório enviado por `run` (ou impresso por `--dry-run`), inclusive quando um ou mais ativos falharam mas ao menos um retornou dados; `check-config` com arquivo válido |
| `1` | Exceção inesperada, não classificada em nenhum outro caminho |
| `2` | Todos os ativos configurados falharam: nenhum relatório é montado e nenhuma mensagem é enviada |
| `3` | Configuração inválida ou ausente; `SMTP_USERNAME`/`SMTP_PASSWORD` ausente em `run`; erro de uso da linha de comando |
| `4` | Falha de SMTP que persiste depois das tentativas, incluindo falha de autenticação, que não é repetida |

O interpretador da linha de comando encerra com `3`, e não com o `2` que o `argparse` usaria por padrão, para
que `2` signifique exatamente uma coisa: nenhum ativo com dados. `--help` continua saindo com `0`.

## Streams e formato de log

- **`stdout`**: todas as linhas de log (`INFO`, `WARNING`, `ERROR`) e toda a saída de dados (o resumo de
  `check-config`, o assunto e o corpo texto de `--dry-run`).
- **`stderr`**: apenas a linha única de diagnóstico de falha fatal e as mensagens de uso do `argparse`.

Formato de cada linha de log:

```text
%(asctime)s %(levelname)s %(name)s: %(message)s
```

com a data em ISO-8601 no fuso de `timezone`, por exemplo:

```text
2026-11-13T20:29:55-0300 INFO finmail: Comando 'check-config' com a configuração 'config.yaml' (2 ativos).
```

Os níveis usados são `INFO` (progresso), `WARNING` (ativo com falha em execução parcial) e `ERROR` (falha
fatal). O log das bibliotecas `yfinance`, `peewee`, `urllib3` e `curl_cffi` fica em `ERROR`, para que o ruído
delas não se confunda com o log do produto, e não existe opção de nível de log.

Consequência prática para o operador:

```bash
python -m finmail run --config ./config.yaml >> finmail.log 2>> finmail.err
```

Tudo o que aconteceu fica em `finmail.log`, e a causa de uma falha fica sempre em `finmail.err`.

## Agendamento com `cron`

```bash
0 7 * * 1-5 SMTP_USERNAME='usuario@exemplo.com' SMTP_PASSWORD='sua-senha-aqui' /home/operador/finmail/.venv/bin/python -m finmail run --config /home/operador/finmail/config.yaml >> /home/operador/finmail/finmail.log 2>> /home/operador/finmail/finmail.err
```

Por que a entrada tem essa forma:

- **Caminhos absolutos** para o interpretador e para `--config`: o diretório de trabalho do agendador não é o
  do projeto, e `./config.yaml` seria procurado no lugar errado.
- **As duas variáveis na própria entrada**: o ambiente mínimo do `cron` não executa as linhas de `export` do
  shell interativo nem carrega o `.env`. Sem elas, `run` termina em código `3` com
  `Credencial ausente: variável SMTP_PASSWORD não definida.`
- **`>>` e `2>>`**: separam o log da execução do diagnóstico fatal, como descrito acima.

Como a entrada passa a conter a senha, o arquivo de `crontab` precisa ser protegido: edite-o apenas com
`crontab -e` e confira as permissões do spool do usuário. Se preferir não deixar a senha na crontab,
coloque-a em um script `chmod 700` chamado pela entrada.

O código de saída é visível para o `cron`: com `MAILTO` definido, ou com um script que inspecione `$?`, uma
execução com código diferente de `0` pode gerar alerta.

## Comportamento de entrega

- **Uma única mensagem por execução**, endereçada a todos os destinatários no cabeçalho `To`. Não há envio
  por destinatário nem montagem de uma segunda mensagem.
- **STARTTLS é obrigatório**: a ordem é `ehlo` → `starttls` → `ehlo` → `login` → envio, com verificação de
  certificado e de hostname pelo contexto padrão do sistema. Não existe caminho para texto claro.
- **Até três tentativas de entrega** da **mesma** mensagem, com esperas de 5 s e 30 s entre elas.
- **Falha de autenticação, servidor sem STARTTLS e certificado inválido não são repetidos**: a execução
  termina com código `4` e uma linha que nomeia host, porta e código de resposta, por exemplo
  `Falha ao enviar o e-mail para smtp.exemplo.com:587: autenticação recusada (código 535).`
- **A coleta de cotações tem uma política própria**: até três tentativas com esperas de 2 s, apenas para
  falha de rede, tempo esgotado e limite de requisições do Yahoo. Erros determinísticos — ativo
  inexistente, ausência de preços no período, série vazia, campo essencial ausente e certificado TLS
  inválido — não são repetidos.

## Execução parcial

Quando um ativo não retorna dados:

- a linha dele **mantém a posição** que o ticker ocupa na configuração;
- a coluna `Estado` mostra `dados indisponíveis: <motivo>`, com o motivo em português;
- as nove colunas de mercado (`Nome`, `Moeda`, `Data`, `Fechamento`, `Variação`, `Variação %`, `Volume`,
  `Mín. 52s`, `Máx. 52s`) mostram `n/d`, porque não há dado de mercado a exibir;
- o restante do relatório não muda, e a variação da linha fica no estilo neutro;
- a execução continua e termina com **código `0`**, registrando um `WARNING` por ativo com falha.

Só a falha total — nenhum ativo com dados — muda o desfecho: aí nenhum relatório é montado, nenhuma
mensagem é enviada, um `ERROR` registra a contagem de falhas e o código de saída é `2`.

```text
BTC-USD | n/d | n/d | n/d | n/d | n/d | n/d | n/d | n/d | n/d | dados indisponíveis: falha de rede após 3 tentativas
```

## Suíte de testes offline

Com o ambiente virtual ativo e o pacote instalado em modo editável, rode a suíte a partir da raiz do
repositório:

```bash
pytest
```

A suíte passa integralmente **sem acesso à rede e sem nenhuma variável de credencial definida**. Isso é
garantido por três mecanismos, todos em `tests/`, sem qualquer costura no código do produto:

- `tests/fixtures/stub/yfinance/`: um dublê de `yfinance`, colocado à frente do `site-packages` no
  `PYTHONPATH` dos subprocessos que executam `python -m finmail`;
- `tests/fixtures/guard/sitecustomize.py`: uma guarda de sockets que recusa qualquer destino fora de
  loopback e é carregada pelo próprio Python na inicialização dos subprocessos;
- um servidor `aiosmtpd` local, com STARTTLS e autenticação, que usa o certificado descartável de
  `tests/fixtures/tls/`.

O pacote `tests/` é a evidência dos critérios de aceitação 1 a 6 e 8. O caminho real da CLI é exercitado em
`tests/test_end_to_end.py`, com um subprocesso `python -m finmail` e asserções sobre o que o servidor SMTP
recebeu; os demais arquivos cobrem um componente cada: `test_config.py`, `test_quotes.py`, `test_report.py`,
`test_mailer.py` e `test_cli.py`.

## Verificação manual

As duas verificações abaixo dependem da rede e do Yahoo Finance, por isso ficam **fora** da suíte
automatizada.

### Execução real com `AAPL` e `PETR4.SA`

Com uma configuração que contenha os dois ativos (como `config.example.yaml`):

```bash
python -m finmail run --dry-run --config ./config.yaml
```

Confirme que as duas linhas aparecem com `Fechamento` numérico e `Data` não vazia, e que o rodapé fecha o
corpo:

```text
AAPL | Apple Inc. | USD | 2026-11-13 | 356,27 | +1,39 | +0,39% | 39.099.985 | 243,42 | 360,83 | ok
PETR4.SA | Petróleo Brasileiro S.A. - Petrobras | BRL | 2026-11-13 | 45,30 | +0,60 | +1,34% | 23.431.100 | 29,53 | 59,79 | ok
```

A conferência é manual porque depende do Yahoo Finance: um `n/d` em `Fechamento` ou uma `Data` vazia indica
problema na fonte de dados naquele momento, não no produto.

### Orçamento de desempenho: 50 ativos em até 120 s no p95

Com uma configuração de 50 ativos, execute pelo menos 20 vezes e registre o tempo de parede de cada execução:

```bash
: > tempos.txt   # zera a amostra: o p95 mede apenas as 20 execuções deste laço
for i in $(seq 1 20); do
  inicio=$(date +%s)
  python -m finmail run --dry-run --config ./config-50.yaml > /dev/null
  echo $(( $(date +%s) - inicio )) >> tempos.txt
  sleep 5
done
sort -n tempos.txt | awk '{t[NR]=$1} END {printf "p95: %d s\n", t[int(NR*0.95)]}'
```

O 95º percentil apurado precisa ser de no máximo 120 s. Espaçe as execuções para não coincidir com as
esperas de 2 s das novas tentativas de cotação nem com um bloqueio temporário do Yahoo. A ordem de grandeza
esperada é de 0,11 a 0,30 s por ativo, ou seja cerca de 7 s para 50 ativos.

## Solução de problemas

| Sintoma | Causa provável e o que fazer |
|---|---|
| `No module named finmail` ao executar `python -m finmail` | O interpretador em uso é outro, ou o ambiente virtual não está ativo: ative-o com `. .venv/bin/activate` e repita `pip install -e ".[dev]"` a partir da raiz do repositório. Em `cron`, use o caminho absoluto do interpretador do `.venv`, como no exemplo de agendamento. |
| Código de saída `3` | A mensagem nomeia o campo e o motivo, por exemplo `Erro de configuração em 'recipients': lista ausente ou vazia (esperado de 1 a 20 endereços).` Corrija o `config.yaml` no campo citado. |
| `Erro de configuração: arquivo 'X' não encontrado.` | O `--config` aponta para um caminho que não existe; em `cron`, use caminho absoluto. |
| `Credencial ausente: variável SMTP_PASSWORD não definida.` | O ambiente do processo não tem a variável. Em `cron`, declare as duas na própria entrada. |
| Código de saída `4` | A transação SMTP falhou depois das tentativas; a linha nomeia host, porta e código de resposta. Confira host, porta, credenciais e se o servidor oferece STARTTLS. Falha de autenticação não é repetida. |
| Código de saída `2` | Nenhum ativo retornou dados. Confira os símbolos (ações da B3 terminam em `.SA`, criptomoedas usam o par com hífen) e a conectividade com o Yahoo Finance; nenhum e-mail é enviado nesse caso. |
| Linha com `dados indisponíveis` | Problema na fonte de dados apenas para aquele símbolo; o motivo aparece na coluna `Estado` e os demais ativos seguem normalmente. |
| Código de saída `1` | Defeito inesperado do programa; o log em `stdout` traz o `ERROR` com o traceback e o `stderr` traz uma única linha de diagnóstico. |
