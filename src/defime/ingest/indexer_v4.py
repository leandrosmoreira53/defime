"""Indexer de Uniswap V4. Mesmo banco do V3, coletor diferente por necessidade.

**Por que nao da para reusar o `PoolIndexer`.** Ele pergunta ao no *"me da os
logs deste endereco"*. No V4 esse endereco e o mesmo `PoolManager` para todas as
pools da chain — pedir por endereco devolveria a chain inteira. A identidade da
pool e o `PoolId`, um `bytes32` que vem no **topic 1**, entao o filtro tem que
ser por topico, nao por endereco.

    V3   eth_getLogs(address=pool,          topics=[[Swap, Mint, Burn, ...]])
    V4   eth_getLogs(address=PoolManager,   topics=[[Swap, ...], poolId])
                                                                 ^^^^^^^^
Tudo o mais e igual, e de proposito: a coluna `pool_address` do inviolavel 8
recebe os 66 caracteres do `PoolId` em vez dos 42 de um endereco (ela sempre foi
`String(66)`, nunca binario de 20 bytes), e `pool_events`, `blocks` e
`backfill_ranges` sao as MESMAS tabelas. Consulta que hoje soma volume por pool
continua funcionando sem saber que existe V4.

**O que este coletor NAO resolve, e nao deve fingir que resolve:**

  - **Nao existe evento `Collect` no V4.** Coletar fee vira `ModifyLiquidity`
    com delta zero, e o valor sai pelo flash accounting (`take`/`settle`), que
    nao emite log da pool. A reconstrucao de fee realizada que o `lifecycle.py`
    faz no V3 **nao se traduz** — quem quiser fee de posicao V4 vai precisar de
    outra fonte.
  - **`Mint`/`Burn` viram um evento so** com sinal (`liquidityDelta`). Quem
    consumir isso tem que olhar o sinal, nao o nome.
  - **`Initialize` nao foi achado** em 6M blocos, entao `tickSpacing` e o
    endereco do **hook** continuam desconhecidos. Sem o hook nao da para
    afirmar que a taxa e estatica — e a medicao ja mostrou taxa observada
    diferente da declarada em tres pools.

**A taxa se mede, nao se le.** Cada `Swap` do V4 carrega o campo `fee` aplicado
naquele swap. `taxa_medida()` em `chain/abi_v4.py` resume a serie; o replay deve
usar ela e nao o tier do agregador.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from ..chain.abi_v4 import INDEXED_EVENTS, POOL_MANAGER, TOPIC0, decode_log
from ..chain.rpc import RpcClient
from ..config import settings
from ..db.models import BackfillRange, Block, IndexerState, PoolEvent
from ..db.registro import batimento_periodico
from ..db.session import session_scope
from ..logging import get_logger

log = get_logger(__name__)


@dataclass
class StatsV4:
    logs: int = 0
    inserted: int = 0
    blocks: int = 0
    chunks: int = 0
    rpc_calls: int = 0
    seconds: float = 0.0
    errors: list[str] = field(default_factory=list)

    def resumo(self) -> dict:
        return {"logs": self.logs, "inseridos": self.inserted,
                "blocos": self.blocks, "chunks": self.chunks,
                "rpc": self.rpc_calls, "segundos": round(self.seconds, 1),
                "erros": len(self.errors)}


def _upsert_ignore(sess, modelo, linhas: list[dict]) -> int:
    if not linhas:
        return 0
    sess.execute(sqlite_insert(modelo).on_conflict_do_nothing(), linhas)
    return len(linhas)


class PoolIndexerV4:
    """Coleta os eventos de UMA pool V4, identificada pelo `PoolId`.

    O `pool_id` e gravado em `pool_events.pool_address`. Nao e gambiarra: a
    coluna e a chave de particionamento do projeto e o que ela guarda e "a
    identidade da pool nesta DEX". No V3 isso e um endereco, no V4 e um id.
    """

    def __init__(self, pool_id: str, rpc: RpcClient | None = None,
                 manager: str | None = None):
        if not (pool_id or "").startswith("0x") or len(pool_id) != 66:
            raise ValueError(
                f"pool_id do V4 tem que ser bytes32 com 0x (66 chars), "
                f"recebi {len(pool_id or '')}: {pool_id!r}")
        self.pool_id = pool_id.lower()
        self.manager = (manager or POOL_MANAGER).lower()
        self.rpc = rpc or RpcClient()
        self.cfg = settings()
        self.topics = [[TOPIC0[n] for n in INDEXED_EVENTS], self.pool_id]

    # ------------------------------------------------------------------ io
    def _persistir(self, logs: list, stats: StatsV4, lo: int, hi: int) -> None:
        numeros = {int(x["blockNumber"], 16) for x in logs}
        heads = self.rpc.block_timestamps(sorted(numeros)) if numeros else {}

        eventos, blocos = [], []
        for entry in logs:
            decodificado = decode_log(entry)
            if not decodificado:
                continue                    # o singleton emite de todas as pools
            nome, args = decodificado
            bn = int(entry["blockNumber"], 16)
            head = heads.get(bn)
            if not head:
                stats.errors.append(f"sem header para o bloco {bn}")
                continue
            eventos.append({
                "pool_address": self.pool_id,      # <- o PoolId, nao o manager
                "block_number": bn,
                "log_index": int(entry["logIndex"], 16),
                "tx_hash": entry["transactionHash"],
                "tx_index": int(entry["transactionIndex"], 16),
                "block_hash": entry["blockHash"],
                "ts": head["ts"],
                "event": nome,
                "args": args,
            })
        for bn, head in heads.items():
            blocos.append({"number": bn, "hash": head["hash"],
                           "parent_hash": head["parent_hash"], "ts": head["ts"]})

        with session_scope() as sess:
            _upsert_ignore(sess, Block, blocos)
            n = _upsert_ignore(sess, PoolEvent, eventos)
            sess.add(BackfillRange(pool_address=self.pool_id, from_block=lo,
                                   to_block=hi, logs=len(eventos),
                                   completed_at=int(time.time())))
            sess.merge(IndexerState(stream="backfill_v4",
                                    pool_address=self.pool_id, last_block=hi,
                                    updated_at=int(time.time()),
                                    note=f"chunk {lo}-{hi}"))
        stats.inserted += n
        stats.logs += len(eventos)
        stats.blocks += len(blocos)

    # ------------------------------------------------------------- backfill
    def backfill(self, from_block: int, to_block: int | None = None,
                 quiet: bool = False) -> StatsV4:
        """Varre [from_block, to_block]. Divide o pedaco quando o no reclama.

        A divisao existe porque `log query timed out` e resposta normal em pool
        movimentada — foi o que travou a NVDA no V3 com pedaco de 500k. Aqui ela
        e recursiva ate um piso, em vez de dividir uma vez so.
        """
        cfg = self.cfg
        if to_block is None:
            to_block = self.rpc.block_number() - cfg.confirmations
        stats = StatsV4()
        t0 = time.time()
        cursor = from_block
        while cursor <= to_block:
            hi = min(cursor + cfg.chunk_size - 1, to_block)
            logs = self._buscar(cursor, hi, stats)
            if logs is None:                # nem o piso venceu: registra e anda
                stats.errors.append(f"faixa {cursor}-{hi} nao coletada")
                cursor = hi + 1
                continue
            self._persistir(logs, stats, cursor, hi)
            stats.chunks += 1
            if not quiet and stats.chunks % 20 == 0:
                log.info("backfill_v4_andamento", pool=self.pool_id[:12],
                         ate=hi, **stats.resumo())
            cursor = hi + 1
        stats.seconds = time.time() - t0
        stats.rpc_calls = self.rpc.calls
        if not quiet:
            log.info("backfill_v4_fim", pool=self.pool_id[:12], **stats.resumo())
        return stats

    def _buscar(self, lo: int, hi: int, stats: StatsV4, piso: int = 500):
        """Logs de [lo, hi], dividindo ao meio enquanto o no der timeout."""
        try:
            return self.rpc.get_logs(self.manager, lo, hi, self.topics)
        except Exception as exc:            # noqa: BLE001
            if hi - lo + 1 <= piso:
                stats.errors.append(f"{lo}-{hi}: {str(exc)[:80]}")
                return None
            meio = lo + (hi - lo) // 2
            a = self._buscar(lo, meio, stats, piso)
            b = self._buscar(meio + 1, hi, stats, piso)
            if a is None and b is None:
                return None
            return (a or []) + (b or [])

    # ----------------------------------------------------------------- tail
    def tail_once(self) -> dict:
        cfg = self.cfg
        head = self.rpc.block_number() - cfg.confirmations
        with session_scope() as sess:
            ultimo = sess.get(IndexerState, ("backfill_v4", self.pool_id))
        de = (ultimo.last_block + 1) if ultimo else head - 1000
        if de > head:
            return {"novos": 0, "head": head, "ate": de - 1}
        st = self.backfill(de, head, quiet=True)
        return {"novos": st.inserted, "head": head, "ate": head,
                "erros": len(st.errors)}

    def run_tail(self, interval: float = 5.0, max_iterations: int | None = None,
                 heartbeat_s: float = 900.0) -> None:
        i, acumulado, ultimo_bat = 0, 0, time.time()
        while max_iterations is None or i < max_iterations:
            try:
                out = self.tail_once()
                acumulado += out["novos"]
                if out["novos"]:
                    log.info("tail_v4", pool=self.pool_id[:12], **out)
                if time.time() - ultimo_bat >= heartbeat_s:
                    batimento_periodico(f"tail_v4:{self.pool_id[:10]}",
                                        minimo_s=heartbeat_s, itens=acumulado,
                                        nota=f"head={out['head']}")
                    acumulado, ultimo_bat = 0, time.time()
            except Exception as exc:        # noqa: BLE001
                log.exception("tail_v4_erro", erro=str(exc)[:200])
                batimento_periodico(f"tail_v4:{self.pool_id[:10]}",
                                    minimo_s=heartbeat_s, ok=False,
                                    erro=str(exc)[:300])
            i += 1
            if max_iterations is None or i < max_iterations:
                time.sleep(interval)


def descobrir_pools(rpc: RpcClient | None = None, *, de: int, ate: int,
                    manager: str | None = None) -> dict[str, dict]:
    """`PoolId -> Initialize` das pools criadas na faixa.

    Existe porque hoje o `PoolId` so se consegue de fora (o agregador, a URL da
    Revert). Com `Initialize` vem `tickSpacing`, os dois tokens e — o que mais
    falta — o endereco do **hook**, que decide se a taxa e estatica.
    """
    rpc = rpc or RpcClient()
    mgr = (manager or POOL_MANAGER).lower()
    logs = rpc.get_logs(mgr, de, ate, [[TOPIC0["Initialize"]]])
    saida = {}
    for entry in logs or []:
        d = decode_log(entry)
        if d and d[0] == "Initialize":
            saida[d[1]["pool_id"]] = {
                **d[1], "bloco": int(entry["blockNumber"], 16)}
    return saida


__all__ = ["PoolIndexerV4", "StatsV4", "descobrir_pools"]
