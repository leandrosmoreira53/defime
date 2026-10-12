"""Fase 4 — onde a fee realmente e paga, por faixa de tick. Sem rede.

A pergunta que isto responde e a unica que importa para escolher range:

    **quanto de fee cada unidade de liquidez ativa recebeu, por faixa?**

Volume sozinho engana. Uma faixa com muito volume e muita liquidez pode pagar
menos por dolar investido que uma faixa morna e vazia. O denominador e a
liquidez ativa NAQUELE momento, nao a de hoje — por isso o calculo caminha pelo
historico refazendo o livro, em vez de dividir o volume acumulado pelo livro
atual.

Fee da LP = fee_tier x 5/6. O `feeProtocol=102` desvia 1/6, verificado on-chain.
"""

from __future__ import annotations

from ..config import settings
from .v3math import tick_to_usd

FRACAO_LP = 5.0 / 6.0


def densidade(pool: str | None = None, *, fee_pips: int = 3000,
              bucket_ticks: int = 60, desde_ts: int | None = None,
              ate_ts: int | None = None) -> dict:
    """Fee paga as LPs por unidade de liquidez ativa, por faixa de tick.

    Caminha os Swaps em ordem. Cada swap paga `volume x tier x 5/6` para quem
    estava ativo no tick dele; o divisor e o campo `liquidity` do PROPRIO
    evento, que ja e a liquidez ativa daquele instante. Isso evita ter que
    reconstruir o livro swap a swap — e o mesmo campo que o check-2 provou
    bater com o livro em 404/404 pontos.
    """
    from sqlalchemy import select

    from ..db.models import PoolEvent
    from ..db.session import session_scope

    pool = (pool or settings().pool_address).lower()
    stmt = (select(PoolEvent.ts, PoolEvent.args)
            .where(PoolEvent.pool_address == pool, PoolEvent.event == "Swap")
            .order_by(PoolEvent.block_number, PoolEvent.log_index))
    if desde_ts:
        stmt = stmt.where(PoolEvent.ts >= desde_ts)
    if ate_ts:
        stmt = stmt.where(PoolEvent.ts <= ate_ts)

    taxa = (fee_pips / 1_000_000) * FRACAO_LP
    volume: dict[int, float] = {}
    fee: dict[int, float] = {}
    por_liquidez: dict[int, float] = {}
    swaps: dict[int, int] = {}
    t_min = t_max = None

    with session_scope() as sess:
        for ts, a in sess.execute(stmt):
            liq = int(a["liquidity"])
            if liq <= 0:
                continue
            v = abs(int(a["amount0"])) / 1e6
            if v <= 0:
                continue
            b = (int(a["tick"]) // bucket_ticks) * bucket_ticks
            f = v * taxa
            volume[b] = volume.get(b, 0.0) + v
            fee[b] = fee.get(b, 0.0) + f
            # fee por unidade de liquidez: e isto que se compara entre faixas
            por_liquidez[b] = por_liquidez.get(b, 0.0) + f / liq
            swaps[b] = swaps.get(b, 0) + 1
            t_min = ts if t_min is None else min(t_min, ts)
            t_max = ts if t_max is None else max(t_max, ts)

    if not fee:
        return {}

    dias = ((t_max - t_min) / 86400) if (t_max and t_min and t_max > t_min) else None
    faixas = []
    for b in sorted(volume):
        faixas.append({
            "tick_de": b, "tick_ate": b + bucket_ticks,
            "usd_de": round(float(tick_to_usd(b + bucket_ticks, 6, 18)), 3),
            "usd_ate": round(float(tick_to_usd(b, 6, 18)), 3),
            "volume_usd": round(volume[b], 2),
            "fee_lp_usd": round(fee[b], 4),
            "fee_por_unidade_de_liquidez": por_liquidez[b],
            "swaps": swaps[b],
        })
    melhor = max(faixas, key=lambda f: f["fee_por_unidade_de_liquidez"])
    return {
        "faixas": faixas,
        "bucket_ticks": bucket_ticks,
        "fee_lp_total_usd": round(sum(fee.values()), 2),
        "volume_total_usd": round(sum(volume.values()), 2),
        "dias": round(dias, 3) if dias else None,
        "fee_lp_por_dia_usd": round(sum(fee.values()) / dias, 2) if dias else None,
        "faixa_mais_densa": {k: melhor[k] for k in
                             ("tick_de", "tick_ate", "usd_de", "usd_ate",
                              "fee_por_unidade_de_liquidez", "swaps")},
    }


def fee_esperada_do_range(dens: dict, tick_lower: int, tick_upper: int,
                          liquidez: int) -> dict:
    """Fee que ESTE range teria recebido no periodo medido, com esta liquidez.

    Retrospectivo, nao previsao: responde "quanto a faixa pagou", que e a
    ancora empirica contra a qual a projecao do simulador pode ser conferida.
    """
    if not dens:
        return {}
    total = 0.0
    faixas_tocadas = 0
    for f in dens["faixas"]:
        if f["tick_ate"] <= tick_lower or f["tick_de"] >= tick_upper:
            continue
        total += f["fee_por_unidade_de_liquidez"] * liquidez
        faixas_tocadas += 1
    dias = dens.get("dias")
    return {
        "fee_usd_no_periodo": round(total, 4),
        "fee_usd_por_dia": round(total / dias, 4) if dias else None,
        "faixas_tocadas": faixas_tocadas,
        "dias": dias,
    }


__all__ = ["densidade", "fee_esperada_do_range", "FRACAO_LP"]
