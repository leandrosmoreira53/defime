"""As pools de cada ATIVO, da medicao do proprio indexador, para o MCP ler.

    por_ativo(bd, agora) -> {simbolo: {...}}

**Fonte: `pool_hora` + `pool_param` + `token_simbolo`**, as mesmas tabelas do
`/heat/` e do `/pools/`. Zero RPC, zero rede. Foi para isto que a T3 existe no
MCP: o universo que o MCP lia do GeckoTerminal nao trazia MU, QUBT, RBLX,
TSLA — pools vivas, indexadas aqui ha semanas.

**Nenhuma taxa suposta.** A fee e a `fee_usd` que o `pools_hora` ja gravou por
leitura: na V3 o `feeProtocol` lido do slot0 na mesma rodada (0x66 = 5/6 nas
de 0,30% e 1%, 0x44 = 3/4 nas de 0,05%; ate 24/09 era 5/6 suposto para toda
V3, e as linhas antigas continuam com ele) e `taxa_lp_v4` na V4, que sai
NULA quando a parte do LP nao e legivel (taxa dinamica, chave ausente). Uma
hora nula no meio da janela anula o APR da janela — somar as outras e dividir
pela janela inteira publicaria uma taxa menor que a medida.

**Cada linha de `pool_hora` e uma AMOSTRA de ~1 h, nao o intervalo desde a
leitura anterior.** Medido em 24/09 na MU: toda linha cobre 36.000 blocos
(~1 h) e as leituras saem a cada ~2 h, com o vao entre elas sem varrer. A
primeira versao deste modulo somava a fee das leituras e dividia pela janela
de relogio — publicava METADE do APR. O denominador certo sao as HORAS
AMOSTRADAS: blocos varridos x duracao do bloco, e a duracao do bloco e
MEDIDA nas proprias leituras (blocos entre a primeira e a ultima / segundos
entre elas), nao suposta. E uma media das horas que o job amostrou, e a
resposta diz quantas foram.

**Basis pool x bolsa so no MESMO instante:** o preco da pool no `ts` da leitura
contra o fechamento da barra de 1 h da bolsa que CONTEM esse `ts` (ate 60 min
de diferenca, declarado), e so com volume > 0 — a barra do leilao das 20:00
UTC ja inventou um desconto de -0,82% aqui. Fora do pregao nao ha par, e isso
nao vira basis zero.
"""

from __future__ import annotations

import sqlite3
import statistics as st

JANELA_APR_H = 24
# Se as ultimas 24 h nao tem MIN_HORAS_AMOSTRADAS, tenta a semana. Medido em 24/09:
# 13 das 27 pools sao lidas pelo pools_hora so ~2x por dia. Semana mistura pregao,
# madrugada e fim de semana — e a janela usada vai na resposta, em `janela_apr_h`.
JANELAS_APR_H = (24, 168)
IDADE_MAXIMA_H = 3.0          # a mesma do /heat/: acima disso a pool nao esta sendo medida
JANELA_BASIS_DIAS = 30
# Abaixo disso o APR nao sai. Medido em 24/09: a MSFT tinha DUAS leituras em 24 h,
# uma delas no pico do pregao ($279 mil numa hora), e anualizar isso publicava
# 390% de APR do LP contra 152% nominal do agregador. Seis horas e um quarto do
# dia: ainda nao cobre todas as sessoes, e por isso as horas vao na resposta.
MIN_HORAS_AMOSTRADAS = 6.0


def _pools(c: sqlite3.Connection) -> list[tuple]:
    return c.execute(
        "select p.pool, p.versao, p.taxa, s.simbolo"
        "  from pool_param p join token_simbolo s"
        "    on s.endereco = case when p.usdg0 then p.token1 else p.token0 end"
        " where s.simbolo is not null").fetchall()


def _segundos_por_bloco(pts: list[tuple]) -> float | None:
    """Duracao do bloco MEDIDA: blocos e segundos entre a primeira e a ultima leitura."""
    if len(pts) < 2:
        return None
    db = pts[-1][9] - pts[0][9]
    dt_s = pts[-1][0] - pts[0][0]
    return dt_s / db if db > 0 and dt_s > 0 else None


def _apr(dentro: list[tuple], ult: tuple, s_por_bloco: float | None) -> tuple:
    """(apr, horas_amostradas, motivo) sobre as leituras `dentro`."""
    if not dentro:
        return None, None, "nenhuma leitura na janela"
    if s_por_bloco is None:
        return None, None, "leituras insuficientes para medir a duracao do bloco"
    horas = sum((p[9] - p[8]) for p in dentro) * s_por_bloco / 3600
    if any(p[6] is None for p in dentro):
        return None, horas, "hora com a fee do LP ilegivel (V4 com taxa dinamica ou chave ausente)"
    if not ult[2]:
        return None, horas, "sem TVL"
    if horas < MIN_HORAS_AMOSTRADAS:
        return None, horas, (f"amostra curta: {horas:.1f} h amostradas em {len(dentro)} leitura(s), "
                             f"minimo {MIN_HORAS_AMOSTRADAS:g} h — anualizar isso publicaria a hora que caiu "
                             "na amostra, nao a pool")
    return 100 * sum(p[6] for p in dentro) / ult[2] * (8760 / horas), horas, None


