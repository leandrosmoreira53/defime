"""`link_pendentes`: liga ao NPM so as transacoes de liquidez que faltam.

As pools observadas (PLTR, QUBT, SGOV) nunca tiveram `npm_link`: em 11/09 a
QUBT tinha 306 `Mint` e ZERO transacoes com evento do NPM, e a pagina dela nao
tinha tabela de posicoes. O job horario delas passou a chamar isto.
"""

import pytest
from eth_utils import keccak

from defime.config import settings
from defime.db.models import PoolEvent
from defime.db.session import session_scope
from defime.ingest import npm_link as NL

pytestmark = pytest.mark.usefixtures("banco_temporario")

POOL = "0x" + "ab" * 20


def _evento(tx, bloco, nome="Mint", endereco=POOL, args=None, li=0):
    return PoolEvent(pool_address=endereco, block_number=bloco, log_index=li,
                     tx_hash=tx, tx_index=0, block_hash="0x" + "00" * 32,
                     ts=1_788_000_000 + bloco, event=nome, args=args or {})


def _recibo_com_increase(tx, token_id=77):
    npm = settings().npm_address.lower()
    t0 = "0x" + keccak(text="IncreaseLiquidity(uint256,uint128,uint256,uint256)").hex()
    return {"logs": [{
        "address": npm, "topics": [t0, "0x" + f"{token_id:064x}"],
        "data": "0x" + f"{10**12:064x}" + f"{5:064x}" + f"{6:064x}",
        "blockNumber": "0x10", "logIndex": "0x1", "transactionHash": tx,
        "transactionIndex": "0x0", "blockHash": "0x" + "00" * 32}]}


class Rpc:
    def __init__(self, recibos):
        self.recibos = recibos
        self.pedidos = []

    def call_batch(self, reqs):
        hs = [p[1][0] for p in reqs]
        self.pedidos += hs
        return [self.recibos.get(h) for h in hs]


def _grava(*evs):
    with session_scope() as s:
        s.add_all(evs)


def test_liga_o_que_falta_e_nao_pede_de_novo_na_rodada_seguinte():
    """A transacao SEM log do NPM (Mint direto na pool) tambem fica marcada:
    sem isso ela seria pedida a cada hora, para sempre."""
    _grava(_evento("0xa1", 10), _evento("0xa2", 11), _evento("0xa2", 11, "Collect", li=1))
    rpc = Rpc({"0xa1": _recibo_com_increase("0xa1"), "0xa2": {"logs": []}})
    r = NL.link_pendentes(POOL, rpc=rpc)
    assert r["pendentes"] == 2 and r["lidos"] == 2 and r["eventos"] == 1
    assert sorted(rpc.pedidos) == ["0xa1", "0xa2"]

    rpc2 = Rpc({})
    r2 = NL.link_pendentes(POOL, rpc=rpc2)
    assert r2["pendentes"] == 0 and rpc2.pedidos == []


def test_recibo_que_falha_nao_e_marcado_e_volta_na_proxima():
    _grava(_evento("0xb1", 10))
    r = NL.link_pendentes(POOL, rpc=Rpc({}))          # recibo None: 429, etc.
    assert r["lidos"] == 0 and r["restam"] == 1
    r2 = NL.link_pendentes(POOL, rpc=Rpc({"0xb1": _recibo_com_increase("0xb1")}))
    assert r2["lidos"] == 1 and r2["eventos"] == 1


def test_o_teto_por_rodada_diz_quanto_sobrou():
    _grava(*[_evento(f"0xc{i}", 10 + i) for i in range(5)])
    rpc = Rpc({f"0xc{i}": {"logs": []} for i in range(5)})
    r = NL.link_pendentes(POOL, limite=3, rpc=rpc)
    assert r["lidos"] == 3 and r["restam"] == 2
    assert NL.link_pendentes(POOL, limite=3, rpc=rpc)["restam"] == 0


def test_transacao_ja_ligada_pelo_npm_link_antigo_nao_e_pedida():
    npm = settings().npm_address.lower()
    _grava(_evento("0xd1", 10), _evento("0xd1", 10, "IncreaseLiquidity", npm,
                                        {"tokenId": 1}))
    rpc = Rpc({})
    assert NL.link_pendentes(POOL, rpc=rpc)["pendentes"] == 0
    assert rpc.pedidos == []


def test_429_encerra_a_rodada_com_o_que_ja_foi_gravado():
    """Em 11/09 lote de 20 recibos levou 429 nos dois RPCs. A rodada tem de
    sair com o que ja gravou e dizer que parou, e nao levantar."""
    _grava(*[_evento(f"0xe{i}", 10 + i) for i in range(12)])

    class Rpc429(Rpc):
        def call_batch(self, reqs):
            if len(self.pedidos) >= 5:
                raise RuntimeError("HTTP 429")
            return super().call_batch(reqs)

    r = NL.link_pendentes(POOL, rpc=Rpc429({f"0xe{i}": {"logs": []}
                                            for i in range(12)}))
    assert r["lidos"] == 5 and r["restam"] == 7 and "429" in r["interrompido"]
