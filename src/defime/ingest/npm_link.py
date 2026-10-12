"""Liga eventos da pool a NFTs especificas, via recibos de transacao.

Problema: o `Mint` da pool traz `owner` = contrato do NPM, nunca o tokenId.
Sem resolver isso nao existe atribuicao de liquidez por posicao — nem a nossa,
nem a dos concorrentes (Fase 4).

Caminho descartado: indexar o NPM inteiro. Ele atende TODAS as pools da chain
(~2.400 IncreaseLiquidity por 250k blocos, vs ~50 na nossa pool) e a varredura
esbarra num limite de RPC que o P5 nao tinha tocado:
`logs matched by query exceeds limit of 10000` — limite por RESULTADO, nao por
span de blocos.

Caminho usado: todo IncreaseLiquidity acontece na MESMA transacao do Mint da
pool. Entao basta buscar os recibos das ~5 mil transacoes que ja mexeram na
liquidez desta pool e extrair de la os logs do NPM. Completo para a nossa pool,
e duas ordens de grandeza mais barato.
"""

from __future__ import annotations

import time

from sqlalchemy import distinct, select

from ..chain.abi import NPM_TOPIC0, decode_log
from ..chain.rpc import RpcClient
from ..config import settings
from ..db.models import PoolEvent
from ..db.session import session_scope
from ..logging import get_logger
from .indexer import _upsert_ignore

log = get_logger(__name__)

EVENTOS_DE_LIQUIDEZ = ("Mint", "Burn", "Collect")


def transacoes_de_liquidez(pool: str) -> list[tuple[str, int, int]]:
    """(tx_hash, bloco, ts) distintos que mexeram na liquidez da pool."""
    with session_scope() as sess:
        rows = sess.execute(
            select(distinct(PoolEvent.tx_hash), PoolEvent.block_number, PoolEvent.ts)
            .where(PoolEvent.pool_address == pool.lower(),
                   PoolEvent.event.in_(EVENTOS_DE_LIQUIDEZ))
            .order_by(PoolEvent.block_number)).all()
    return [(h, b, t) for h, b, t in rows]


def _abre_visto():
    """Tabela das transacoes cujo recibo JA foi lido, com ou sem log do NPM.

    Sem ela, toda transacao de liquidez que nao passa pelo NPM (Mint direto na
    pool, outro gestor de posicao) seria pedida de novo a cada rodada — para
    sempre, e crescendo."""
    import sqlite3

    caminho = settings().database_url.replace("sqlite:///", "").replace(
        "sqlite://", "")
    c = sqlite3.connect(caminho, timeout=30)
    c.execute("""create table if not exists npm_link_visto(
        pool_address text not null,
        tx_hash text not null,
        lido_em integer not null,
        primary key (pool_address, tx_hash))""")
    return c


