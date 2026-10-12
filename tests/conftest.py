"""Fixtures de teste. A que importa: `banco_temporario`.

**Existe porque a falta dela custou linhas falsas no banco de producao.** Em
2026-08-29 o primeiro `tests/test_registro.py` rodou sem isolamento e escreveu
8 linhas de fixture em `defime.db` — o mesmo banco de onde saem os numeros dos
relatorios. Nada falhou; o dado so ficou la.

Nao e fixture automatica de proposito. Boa parte da suite LE o banco real (os
`checks_*` medem a pool de verdade) e forcar todo teste para um banco vazio
transformaria "medicao reprovou" em "nao havia dado" — que e justamente a
distincao que o documento de regras do projeto manda nunca colapsar. Quem ESCREVE pede a fixture:

    pytestmark = pytest.mark.usefixtures("banco_temporario")
"""

from __future__ import annotations

import sys
from pathlib import Path

# A raiz do repo entra no path: `painel_render` e `painel_coleta` moram la e
# nao em `src/`, porque o painel e script de topo e nao pacote. Sem isto o
# teste de colunas nao importa.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from defime.db import session as S


@pytest.fixture
def banco_temporario(tmp_path, monkeypatch):
    """Redireciona o engine para um SQLite descartavel, e o devolve no fim.

    Mexe no singleton `_engine` porque ele e construido na primeira chamada e
    fica: trocar so a variavel de ambiente nao teria efeito depois que qualquer
    teste anterior ja tivesse aberto o banco real.
    """
    from defime.config import settings
    from defime.db.models import Base

    anterior = (S._engine, S._Session)
    S._engine = S._Session = None
    monkeypatch.setenv("DATABASE_URL",
                       f"sqlite:///{(tmp_path / 'teste.db').as_posix()}")
    settings.cache_clear() if hasattr(settings, "cache_clear") else None
    Base.metadata.create_all(S.get_engine())
    try:
        yield S.get_engine()
    finally:
        S._engine, S._Session = anterior
        if hasattr(settings, "cache_clear"):
            settings.cache_clear()
