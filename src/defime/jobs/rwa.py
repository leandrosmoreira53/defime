"""Universo de pools RWA da Robinhood Chain → `rwa.json`, a entrada do `/heat/pools/`.

    python -m defime.jobs.rwa --estado ./estado            # rodar de hora em hora

Para cada pool RWA do universo (ações e ETFs tokenizados, verificados pelo contrato):
  - a taxa que CHEGA ao LP, lida do contrato (V3 com feeProtocol entrega 5/6 da nominal; V4 desconta o protocolFee);
    pool com hook fica sem taxa (a taxa muda a cada swap) e é marcada, nunca vira 0%;
  - TVL e volume de 1 h e 24 h do agregador, e o volume de 7 e 30 dias do OHLCV do GeckoTerminal;
  - APR de 1 h, 24 h, 7 d e 30 d com a taxa do LP;
  - uma série de `vol_1h` por pool (banco SQLite próprio), de onde o `/heat/pools/` mede concentração e aquecimento.

Fontes: DexScreener, GeckoTerminal e leitura on-chain, via o cliente do `mcp-defime`
(pip install "defime[rwa]"). Este módulo NÃO calcula concentração nem giro: entrega os pontos, e a conta mora
num lugar só, em `defime.heat_pools`.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

from defime_mcp.client import PoolClient
from defime_mcp.config import Settings
from defime_mcp.exceptions import DefimeError, NotFoundHTTP
from defime_mcp.ids import PoolRef
from defime_mcp.ratelimit import HostLimiter

from defime.estado import gravar_estado

TTL_HIST_S = 6 * 3600
TTL_TAXA_S = 24 * 3600
IDADE_MAX_INDEXADOR_S = 26 * 3600     # o pools_hora completo roda 1x/dia; o de relevantes a cada 2h
DIVERGENCIA_ALTA_PCT = 25.0
SERIE_RETER_S = 7 * 86400            # a série só precisa de 24 h; 7 d dá folga para olhar para trás
SERIE_ENTREGA_S = 26 * 3600          # o que vai no rwa.json: 24 h e um pouco, para a janela não faltar


async def _taxa_lp(c: PoolClient, pid: str) -> dict[str, Any]:
    chave = f"rwa:lpfee:{pid}"
    hit = await c.cache.get(chave, TTL_TAXA_S)
    if hit and hit.fresh:
        return hit.value
    try:
        st = await c.onchain.read(PoolRef("robinhood", pid))
        if st.lp_fee_pct is None:
            v = {"lp": None, "ver": st.protocol[-2:], "motivo": "hook"}
        else:
            v = {"lp": st.lp_fee_pct, "ver": st.protocol[-2:], "motivo": None}
    except NotFoundHTTP:
        v = {"lp": None, "ver": None, "motivo": "outro_amm"}
    except DefimeError as exc:
        return {"lp": None, "ver": None, "motivo": f"rpc: {exc}"}          # não cacheia falha transitória
    await c.cache.set(chave, v)
    return v


async def _volumes(c: PoolClient, pid: str) -> dict[str, Any]:
    chave = f"rwa:ohlcv30d:{pid}"
    hit = await c.cache.get(chave, TTL_HIST_S)
    if hit and hit.fresh:
        rows = hit.value
    else:
        try:
            rows = await c.gecko.ohlcv(PoolRef("robinhood", pid), "30d")
        except DefimeError as exc:
            if hit:                                   # stale-if-error
                rows = hit.value
            else:
                return {"erro": str(exc)}
        else:
            await c.cache.set(chave, rows)
    if not rows:
        return {"v7d": 0.0, "v30d": 0.0, "dias": 0.0}
    ult = max(int(r[0]) for r in rows)
    dias = (ult - min(int(r[0]) for r in rows)) / 86400 + 1
    return {"v7d": sum(float(r[5] or 0) for r in rows if int(r[0]) > ult - 7 * 86400),
            "v30d": sum(float(r[5] or 0) for r in rows), "dias": dias}


async def _gt_pool(c: PoolClient, pid: str) -> tuple[str | None, float | None]:
    """TVL do agregador para pool indexada que ficou fora do universo listado (cache ~1h)."""
    chave = f"rwa:gtpool:{pid}"
    hit = await c.cache.get(chave, 3000)
    if hit and hit.fresh:
        return tuple(hit.value)
    try:
        p = await c.gecko.get_pool(PoolRef("robinhood", pid))
        v = (p.name, p.tvl_usd)
    except NotFoundHTTP:
        v = (None, None)
    except DefimeError:
        return tuple(hit.value) if hit else (None, None)
    await c.cache.set(chave, list(v))
    return v


def _apr(vol: float | None, taxa: float | None, tvl: float | None, dias: float) -> float | None:
    if vol is None or taxa is None or not tvl or dias <= 0:
        return None
    return vol * taxa / 100 / tvl * 365 / dias * 100


def indexador(bd_indexador: str | None, agora: int) -> dict[str, dict[str, Any]]:
    """Última leitura de `pool_hora` por pool, se houver um indexador próprio (banco aberto SÓ LEITURA).

    Opcional: sem banco, o universo vem só do agregador e `tem_indexador` sai falso."""
    if not bd_indexador or not Path(bd_indexador).exists():
        return {}
    con = sqlite3.connect(f"file:{bd_indexador}?mode=ro", uri=True, timeout=30)
    try:
        linhas = con.execute("""
            select h.pool, h.ts, h.tvl_usd, h.tvl_fonte, h.book_usd,
                   coalesce(s0.simbolo, '?') || '/' || coalesce(s1.simbolo, '?'), p.taxa, p.versao
            from pool_hora h
            join (select pool, max(ts) m from pool_hora group by pool) u on u.pool = h.pool and u.m = h.ts
            left join pool_param p on p.pool = h.pool
            left join token_simbolo s0 on s0.endereco = lower(case when p.usdg0 then p.token1 else p.token0 end)
            left join token_simbolo s1 on s1.endereco = lower(case when p.usdg0 then p.token0 else p.token1 end)
            where h.ts > ?""", (agora - IDADE_MAX_INDEXADOR_S,)).fetchall()
    finally:
        con.close()
    return {r[0].lower(): {"ts": r[1], "tvl": r[2], "fonte": r[3], "book": r[4], "nome": r[5],
                           "taxa": r[6], "versao": r[7]} for r in linhas}


async def coletar(c: PoolClient, bd_indexador: str | None) -> dict[str, Any]:
    agora = int(time.time())
    uni = await c.universe("robinhood")
    idx = indexador(bd_indexador, agora)
    rwa = []
    for i in sorted((i for i in uni.items if i.category == "rwa"), key=lambda i: -(i.tvl_usd or 0)):
        taxa = await _taxa_lp(c, i.pool_id)
        vol = await _volumes(c, i.pool_id)
        if taxa["lp"] is not None:
            base, marca = taxa["lp"], ""
        elif taxa["motivo"] == "hook":
            base, marca = None, "hook"
        else:
            base, marca = i.fee_pct, "nominal"
        ix = idx.get(i.pool_id)
        rwa.append({
            "nome": i.name, "ativo": i.rwa_asset, "pool_id": i.pool_id, "versao": taxa["ver"],
            "fee": i.fee_pct, "lp": taxa["lp"], "marca": marca, "motivo": taxa["motivo"],
            "tvl": i.tvl_usd, "tvl_idx": ix and ix["tvl"], "fonte_idx": ix and ix["fonte"],
            "apr_1h": _apr(i.volume_1h_usd, base, i.tvl_usd, 1 / 24),
            "apr_24h": _apr(i.volume_24h_usd, base, i.tvl_usd, 1),
            "apr_7d": _apr(vol.get("v7d"), base, i.tvl_usd, min(7, vol.get("dias") or 0)),
            "apr_30d": _apr(vol.get("v30d"), base, i.tvl_usd, min(30, vol.get("dias") or 0)),
            "dias": vol.get("dias"), "erro_hist": vol.get("erro"),
            # crus, para o /heat/pools/: giro, aquecendo e idade saem deles lá, não aqui
            "vol_1h": i.volume_1h_usd, "vol_24h": i.volume_24h_usd, "dex": i.dex,
            "criada": i.created_at.isoformat() if i.created_at else None,
        })
    por_id = {i.pool_id: (i.name, i.tvl_usd) for i in uni.items}
    conf = []
    for pid, ix in sorted(idx.items(), key=lambda kv: -(kv[1]["tvl"] or 0)):
        nome_ag, tvl_ag = por_id.get(pid) or await _gt_pool(c, pid)
        nome = ix["nome"] if ix["nome"] and "?" not in ix["nome"] else (nome_ag or ix["nome"])
        conf.append({"pool_id": pid, "nome": nome, "taxa": ix["taxa"], "versao": ix["versao"],
                     "tvl_idx": ix["tvl"], "fonte": ix["fonte"], "book": ix["book"], "ts": ix["ts"],
                     "tvl_ag": tvl_ag, "nome_ag": nome_ag, "div": _div(ix["tvl"], tvl_ag)})
    return {"ts": agora, "rwa": rwa, "conf": conf, "notas_universo": uni.notes,
            "n_universo": len(uni.items), "tem_indexador": bool(idx)}


def serie_gravar(db: str | Path, ts: int, rwa: list[dict[str, Any]], agora: int | None = None) -> int:
    """Um ponto de `vol_1h` por pool nesta rodada. Devolve quantos gravou.

    `vol_1h` ausente NÃO vira ponto: zero afirmaria que a pool não negociou naquela hora, e é
    justamente a hora cheia que a concentração procura. Rodada repetida no mesmo `ts` não duplica.
    """
    agora = agora if agora is not None else ts
    con = sqlite3.connect(db, timeout=30)
    try:
        con.execute("""create table if not exists volume_hora(
            pool_id text not null, ts integer not null, vol_1h real not null, vol_24h real, tvl real,
            primary key (pool_id, ts))""")
        linhas = [(r["pool_id"], ts, r["vol_1h"], r.get("vol_24h"), r.get("tvl"))
                  for r in rwa if r.get("vol_1h") is not None]
        con.executemany("insert or ignore into volume_hora values (?,?,?,?,?)", linhas)
        con.execute("delete from volume_hora where ts < ?", (agora - SERIE_RETER_S,))
        con.commit()
        return len(linhas)
    finally:
        con.close()


def serie_ler(db: str | Path, agora: int, janela_s: int = SERIE_ENTREGA_S) -> dict[str, list[list[float]]]:
    """`{pool_id: [[ts, vol_1h], ...]}` da janela, em ordem de tempo. Banco ausente → vazio."""
    if not Path(db).exists():
        return {}
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30)
    try:
        out: dict[str, list[list[float]]] = {}
        for pid, ts, v in con.execute("select pool_id, ts, vol_1h from volume_hora where ts >= ? "
                                      "order by pool_id, ts", (agora - janela_s,)):
            out.setdefault(pid, []).append([ts, v])
        return out
    except sqlite3.OperationalError:          # tabela ainda não criada
        return {}
    finally:
        con.close()


def estado_dados(d: dict[str, Any], serie: dict[str, list[list[float]]]) -> dict[str, Any]:
    """O que vai no `rwa.json`: a coleta da hora mais a série de cada pool, sem conta nova."""
    return {"ts": d["ts"], "n_universo": d["n_universo"], "notas_universo": d.get("notas_universo", []),
            "tem_indexador": d.get("tem_indexador"), "serie_janela_s": SERIE_ENTREGA_S,
            "rwa": [{**r, "serie_vol_1h": serie.get(r["pool_id"], [])} for r in d["rwa"]],
            "conf": d["conf"]}



def _div(a: float | None, b: float | None) -> float | None:
    if not a or not b:
        return None
    return (b - a) / a * 100



def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--estado", default=os.environ.get("DEFIME_ESTADO_DIR", "estado"),
                    help="pasta onde grava rwa.json (lida pelo /heat/pools/)")
    ap.add_argument("--indexador", default=None, help="banco SQLite com a tabela pool_hora (opcional)")
    ap.add_argument("--cache", default="cache/rwa.db")
    ap.add_argument("--serie", default=None, help="banco da série de vol_1h (padrão: ao lado do cache)")
    a = ap.parse_args()
    t0 = time.time()
    # Job de hora em hora não tem pressa: vai BEM abaixo do limite do GeckoTerminal (medido: 429 a 20/min).
    HostLimiter.KNOWN["api.geckoterminal.com"] = 0.12
    Path(a.cache).parent.mkdir(parents=True, exist_ok=True)
    s = Settings(cache_path=Path(a.cache), ttl_list=3000, scope="rwa_bluechip", onchain_enabled=True,
                 max_retries=4, max_retry_after=60, user_agent="defime/0.1 (+https://github.com/leandrosmoreira53/defime)")

    async def run() -> dict[str, Any]:
        async with PoolClient(s) as c:
            # Universo mais fundo que o da tool do MCP (que precisa responder em segundos).
            c.SCOPED_PAGES = (("stable", 5), ("bluechip", 2), ("top", 1))
            return await coletar(c, a.indexador)

    d = asyncio.run(run())
    serie_db = Path(a.serie) if a.serie else Path(a.cache).with_name("rwa_volume_hora.db")
    n = serie_gravar(serie_db, d["ts"], d["rwa"])
    Path(a.estado).mkdir(parents=True, exist_ok=True)
    os.environ["DEFIME_ESTADO_DIR"] = str(Path(a.estado).resolve())
    est = gravar_estado("rwa", estado_dados(d, serie_ler(serie_db, d["ts"])), d["ts"])
    print(f"{len(d['rwa'])} pools RWA · {len(d['conf'])} na conferência · série +{n} · "
          f"estado {est or '—'} · {time.time() - t0:.0f}s")
    return 0 if est else 1


if __name__ == "__main__":
    sys.exit(main())
