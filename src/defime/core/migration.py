"""Fase 4 — para onde a liquidez esta se mexendo. Camada 2, sem rede.

Tudo aqui e reconstruido do historico da chain, entao **funciona desde o
primeiro dia** — nao depende de snapshot nenhum ter sido tirado no passado.
Essa foi a razao de projeto: um sistema que so comeca a enxergar migracao
depois de acumular snapshots proprios chega tarde demais para servir.

O que se mede num instante:
  - centro de massa da liquidez, em USD
  - massa acima e abaixo do preco
  - massa dentro de +-2% e +-5%

E a migracao e a diferenca disso entre dois instantes (1h / 4h / 24h).
"""

from __future__ import annotations

import math

from ..config import settings
from .book import TickBook, build_book
from .v3math import tick_to_usd

LN_1_0001 = math.log(1.0001)


def _tick_para_usd(tick: int) -> float:
    return float(tick_to_usd(tick, 6, 18))


def _usd_para_tick(usd: float) -> int:
    """USD -> tick. Monotonicamente DECRESCENTE nesta pool."""
    return int(round(-math.log(usd / 1e12) / LN_1_0001))


def _massa_por_faixa(book: TickBook) -> list[tuple[int, int, int]]:
    """[(tick_de, tick_ate, liquidez_ativa)] — a curva do livro."""
    curva = book.cumulative()
    return [(curva[i][0], curva[i + 1][0], max(0, curva[i][1]))
            for i in range(len(curva) - 1)]


def _massa_entre(faixas: list[tuple[int, int, int]], t0: int, t1: int) -> float:
    """Liquidez x largura entre dois ticks. E a unidade certa: liquidez sozinha
    nao soma entre faixas de larguras diferentes."""
    lo, hi = min(t0, t1), max(t0, t1)
    total = 0.0
    for a, b, liq in faixas:
        de, ate = max(a, lo), min(b, hi)
        if ate > de:
            total += liq * (ate - de)
    return total


def _mediana_da_massa(faixas: list[tuple[int, int, int]], total: float) -> float:
    """Tick onde a massa acumulada (liquidez x largura) cruza 50%.

    Robusta a faixa larga: o que desloca o cruzamento e a MASSA da cauda, nao a
    largura dela. Interpola dentro da faixa que cruza para nao granular no
    tickSpacing — sem isso o centro pularia de 60 em 60 ticks e os deltas de 1h
    (que sao de centavos) virariam zero ou saltos.
    """
    if total <= 0:
        return 0.0
    metade = total / 2.0
    acumulado = 0.0
    for a, b, liq in sorted(faixas):
        if liq <= 0:
            continue
        massa = liq * (b - a)
        if acumulado + massa >= metade:
            fracao = (metade - acumulado) / massa if massa else 0.0
            return a + fracao * (b - a)
        acumulado += massa
    return float(max(b for _, b, _ in faixas))


def perfil(book: TickBook, tick_atual: int) -> dict:
    """Retrato da distribuicao de liquidez num instante."""
    faixas = _massa_por_faixa(book)
    if not faixas:
        return {}
    total = sum(liq * (b - a) for a, b, liq in faixas)
    if total <= 0:
        return {}

    preco = _tick_para_usd(tick_atual)
    t_min = min(a for a, _, _ in faixas)
    t_max = max(b for _, b, _ in faixas)

    # tick maior = USD menor: "acima do preco em USD" e tick MENOR
    acima = _massa_entre(faixas, t_min, tick_atual)
    abaixo = _massa_entre(faixas, tick_atual, t_max)

    bandas = {}
    for pct in (0.02, 0.05):
        t_alto = _usd_para_tick(preco * (1 + pct))   # USD maior -> tick menor
        t_baixo = _usd_para_tick(preco * (1 - pct))
        bandas[f"massa_em_{int(pct*100)}pct"] = round(
            _massa_entre(faixas, t_alto, t_baixo) / total, 6)

    # Centro da massa = MEDIANA da distribuicao, nao media. Duas correcoes
    # sucessivas chegaram aqui, e as duas foram medidas:
    #
    # 1. A primeira versao fazia media aritmetica em USD
    #    (`meio_usd = (usd(a) + usd(b)) / 2`, ponderada por liquidez x largura).
    #    Isso EXPLODE: `t_min` vem da faixa mais larga do livro, e uma posicao
    #    quase-full-range tem tick muito negativo cujo USD e astronomico. Medido
    #    em 2026-08-18: centro saindo **$7,5e48**, e o validador numerico
    #    reprovando o proprio template (falso positivo do check-4 criterio 4).
    #
    # 2. A segunda versao passou a media para espaco de TICK — linear em
    #    log-preco, a mesma decisao que `core/consensus.py` documenta. Tirou a
    #    explosao mas NAO tirou o vies: uma unica faixa enorme continua
    #    arrastando a media. Medido em 2026-08-19: centro **$155,83** com o
    #    preco em $131,49 e apenas **27,8% da massa acima do preco**. Os dois
    #    numeros se contradizem — se so 27,8% esta acima, o centro tem que estar
    #    ABAIXO. Foi assim que o vies apareceu: por inconsistencia interna.
    #
    # A mediana nao tem esse problema por construcao: ela e o tick onde a massa
    # acumulada cruza 50%, e uma cauda larga desloca o ponto de cruzamento em
    # proporcao a massa dela, nao em proporcao a largura dela. Com interpolacao
    # linear dentro da faixa que cruza, para nao granular no tickSpacing.
    centro_tick = _mediana_da_massa(faixas, total)
    return {
        "preco_usd": round(preco, 4),
        "tick": tick_atual,
        "centro_de_massa_usd": round(_tick_para_usd(int(round(centro_tick))), 4),
        "centro_de_massa_tick": round(centro_tick, 2),
        "centro_de_massa_metodo": "mediana_da_massa",
        "massa_total": total,
        "fracao_acima_do_preco": round(acima / total, 6),
        "fracao_abaixo_do_preco": round(abaixo / total, 6),
        **bandas,
        "faixas": len(faixas),
    }


