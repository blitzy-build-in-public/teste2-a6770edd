"""Ponto de entrada de ``python -m finmail``.

Executado como módulo, este arquivo delega a :func:`finmail.cli.main` e propaga o
código de saída que ela devolve, de modo que o ``cron`` do operador distinga
sucesso (``0``), defeito inesperado (``1``), nenhum ativo com dados (``2``),
configuração inválida (``3``) e falha de SMTP (``4``).

Importado por outra ferramenta, nada executa: ``main`` só roda sob o guarda
``__name__ == "__main__"``.
"""

from finmail.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