def _uma_pool(c: sqlite3.Connection, pool: str, agora: int) -> dict | None:
    desde = agora - (max(JANELAS_APR_H) + 6) * 3600
    pts = c.execute(
        "select ts, preco, tvl_usd, tvl_fonte, book_usd, volume_usd, fee_usd, swaps,"
        "       bloco_de, bloco_ate"
        "  from pool_hora where pool = ? and ts > ? and bloco_de is not null"
        "   and bloco_ate > bloco_de order by ts", (pool, desde)).fetchall()
    if not pts:
        return None
    ult = pts[-1]
    s_por_bloco = _segundos_por_bloco(pts)
    apr = amostradas_h = motivo = None
    janela = JANELAS_APR_H[0]
    for janela in JANELAS_APR_H:
        dentro = [p for p in pts if p[0] > agora - janela * 3600]
        apr, amostradas_h, motivo = _apr(dentro, ult, s_por_bloco)
        # so alarga por falta de AMOSTRA; fee ilegivel ou TVL ausente nao melhoram com mais dias
        if apr is not None or not (motivo or "").startswith(("amostra curta", "nenhuma leitura")):
            break
    return {
        "pool": pool, "ts": ult[0], "idade_h": round((agora - ult[0]) / 3600, 2),
        "preco": ult[1], "tvl_usd": ult[2], "tvl_fonte": ult[3], "book_1pct_usd": ult[4],
        "janela_apr_h": janela,
        "volume_janela_usd": sum((p[5] or 0) for p in dentro),
        "fee_lp_janela_usd": (sum(p[6] for p in dentro) if dentro and all(p[6] is not None for p in dentro)
                              else None),
        "swaps_janela": sum((p[7] or 0) for p in dentro),
        "leituras_janela": len(dentro),
        "horas_amostradas": round(amostradas_h, 2) if amostradas_h is not None else None,
        "segundos_por_bloco": round(s_por_bloco, 4) if s_por_bloco else None,
        "apr_lp_pct": round(apr, 2) if apr is not None else None,
        "motivo_apr": motivo,
    }


def basis(c: sqlite3.Connection, pool: str, simbolo: str, agora: int) -> dict | None:
    """Preco da pool contra a barra de 1 h da bolsa que contem o MESMO instante."""
    desde = agora - JANELA_BASIS_DIAS * 86400
    pts = c.execute("select ts, preco from pool_hora where pool = ? and ts > ? and preco > 0"
                    " order by ts", (pool, desde)).fetchall()
    if not pts:
        return None
    barras = c.execute(
        "select ts, fechamento from acoes_ohlc where simbolo = ? and intervalo = '1h'"
        " and ts > ? and volume > 0 and fechamento > 0 order by ts",
        (simbolo, desde - 3600)).fetchall()
    if not barras:
        return None
    pares = []
    j = 0
    for ts, p in pts:
        while j + 1 < len(barras) and barras[j + 1][0] <= ts:
            j += 1
        b_ts, b_c = barras[j]
        if b_ts <= ts < b_ts + 3600:
            pares.append((ts, 100 * (p / b_c - 1)))
    if not pares:
        return {"n": 0, "motivo": "nenhuma leitura da pool caiu dentro de uma barra do pregao"}
    vals = [v for _, v in pares]
    return {"n": len(pares), "ultimo_pct": round(vals[-1], 3), "ultimo_ts": pares[-1][0],
            "idade_ultimo_h": round((agora - pares[-1][0]) / 3600, 1),
            "mediana_pct": round(st.median(vals), 3),
            "desvio_pct": round(st.stdev(vals), 3) if len(vals) >= 2 else None,
            "janela_dias": JANELA_BASIS_DIAS,
            "pareamento": "preco da pool no ts da leitura x fechamento da barra de 1 h que contem o ts "
                          "(ate 60 min de diferenca); barras de volume zero excluidas"}


def por_ativo(bd: str, agora: int) -> dict[str, dict]:
    c = sqlite3.connect(bd, timeout=60)
    try:
        grupos: dict[str, list[dict]] = {}
        for pool, versao, taxa, sim in _pools(c):
            r = _uma_pool(c, pool, agora)
            if r is None:
                continue
            r.update({"versao": versao, "taxa_medida_pct": round((taxa or 0) * 100, 4)})
            grupos.setdefault(sim.upper(), []).append(r)
        out = {}
        for sim, ps in grupos.items():
            frescas = [p for p in ps if p["idade_h"] <= IDADE_MAXIMA_H]
            # A dominante sai das FRESCAS: uma pool parada ha dias com TVL antigo maior
            # nao pode falar pelo ativo agora.
            dom = max(frescas or ps, key=lambda p: p["tvl_usd"] or 0)
            out[sim] = {
                "dominante": dom, "dominante_fresca": bool(frescas),
                "pools": sorted(ps, key=lambda p: -(p["tvl_usd"] or 0)),
                "n_pools": len(ps), "tvl_total_usd": sum(p["tvl_usd"] or 0 for p in ps),
                "basis": basis(c, dom["pool"], sim, agora),
            }
        return out
    finally:
        c.close()


__all__ = ["por_ativo", "basis", "JANELA_APR_H", "JANELAS_APR_H", "IDADE_MAXIMA_H", "MIN_HORAS_AMOSTRADAS"]