def link_pendentes(pool: str, limite: int = 400, rpc: RpcClient | None = None,
                   lote: int = 5) -> dict:
    """Liga as transacoes de liquidez da pool que AINDA NAO foram ligadas.

    **Existe porque as pools observadas nunca tiveram `npm_link`**, e sem ele
    nao ha posicao reconstruida: em 11/09 a QUBT tinha 306 `Mint` e ZERO
    transacoes com evento do NPM, a SGOV 15 e zero, a PLTR 926 e 19 — e a
    tabela de posicoes da pagina delas simplesmente nao existia.

    **Pendente, e nao janela de horas**, por causa do coletor que alimenta
    essas pools: o `alcancar` anda em pedacos de 300 mil blocos e chega
    atrasado. Uma janela "ultimas N horas" perderia para sempre toda transacao
    que entrasse no banco depois de a janela passar por ela.

    `limite` fixa o custo por rodada: o que sobra fica para a proxima, e o
    retorno diz quanto sobrou — pool que ficou para tras e informacao.
    Recibo que falha NAO e marcado como visto; volta na rodada seguinte.

    **429 encerra a rodada, nao a derruba.** Lote de 20 recibos levou 429 no
    primario e no blockmachine em 11/09; o de 5 e o que o documento de regras do projeto mediu
    passando. Quando o limite fecha mesmo assim, o que ja foi gravado fica, o
    retorno diz `interrompido` e o resto entra na proxima hora.
    """
    cfg = settings()
    pool = pool.lower()
    npm = cfg.npm_address.lower()
    rpc = rpc or RpcClient()
    c = _abre_visto()
    try:
        # **Tres leituras pelo indice de (pool, bloco) e a diferenca em
        # Python.** A primeira versao fazia `not exists` casando `tx_hash`
        # dentro de `pool_events`, que nao tem indice nessa coluna: na VPS a
        # contagem passou de dois minutos para tres pools. O job e horario.
        cand = c.execute(
            """select tx_hash, min(ts), min(block_number) from pool_events
               where pool_address = ? and event in ('Mint','Burn','Collect')
               group by tx_hash""", (pool,)).fetchall()
        if not cand:
            return {"pendentes": 0, "lidos": 0, "eventos": 0,
                    "inseridos": 0, "restam": 0}
        desde = min(b for _h, _t, b in cand)
        ligadas = {h for (h,) in c.execute(
            "select tx_hash from pool_events where pool_address = ? "
            "and block_number >= ?", (npm, desde))}
        vistas = {h for (h,) in c.execute(
            "select tx_hash from npm_link_visto where pool_address = ?",
            (pool,))}
        pend = [(h, ts) for h, ts, b in sorted(cand, key=lambda x: x[2])
                if h not in ligadas and h not in vistas]
        fila = pend[:limite]
        topicos = {v.lower() for v in NPM_TOPIC0.values()}
        total = inseridos = lidos = 0
        interrompido = None
        agora = int(time.time())
        for i in range(0, len(fila), lote):
            fatia = fila[i:i + lote]
            try:
                recibos = rpc.call_batch(
                    [("eth_getTransactionReceipt", [h]) for h, _ts in fatia])
            except Exception as exc:                          # noqa: BLE001
                interrompido = f"{type(exc).__name__}: {exc}"[:160]
                break
            linhas, vistos = [], []
            for (h, ts), rec in zip(fatia, recibos):
                if not rec:
                    continue
                vistos.append((pool, h, agora))
                for e in rec.get("logs") or []:
                    if ((e.get("address") or "").lower() != npm
                            or not e.get("topics")
                            or e["topics"][0].lower() not in topicos):
                        continue
                    dec = decode_log(e, registry="npm")
                    if not dec:
                        continue
                    linhas.append({
                        "pool_address": npm,
                        "block_number": int(e["blockNumber"], 16),
                        "log_index": int(e["logIndex"], 16),
                        "tx_hash": e["transactionHash"],
                        "tx_index": int(e["transactionIndex"], 16),
                        "block_hash": e["blockHash"], "ts": ts,
                        "event": dec[0], "args": dec[1]})
            if linhas:
                with session_scope() as sess:
                    inseridos += _upsert_ignore(sess, PoolEvent, linhas)
                total += len(linhas)
            # So DEPOIS de gravar os eventos: marcar antes e cair no meio
            # deixaria transacao "vista" sem o evento dela no banco.
            c.executemany("insert or ignore into npm_link_visto values (?,?,?)",
                          vistos)
            c.commit()
            lidos += len(vistos)
        return {"pendentes": len(pend), "lidos": lidos, "eventos": total,
                "inseridos": inseridos, "restam": len(pend) - lidos,
                "interrompido": interrompido}
    finally:
        c.close()


def link_npm_events(pool: str | None = None, batch: int | None = None) -> dict:
    """Busca recibos e grava os logs do NPM sob pool_address = endereco do NPM."""
    cfg = settings()
    pool = (pool or cfg.pool_address).lower()
    npm = cfg.npm_address.lower()
    rpc = RpcClient()
    batch = batch or cfg.batch_size

    txs = transacoes_de_liquidez(pool)
    log.info("npm_link_inicio", transacoes=len(txs))
    if not txs:
        return {"transacoes": 0, "eventos": 0, "inseridos": 0}

    meta = {h: (b, t) for h, b, t in txs}
    hashes = [h for h, _, _ in txs]
    topicos_npm = {v.lower() for v in NPM_TOPIC0.values()}

    total = inseridos = 0
    t0 = time.time()
    for i in range(0, len(hashes), batch):
        fatia = hashes[i:i + batch]
        recibos = rpc.call_batch([("eth_getTransactionReceipt", [h]) for h in fatia])

        linhas = []
        for h, recibo in zip(fatia, recibos):
            if not recibo:
                continue
            bloco, ts = meta[h]
            for entry in recibo.get("logs") or []:
                if (entry.get("address") or "").lower() != npm:
                    continue
                if not entry.get("topics") or entry["topics"][0].lower() not in topicos_npm:
                    continue
                decodificado = decode_log(entry, registry="npm")
                if not decodificado:
                    continue
                nome, args = decodificado
                linhas.append({
                    "pool_address": npm,          # namespace do NPM
                    "block_number": int(entry["blockNumber"], 16),
                    "log_index": int(entry["logIndex"], 16),
                    "tx_hash": entry["transactionHash"],
                    "tx_index": int(entry["transactionIndex"], 16),
                    "block_hash": entry["blockHash"],
                    "ts": ts,
                    "event": nome,
                    "args": args,
                })

        if linhas:
            with session_scope() as sess:
                inseridos += _upsert_ignore(sess, PoolEvent, linhas)
            total += len(linhas)

        if (i // batch) % 20 == 0:
            log.info("npm_link_progresso", transacoes=min(i + batch, len(hashes)),
                     de=len(hashes), eventos=total, rpc=rpc.calls,
                     retries=rpc.retries)

    dt = time.time() - t0
    log.info("npm_link_fim", transacoes=len(hashes), eventos=total,
             inseridos=inseridos, segundos=round(dt, 1))
    return {"transacoes": len(hashes), "eventos": total, "inseridos": inseridos,
            "segundos": round(dt, 1)}


__all__ = ["link_npm_events", "transacoes_de_liquidez"]
