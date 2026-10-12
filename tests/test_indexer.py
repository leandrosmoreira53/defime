"""Logica do indexer, isolada da rede.

Usa banco SQLite temporario e um RPC falso. Nenhum teste aqui toca a internet.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 't.db').as_posix()}")
    monkeypatch.setenv("DNS_BYPASS", "false")
    from defime import config
    from defime.db import session as sess_mod
    config.settings.cache_clear()
    sess_mod.reset_engine()
    sess_mod.create_all()
    yield
    sess_mod.reset_engine()
    config.settings.cache_clear()


class FakeRpc:
    """RPC de mentira: devolve logs e headers plantados."""

    def __init__(self, logs_por_faixa=None, headers=None):
        self.logs_por_faixa = logs_por_faixa or {}
        self.headers = headers or {}
        self.calls = 0
        self.retries = 0

    def get_logs(self, address, from_block, to_block, topics=None):
        self.calls += 1
        out = []
        for (lo, hi), logs in self.logs_por_faixa.items():
            if lo >= from_block and hi <= to_block:
                out.extend(logs)
        return out

    def block_timestamps(self, numbers, batch_size=None):
        return {n: self.headers[n] for n in numbers if n in self.headers}

    def block_number(self):
        return 1_000_000


def _swap_log(block, log_index):
    from eth_abi import encode as abi_encode
    from defime.chain.abi import TOPIC0
    data = "0x" + abi_encode(
        ["int256", "int256", "uint160", "uint128", "int24"],
        [1000, -2000, 79228162514264337593543950336, 5000, 100]).hex()
    return {
        "blockNumber": hex(block),
        "logIndex": hex(log_index),
        "transactionHash": "0x" + "aa" * 32,
        "transactionIndex": "0x0",
        "blockHash": "0x" + f"{block:064x}",
        "topics": [TOPIC0["Swap"], "0x" + "11" * 32, "0x" + "22" * 32],
        "data": data,
    }


def test_gaps_faixa_vazia_devolve_tudo(db):
    from defime.db.session import session_scope
    from defime.ingest.indexer import PoolIndexer
    idx = PoolIndexer(rpc=FakeRpc())
    with session_scope() as sess:
        assert idx.gaps(sess, 100, 200) == [(100, 200)]


def test_gaps_detecta_buraco_no_meio(db):
    from defime.db.models import BackfillRange
    from defime.db.session import session_scope
    from defime.ingest.indexer import PoolIndexer
    idx = PoolIndexer(rpc=FakeRpc())
    with session_scope() as sess:
        for lo, hi in [(100, 149), (200, 250)]:
            sess.add(BackfillRange(pool_address=idx.pool, from_block=lo,
                                   to_block=hi, logs=0, completed_at=0))
    with session_scope() as sess:
        assert idx.gaps(sess, 100, 250) == [(150, 199)]


def test_gaps_funde_faixas_adjacentes(db):
    from defime.db.models import BackfillRange
    from defime.db.session import session_scope
    from defime.ingest.indexer import PoolIndexer
    idx = PoolIndexer(rpc=FakeRpc())
    with session_scope() as sess:
        for lo, hi in [(100, 149), (150, 199), (180, 260)]:  # adjacente + sobreposta
            sess.add(BackfillRange(pool_address=idx.pool, from_block=lo,
                                   to_block=hi, logs=0, completed_at=0))
    with session_scope() as sess:
        assert idx.covered_ranges(sess) == [(100, 260)]
        assert idx.gaps(sess, 100, 260) == []


def test_backfill_e_idempotente(db):
    """O criterio central da Fase 1: rodar duas vezes nao duplica."""
    from defime.db.models import PoolEvent
    from defime.db.session import session_scope
    from defime.ingest.indexer import PoolIndexer

    logs = [_swap_log(1000, 0), _swap_log(1000, 1), _swap_log(1500, 0)]
    headers = {1000: {"ts": 1700000000, "hash": "0x" + f"{1000:064x}",
                      "parent_hash": "0x" + "00" * 32},
               1500: {"ts": 1700000100, "hash": "0x" + f"{1500:064x}",
                      "parent_hash": "0x" + "00" * 32}}
    rpc = FakeRpc({(1000, 1500): logs}, headers)
    idx = PoolIndexer(rpc=rpc)

    idx.backfill(from_block=900, to_block=2000)
    with session_scope() as sess:
        primeiro = sess.execute(select(func.count()).select_from(PoolEvent)).scalar_one()
    assert primeiro == 3

    # segunda passada: a faixa ja esta coberta, nao deve mudar nada
    idx.backfill(from_block=900, to_block=2000)
    with session_scope() as sess:
        segundo = sess.execute(select(func.count()).select_from(PoolEvent)).scalar_one()
    assert segundo == primeiro

    # terceira: forca reprocesso do mesmo chunk, testando o ON CONFLICT
    from defime.ingest.indexer import BackfillStats
    idx._persist_chunk(logs, BackfillStats(), 900, 2000)
    with session_scope() as sess:
        terceiro = sess.execute(select(func.count()).select_from(PoolEvent)).scalar_one()
    assert terceiro == primeiro


def test_retomada_apos_interrupcao(db):
    """Simula morte no meio: metade indexada, reinicio completa o resto."""
    from defime.db.models import PoolEvent
    from defime.db.session import session_scope
    from defime.ingest.indexer import PoolIndexer

    headers = {n: {"ts": 1700000000 + n, "hash": "0x" + f"{n:064x}",
                   "parent_hash": "0x" + "00" * 32} for n in (1000, 5000)}
    rpc = FakeRpc({(1000, 1000): [_swap_log(1000, 0)],
                   (5000, 5000): [_swap_log(5000, 0)]}, headers)
    idx = PoolIndexer(rpc=rpc)

    idx.backfill(from_block=900, to_block=2000)     # so a primeira metade
    with session_scope() as sess:
        assert sess.execute(select(func.count()).select_from(PoolEvent)).scalar_one() == 1
        assert idx.gaps(sess, 900, 6000) == [(2001, 6000)]

    idx.backfill(from_block=900, to_block=6000)     # reinicio
    with session_scope() as sess:
        assert sess.execute(select(func.count()).select_from(PoolEvent)).scalar_one() == 2
        assert idx.gaps(sess, 900, 6000) == []


def test_rewind_apaga_a_partir_do_bloco(db):
    from defime.db.models import PoolEvent
    from defime.db.session import session_scope
    from defime.ingest.indexer import PoolIndexer

    headers = {n: {"ts": 1700000000 + n, "hash": "0x" + f"{n:064x}",
                   "parent_hash": "0x" + "00" * 32} for n in (1000, 5000)}
    rpc = FakeRpc({(1000, 5000): [_swap_log(1000, 0), _swap_log(5000, 0)]}, headers)
    idx = PoolIndexer(rpc=rpc)
    idx.backfill(from_block=900, to_block=6000)

    idx.rewind(5000)
    with session_scope() as sess:
        restantes = sess.execute(
            select(PoolEvent.block_number)).scalars().all()
    assert restantes == [1000]


def test_nenhum_teste_faz_rede():
    """Guarda explicita: FakeRpc nao tem metodo de rede."""
    assert not hasattr(FakeRpc, "call")
    assert not hasattr(FakeRpc, "eth_call")