def _estado_no_bloco(pool: str, bloco: int) -> tuple[TickBook, int] | None:
    from sqlalchemy import select

    from ..db.models import PoolEvent
    from ..db.session import session_scope
    with session_scope() as sess:
        linha = sess.execute(
            select(PoolEvent.args)
            .where(PoolEvent.pool_address == pool, PoolEvent.event == "Swap",
                   PoolEvent.block_number <= bloco)
            .order_by(PoolEvent.block_number.desc(),
                      PoolEvent.log_index.desc()).limit(1)).first()
    if not linha:
        return None
    return build_book(pool, upto_block=bloco), int(linha[0]["tick"])


def _bloco_em(pool: str, ts: int) -> int | None:
    from sqlalchemy import select

    from ..db.models import PoolEvent
    from ..db.session import session_scope
    with session_scope() as sess:
        return sess.execute(
            select(PoolEvent.block_number)
            .where(PoolEvent.pool_address == pool, PoolEvent.ts <= ts)
            .order_by(PoolEvent.block_number.desc()).limit(1)).scalar()


def migracao(agora_ts: int, *, pool: str | None = None,
             janelas_h: tuple[float, ...] = (1.0, 4.0, 24.0)) -> dict:
    """Delta do perfil de liquidez em cada janela. Nada depende de snapshot."""
    pool = (pool or settings().pool_address).lower()
    bloco_agora = _bloco_em(pool, agora_ts)
    if bloco_agora is None:
        return {}
    atual = _estado_no_bloco(pool, bloco_agora)
    if atual is None:
        return {}
    p_agora = perfil(*atual)

    deltas = {}
    for h in janelas_h:
        bloco_antes = _bloco_em(pool, agora_ts - int(h * 3600))
        if bloco_antes is None or bloco_antes >= bloco_agora:
            deltas[f"{h:g}h"] = {"disponivel": False,
                                 "motivo": "sem dado anterior nesta janela"}
            continue
        antes = _estado_no_bloco(pool, bloco_antes)
        if antes is None:
            deltas[f"{h:g}h"] = {"disponivel": False, "motivo": "sem swap"}
            continue
        p_antes = perfil(*antes)
        if not p_antes:
            deltas[f"{h:g}h"] = {"disponivel": False, "motivo": "livro vazio"}
            continue
        deltas[f"{h:g}h"] = {
            "disponivel": True,
            "centro_de_massa_usd_antes": p_antes["centro_de_massa_usd"],
            "delta_centro_de_massa_usd": round(
                p_agora["centro_de_massa_usd"] - p_antes["centro_de_massa_usd"], 4),
            "delta_fracao_acima": round(
                p_agora["fracao_acima_do_preco"] - p_antes["fracao_acima_do_preco"], 6),
            "delta_massa_em_2pct": round(
                p_agora["massa_em_2pct"] - p_antes["massa_em_2pct"], 6),
            "delta_massa_em_5pct": round(
                p_agora["massa_em_5pct"] - p_antes["massa_em_5pct"], 6),
            "crescimento_da_massa": round(
                p_agora["massa_total"] / p_antes["massa_total"] - 1, 6)
            if p_antes["massa_total"] else None,
            "bloco_antes": bloco_antes,
        }
    return {"agora": p_agora, "bloco": bloco_agora, "janelas": deltas}


__all__ = ["perfil", "migracao"]
