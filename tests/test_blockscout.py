"""O coletor do Blockscout: o que ele promete e o que ele nao pode prometer."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def test_uma_escrita_travada_nao_mata_a_coleta(monkeypatch):
    """Medido em 2026-09-03: a LLY paginou 20 minutos e perdeu tudo.

    Quatorze processos escrevem neste SQLite. O `busy_timeout` de 30s do
    engine nao cobre uma escrita longa dos permanentes, e o lote e
    idempotente — repetir e seguro, desistir e que nao e.
    """
    from sqlalchemy.exc import OperationalError

    from defime.ingest import blockscout as bs

    chamadas = {"n": 0}

    class _Falso:
        def __enter__(self):
            chamadas["n"] += 1
            if chamadas["n"] < 3:
                raise OperationalError("INSERT", {}, Exception(
                    "database is locked"))
            return object()

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(bs, "session_scope", _Falso)
    monkeypatch.setattr(bs, "_upsert_ignore", lambda *a: 7)
    monkeypatch.setattr(bs.time, "sleep", lambda s: None)

    assert bs._gravar_com_espera([{"x": 1}]) == 7
    assert chamadas["n"] == 3


def test_erro_que_nao_e_trava_sobe_na_hora(monkeypatch):
    """Esperar por um erro de schema so atrasaria a noticia dele."""
    from sqlalchemy.exc import OperationalError

    from defime.ingest import blockscout as bs

    class _Falso:
        def __enter__(self):
            raise OperationalError("INSERT", {}, Exception("no such table"))

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(bs, "session_scope", _Falso)
    monkeypatch.setattr(bs.time, "sleep", lambda s: pytest.fail("esperou"))
    with pytest.raises(OperationalError):
        bs._gravar_com_espera([{"x": 1}])


def test_tx_index_e_sentinela_e_nao_zero():
    """Zero seria "primeira transacao do bloco" — uma afirmacao falsa.

    O Blockscout nao expoe `transactionIndex`; -1 nao se confunde com
    posicao valida e nada no projeto calcula com ele.
    """
    from defime.ingest.blockscout import TX_INDEX_AUSENTE
    assert TX_INDEX_AUSENTE == -1


def test_o_coletor_nao_grava_marca_de_cobertura():
    """`BackfillRange` significa "esta faixa foi varrida por inteiro".

    Paginar do topo para tras nao garante isso — a pagina pode acabar no meio
    de um bloco. Gravar a marca aqui reintroduziria o bug de 01/09, em que
    faixa marcada como completa escondia Mint que a chain tinha e o banco nao.
    """
    import ast

    caminho = (Path(__file__).resolve().parents[1] / "src" / "defime" /
               "ingest" / "blockscout.py")
    arvore = ast.parse(caminho.read_text(encoding="utf-8"))
    nomes = {n.id for n in ast.walk(arvore) if isinstance(n, ast.Name)}
    nomes |= {n.attr for n in ast.walk(arvore) if isinstance(n, ast.Attribute)}
    assert "BackfillRange" not in nomes
