"""Indexer da pool — Fase 1.

Garantias que o check-1 exige e que o codigo abaixo implementa:

  idempotente   PK natural (pool, block, log_index) + upsert que ignora conflito.
                Rodar o backfill duas vezes nao muda contagem.
  retomavel     backfill_ranges registra faixas JA confirmadas. Matar o processo
                no meio e reiniciar continua de onde parou, sem relerar o que ja
                estava fechado nem pular o que faltava.
  sem buracos   cobertura vira query sobre backfill_ranges, nao inspecao visual.
  reorg-safe    so indexa ate head - confirmations; guarda hash e parent_hash e
                detecta divergencia antes de avancar.

O indexer NAO interpreta nada. Nao sabe o que e posicao, fee ou LP. Grava evento.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from ..chain.abi import INDEXED_EVENTS, TOPIC0, decode_log
from ..chain.rpc import RpcClient, RpcError
from ..config import settings
from ..db.models import BackfillRange, Block, IndexerState, PoolEvent
from ..db.registro import batimento_periodico
from ..db.session import session_scope
from ..logging import get_logger

log = get_logger(__name__)


@dataclass
class BackfillStats:
    chunks: int = 0
    logs: int = 0
    inserted: int = 0
    blocks: int = 0
    rpc_calls: int = 0
    seconds: float = 0.0
    ranges_skipped: int = 0
    errors: list = field(default_factory=list)


def _upsert_ignore(sess: Session, model, rows: list[dict]) -> int:
    """INSERT ... ON CONFLICT DO NOTHING, para os dois bancos.

    E o que torna o backfill reexecutavel sem medo.
    """
    if not rows:
        return 0
    cfg = settings()
    if cfg.is_sqlite:
        stmt = sqlite_insert(model).values(rows).on_conflict_do_nothing()
    else:
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        stmt = pg_insert(model).values(rows).on_conflict_do_nothing()
    result = sess.execute(stmt)
    return result.rowcount if result.rowcount and result.rowcount > 0 else 0


class PoolIndexer:
    def __init__(self, rpc: RpcClient | None = None, pool: str | None = None):
        cfg = settings()
        self.cfg = cfg
        self.rpc = rpc or RpcClient()
        self.pool = (pool or cfg.pool_address).lower()
        self.topics = [[TOPIC0[e] for e in INDEXED_EVENTS]]

    # ---- cobertura ---------------------------------------------------

    def covered_ranges(self, sess: Session) -> list[tuple[int, int]]:
        rows = sess.execute(
            select(BackfillRange.from_block, BackfillRange.to_block)
            .where(BackfillRange.pool_address == self.pool)
            .order_by(BackfillRange.from_block)
        ).all()
        merged: list[list[int]] = []
        for lo, hi in rows:
            if merged and lo <= merged[-1][1] + 1:
                merged[-1][1] = max(merged[-1][1], hi)
            else:
                merged.append([lo, hi])
        return [(a, b) for a, b in merged]

    def gaps(self, sess: Session, start: int, end: int) -> list[tuple[int, int]]:
        """Faixas de [start, end] ainda NAO confirmadas. Base do check-1."""
        if end < start:
            return []
        out, cursor = [], start
        for lo, hi in self.covered_ranges(sess):
            if hi < cursor:
                continue
            if lo > cursor:
                out.append((cursor, min(lo - 1, end)))
            cursor = max(cursor, hi + 1)
            if cursor > end:
                break
        if cursor <= end:
            out.append((cursor, end))
        return [(a, b) for a, b in out if a <= b]

    # ---- backfill ----------------------------------------------------

    def _persist_chunk(self, logs: list, stats: BackfillStats,
                       lo: int, hi: int) -> None:
        block_numbers = {int(l["blockNumber"], 16) for l in logs}
        headers = self.rpc.block_timestamps(sorted(block_numbers)) if block_numbers else {}

        event_rows, block_rows = [], []
        descartados = 0
        for entry in logs:
            decoded = decode_log(entry)
            if not decoded:
                continue
            name, args = decoded
            bn = int(entry["blockNumber"], 16)
            head = headers.get(bn)
            if not head:
                # **Descartar o evento e correto; marcar a faixa como coberta
                # depois disso e que mente.** `block_timestamps` e um batch de
                # RPC: um item que volta 429 ou null deixa o bloco sem header e
                # todo evento dele cai fora aqui. Se a faixa fosse gravada
                # assim mesmo, `gaps()` passaria a jurar cobertura sobre
                # blocos cujos eventos nunca entraram — e o sintoma nao seria
                # erro, seria liquidez cumulativa negativa semanas depois.
                #
                # Medido em 2026-09-01 na MU: dois Mint (blocos 50.200.294 e
                # 50.227.974) existiam na chain e nao no banco, dentro de faixa
                # marcada como completa. Mesma familia do marco de qualidade do
                # H4 — o passo de PRUDENCIA fabricando a afirmacao falsa.
                stats.errors.append(f"sem header para o bloco {bn}")
                descartados += 1
                continue
            event_rows.append({
                "pool_address": self.pool,
                "block_number": bn,
                "log_index": int(entry["logIndex"], 16),
                "tx_hash": entry["transactionHash"],
                "tx_index": int(entry["transactionIndex"], 16),
                "block_hash": entry["blockHash"],
                "ts": head["ts"],
                "event": name,
                "args": args,
            })
        for bn, head in headers.items():
            block_rows.append({"number": bn, "hash": head["hash"],
                               "parent_hash": head["parent_hash"], "ts": head["ts"]})

        with session_scope() as sess:
            _upsert_ignore(sess, Block, block_rows)
            inserted = _upsert_ignore(sess, PoolEvent, event_rows)
            # O que chegou fica — `_upsert_ignore` e idempotente e a proxima
            # passada completa. O que NAO fica e a marca de cobertura: sem ela
            # a faixa continua em `gaps()` e sera refeita.
            if not descartados:
                sess.add(BackfillRange(pool_address=self.pool, from_block=lo,
                                       to_block=hi, logs=len(event_rows),
                                       completed_at=int(time.time())))
                sess.merge(IndexerState(stream="backfill", pool_address=self.pool,
                                        last_block=hi, updated_at=int(time.time()),
                                        note=f"chunk {lo}-{hi}"))
        if descartados:
            raise RpcError("block_timestamps",
                           f"{descartados} evento(s) sem header em {lo}-{hi}; "
                           "faixa NAO marcada como coberta")
        stats.inserted += inserted
        stats.logs += len(event_rows)
        stats.blocks += len(block_rows)

    def backfill(self, from_block: int | None = None, to_block: int | None = None,
                 progress_every: int = 1, quiet: bool = False) -> BackfillStats:
        """quiet=True silencia o regime permanente.

        O tail chama isto a cada 5s. Sem `quiet`, um processo 24/7 emite ~52 mil
        linhas por dia dizendo "nao achei nada" — ruido que esconde o que importa.
        Com quiet, so fala quando ha evento ou erro.
        """
        cfg = self.cfg
        start = from_block if from_block is not None else cfg.pool_creation_block
        if to_block is None:
            to_block = self.rpc.block_number() - cfg.confirmations

        stats = BackfillStats()
        t0 = time.time()

        with session_scope() as sess:
            pending = self.gaps(sess, start, to_block)
        total_blocks = sum(hi - lo + 1 for lo, hi in pending)
        if not quiet:
            log.info("backfill_inicio", de=start, ate=to_block,
                     faixas_pendentes=len(pending), blocos_pendentes=total_blocks,
                     chunk=cfg.chunk_size)
        if not pending:
            if not quiet:
                log.info("backfill_nada_a_fazer", motivo="faixa ja coberta")
            stats.seconds = time.time() - t0
            return stats

        done_blocks = 0
        for lo, hi in pending:
            cursor = lo
            while cursor <= hi:
                chunk_hi = min(cursor + cfg.chunk_size - 1, hi)
                # **Encolhe ate passar, em vez de dividir uma vez e desistir.**
                # `log query timed out` depende de QUANTOS logs ha na faixa,
                # nao do tamanho dela: a mesma janela de 250k passa num
                # trecho quieto e estoura num movimentado. A versao anterior
                # partia ao meio uma unica vez e levantava se a metade tambem
                # falhasse — e em 2026-09-01 isso travou o reparo da MU com
                # 2,5M de blocos restantes, repetindo o mesmo erro a cada
                # tentativa. Mesma licao que o `pools_hora` ja tinha pago,
                # onde treze pools V4 sairam como "nenhum Swap" quando o que
                # tinham era uma consulta que nao voltou.
                logs = None
                while logs is None:
                    try:
                        logs = self.rpc.get_logs(self.pool, cursor, chunk_hi,
                                                 self.topics)
                    except RpcError as exc:
                        if chunk_hi <= cursor or chunk_hi - cursor < 1_000:
                            raise
                        meio = cursor + (chunk_hi - cursor) // 2
                        log.warning("chunk_dividido", de=cursor, ate=chunk_hi,
                                    novo_ate=meio, erro=str(exc)[:120])
                        chunk_hi = meio

                self._persist_chunk(logs, stats, cursor, chunk_hi)
                stats.chunks += 1
                done_blocks += chunk_hi - cursor + 1

                if not quiet and stats.chunks % progress_every == 0:
                    pct = 100 * done_blocks / max(1, total_blocks)
                    log.info("backfill_progresso", chunk=stats.chunks,
                             ate=chunk_hi, pct=round(pct, 1),
                             eventos=stats.logs,
                             rpc=self.rpc.calls, retries=self.rpc.retries)
                cursor = chunk_hi + 1

        stats.seconds = time.time() - t0
        stats.rpc_calls = self.rpc.calls
        if not quiet or stats.logs:
            log.info("backfill_fim", chunks=stats.chunks, eventos=stats.logs,
                 inseridos=stats.inserted, blocos=stats.blocks,
                 segundos=round(stats.seconds, 1), rpc=self.rpc.calls,
                 retries=self.rpc.retries, erros=len(stats.errors))
        return stats

    def rescan_event(self, event: str, from_block: int | None = None,
                     to_block: int | None = None) -> BackfillStats:
        """Varre a faixa inteira buscando UM tipo de evento, ignorando a
        cobertura ja registrada.

        Existe porque descobrir um evento relevante depois do backfill e normal
        (foi o caso de CollectProtocol, achado pelo criterio 3). Reindexar tudo
        levaria 42min; isto leva minutos, e o ON CONFLICT garante que os eventos
        ja gravados nao sao tocados.
        """
        cfg = self.cfg
        lo = from_block if from_block is not None else cfg.pool_creation_block
        hi = to_block if to_block is not None else self.rpc.block_number() - cfg.confirmations
        topics = [[TOPIC0[event]]]

        stats = BackfillStats()
        t0 = time.time()
        log.info("rescan_inicio", evento=event, de=lo, ate=hi)
        cursor = lo
        while cursor <= hi:
            chunk_hi = min(cursor + cfg.chunk_size - 1, hi)
            logs = self.rpc.get_logs(self.pool, cursor, chunk_hi, topics)
            if logs:
                # NAO registra BackfillRange: esta varredura e parcial por
                # definicao (um unico topico) e nao pode ser lida como cobertura.
                block_numbers = {int(l["blockNumber"], 16) for l in logs}
                headers = self.rpc.block_timestamps(sorted(block_numbers))
                rows, block_rows = [], []
                for entry in logs:
                    decoded = decode_log(entry)
                    if not decoded:
                        continue
                    name, args = decoded
                    bn = int(entry["blockNumber"], 16)
                    head = headers.get(bn)
                    if not head:
                        continue
                    rows.append({
                        "pool_address": self.pool, "block_number": bn,
                        "log_index": int(entry["logIndex"], 16),
                        "tx_hash": entry["transactionHash"],
                        "tx_index": int(entry["transactionIndex"], 16),
                        "block_hash": entry["blockHash"], "ts": head["ts"],
                        "event": name, "args": args})
                for bn, head in headers.items():
                    block_rows.append({"number": bn, "hash": head["hash"],
                                       "parent_hash": head["parent_hash"],
                                       "ts": head["ts"]})
                with session_scope() as sess:
                    _upsert_ignore(sess, Block, block_rows)
                    stats.inserted += _upsert_ignore(sess, PoolEvent, rows)
                stats.logs += len(rows)
            stats.chunks += 1
            cursor = chunk_hi + 1

        stats.seconds = time.time() - t0
        log.info("rescan_fim", evento=event, encontrados=stats.logs,
                 inseridos=stats.inserted, segundos=round(stats.seconds, 1))
        return stats

    # ---- reorg -------------------------------------------------------

    def detect_reorg(self, sess: Session, depth: int = 50) -> int | None:
        """Devolve o menor bloco divergente, ou None se a cadeia bate.

        Compara hash guardado com hash atual nos ultimos `depth` blocos que
        tem evento. Sem isto, um reorg corrompe o banco em silencio — o pior
        modo de falha possivel para este projeto.
        """
        rows = sess.execute(
            select(Block.number, Block.hash).order_by(Block.number.desc()).limit(depth)
        ).all()
        if not rows:
            return None
        numbers = [n for n, _ in rows]
        stored = {n: h for n, h in rows}
        current = self.rpc.block_timestamps(numbers)
        divergent = [n for n in numbers
                     if n in current and current[n]["hash"] != stored[n]]
        return min(divergent) if divergent else None

    def rewind(self, from_block: int) -> dict:
        """Apaga tudo a partir de `from_block` para reindexar limpo."""
        with session_scope() as sess:
            ev = sess.query(PoolEvent).filter(
                PoolEvent.pool_address == self.pool,
                PoolEvent.block_number >= from_block).delete()
            bl = sess.query(Block).filter(Block.number >= from_block).delete()
            rg = sess.query(BackfillRange).filter(
                BackfillRange.pool_address == self.pool,
                BackfillRange.to_block >= from_block).delete()
        log.warning("rewind", de=from_block, eventos=ev, blocos=bl, faixas=rg)
        return {"eventos": ev, "blocos": bl, "faixas": rg}

    # ---- tail --------------------------------------------------------

    def tail_once(self) -> dict:
        """Um passo do tail. Idempotente: pode ser chamado a qualquer momento."""
        cfg = self.cfg
        head = self.rpc.block_number()
        safe = head - cfg.confirmations

        with session_scope() as sess:
            reorg_at = self.detect_reorg(sess)
        if reorg_at is not None:
            log.error("reorg_detectado", bloco=reorg_at)
            self.rewind(reorg_at)

        with session_scope() as sess:
            covered = self.covered_ranges(sess)
        last = max((hi for _, hi in covered), default=cfg.pool_creation_block - 1)
        if safe <= last:
            return {"head": head, "safe": safe, "novos": 0, "ate": last}

        stats = self.backfill(last + 1, safe, quiet=True)
        return {"head": head, "safe": safe, "novos": stats.logs, "ate": safe}

    def run_tail(self, interval: float = 5.0, max_iterations: int | None = None,
                 heartbeat_s: float = 900.0):
        """Loop do tail. Degrada e continua — nunca morre por erro transitorio.

        Silencioso por padrao: fala quando ha evento novo, quando ha erro, e a
        cada `heartbeat_s` para provar que esta vivo. Um processo 24/7 que fala
        a cada 5s vira ruido; um que nunca fala nao se distingue de um travado.
        """
        i = 0
        acumulado = 0
        ultimo_bat = time.time()
        while max_iterations is None or i < max_iterations:
            try:
                out = self.tail_once()
                if out["novos"]:
                    acumulado += out["novos"]
                    log.info("tail", **out)
                if time.time() - ultimo_bat >= heartbeat_s:
                    log.info("tail_vivo", head=out["head"], ate=out["ate"],
                             eventos_desde_o_ultimo_bat=acumulado,
                             ciclos=i + 1, rpc=self.rpc.calls,
                             retries=self.rpc.retries)
                    # O log so prova que estava vivo para quem estava olhando.
                    # Isto sobrevive ao terminal fechado.
                    batimento_periodico("tail", minimo_s=heartbeat_s,
                                        itens=acumulado,
                                        nota=f"head={out['head']}")
                    acumulado = 0
                    ultimo_bat = time.time()
            except Exception as exc:  # noqa: BLE001
                log.exception("tail_erro", erro=str(exc)[:200])
                # Ciclo que TENTOU e falhou nao e o mesmo que ciclo ausente.
                batimento_periodico("tail", minimo_s=heartbeat_s, ok=False,
                                    erro=str(exc)[:300])
            i += 1
            if max_iterations is None or i < max_iterations:
                time.sleep(interval)


def coverage_report(pool: str | None = None) -> dict:
    """Fatos de cobertura para o check-1."""
    cfg = settings()
    pool = (pool or cfg.pool_address).lower()
    idx = PoolIndexer(pool=pool)
    with session_scope() as sess:
        total = sess.execute(
            select(func.count()).select_from(PoolEvent)
            .where(PoolEvent.pool_address == pool)).scalar_one()
        by_event = dict(sess.execute(
            select(PoolEvent.event, func.count())
            .where(PoolEvent.pool_address == pool)
            .group_by(PoolEvent.event)).all())
        lo = sess.execute(
            select(func.min(PoolEvent.block_number))
            .where(PoolEvent.pool_address == pool)).scalar()
        hi = sess.execute(
            select(func.max(PoolEvent.block_number))
            .where(PoolEvent.pool_address == pool)).scalar()
        ranges = idx.covered_ranges(sess)
        state = sess.get(IndexerState, "backfill")
        top = state.last_block if state else 0
        holes = idx.gaps(sess, cfg.pool_creation_block, top) if top else []
    return {"pool": pool, "eventos": total, "por_evento": by_event,
            "primeiro_bloco_com_evento": lo, "ultimo_bloco_com_evento": hi,
            "faixas_cobertas": ranges, "buracos": holes,
            "ultimo_bloco_confirmado": top}


__all__ = ["PoolIndexer", "BackfillStats", "coverage_report"]
