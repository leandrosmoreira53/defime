"""Serie de precos da pool, reconstruida dos Swaps. Camada 2 — sem rede.

Cada Swap carrega o `tick` resultante. Isso da uma serie de precos com
granularidade de transacao desde a criacao da pool, sem depender de nenhum
feed externo — e cobrindo 24/7, inclusive quando a NYSE esta fechada.

Essa serie e a base de tudo na Fase 3: volatilidade realizada, ATR, e o
calendario empirico de quando o preco realmente se move.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from sqlalchemy import func, select

from ..config import settings
from ..db.models import PoolEvent
from ..db.session import session_scope
from .v3math import tick_to_usd


@dataclass
class Barra:
    ts: int
    abertura: float
    maxima: float
    minima: float
    fechamento: float
    swaps: int

    @property
    def amplitude(self) -> float:
        return self.maxima - self.minima


_CACHE_SERIE: dict[str, tuple[int, list[tuple[int, int]]]] = {}


def limpar_cache() -> None:
    """Descarta a serie em memoria. Usar em teste que troca de banco."""
    _CACHE_SERIE.clear()


def _serie_completa(pool: str) -> list[tuple[int, int]]:
    """A serie inteira, lida UMA vez e revalidada contra o ultimo ts do banco.

    Por que existe: o perfil do replay mostrou `barras()` consumindo **54% do
    tempo total**, com 384.941 chamadas a `json.loads` — o mesmo dado sendo
    relido e re-desserializado dez vezes, porque cada ponto de decisao chama
    `barras(ate_ts=...)` com um corte diferente.

    A chave e que cortar por `ate_ts` NAO precisa do banco: e uma fatia de uma
    lista ja ordenada por tempo. Ler tudo e fatiar em memoria da o mesmo
    resultado, byte a byte.

    **Isto nao afrouxa o anti-lookahead.** Fatiar por ts e exatamente o que o
    `WHERE ts <= ate_ts` fazia; e um cache velho so pode ESCONDER dado novo,
    nunca revelar dado futuro. A revalidacao por `max(ts)` e uma leitura
    indexada de microssegundos e mantem o cache correto com o tail escrevendo.
    """
    with session_scope() as sess:
        ultimo = sess.execute(
            select(func.max(PoolEvent.ts))
            .where(PoolEvent.pool_address == pool,
                   PoolEvent.event == "Swap")).scalar() or 0

    guardado = _CACHE_SERIE.get(pool)
    if guardado is not None and guardado[0] == ultimo:
        return guardado[1]

    stmt = (select(PoolEvent.ts, PoolEvent.args)
            .where(PoolEvent.pool_address == pool, PoolEvent.event == "Swap")
            .order_by(PoolEvent.block_number, PoolEvent.log_index))
    with session_scope() as sess:
        serie = [(ts, int(a["tick"])) for ts, a in sess.execute(stmt)]
    _CACHE_SERIE[pool] = (ultimo, serie)
    return serie


def serie_de_ticks(pool: str | None = None,
                   desde_ts: int | None = None,
                   ate_ts: int | None = None) -> list[tuple[int, int]]:
    """[(ts, tick)] de cada Swap, em ordem cronologica.

    `ate_ts` existe para o replay: a calibracao feita no instante T nao pode
    enxergar swap nenhum posterior a T.
    """
    cfg = settings()
    pool = (pool or cfg.pool_address).lower()
    serie = _serie_completa(pool)
    if desde_ts is None and ate_ts is None:
        return serie
    # bisect sobre a lista ordenada: o mesmo recorte que o WHERE fazia
    from bisect import bisect_left, bisect_right
    marcas = [t for t, _ in serie]
    ini = bisect_left(marcas, desde_ts) if desde_ts else 0
    fim = bisect_right(marcas, ate_ts) if ate_ts else len(serie)
    return serie[ini:fim]


def barras(intervalo_s: int = 3600, pool: str | None = None,
           dec0: int = 6, dec1: int = 18,
           desde_ts: int | None = None,
           ate_ts: int | None = None) -> list[Barra]:
    """Agrega os swaps em barras OHLC de `intervalo_s`.

    Barras sem swap nenhum NAO sao inventadas: a ausencia de negocio e
    informacao, e preencher com o ultimo preco criaria volatilidade zero
    artificial — exatamente o vies que envenenaria a estimativa de risco.
    """
    ticks = serie_de_ticks(pool, desde_ts, ate_ts)
    if not ticks:
        return []

    # cache: poucos ticks distintos, conversao e cara
    cache: dict[int, float] = {}

    def usd(t: int) -> float:
        if t not in cache:
            cache[t] = float(tick_to_usd(t, dec0, dec1))
        return cache[t]

    saida: list[Barra] = []
    balde = None
    o = h = l = c = 0.0
    n = 0
    for ts, tick in ticks:
        b = (ts // intervalo_s) * intervalo_s
        p = usd(tick)
        if balde is None:
            balde, o, h, l, c, n = b, p, p, p, p, 1
            continue
        if b != balde:
            saida.append(Barra(balde, o, h, l, c, n))
            balde, o, h, l, c, n = b, p, p, p, p, 1
        else:
            h = max(h, p)
            l = min(l, p)
            c = p
            n += 1
    if balde is not None:
        saida.append(Barra(balde, o, h, l, c, n))
    return saida


def true_range(barras_: list[Barra]) -> list[float]:
    """TR classico, com o fechamento anterior."""
    out = []
    for i, b in enumerate(barras_):
        if i == 0:
            out.append(b.amplitude)
            continue
        fech = barras_[i - 1].fechamento
        out.append(max(b.maxima - b.minima, abs(b.maxima - fech),
                       abs(b.minima - fech)))
    return out


def atr(barras_: list[Barra], periodo: int = 14) -> float | None:
    tr = true_range(barras_)
    if len(tr) < periodo:
        return None
    return sum(tr[-periodo:]) / periodo


def retornos_log(barras_: list[Barra]) -> list[float]:
    out = []
    for i in range(1, len(barras_)):
        a, b = barras_[i - 1].fechamento, barras_[i].fechamento
        if a > 0 and b > 0:
            out.append(math.log(b / a))
    return out


def volatilidade_realizada(barras_: list[Barra], intervalo_s: int,
                           anualizar: bool = True) -> float | None:
    """Desvio-padrao dos retornos log, opcionalmente anualizado."""
    r = retornos_log(barras_)
    if len(r) < 3:
        return None
    media = sum(r) / len(r)
    var = sum((x - media) ** 2 for x in r) / (len(r) - 1)
    sigma = math.sqrt(var)
    if not anualizar:
        return sigma
    por_ano = (365 * 24 * 3600) / intervalo_s
    return sigma * math.sqrt(por_ano)


def calendario_de_atividade(intervalo_s: int = 3600,
                            pool: str | None = None) -> dict:
    """Quanto do tempo a pool efetivamente negocia, por hora UTC.

    Responde com DADO a pergunta que o plano do projeto respondia por suposicao (~81%
    do tempo sem ancora de mercado) e que caiu quando /rhj/assets mostrou
    overnight TRADABLE.
    """
    from datetime import datetime, timezone
    bs = barras(intervalo_s, pool)
    if not bs:
        return {}
    por_hora: dict[int, dict] = {}
    for b in bs:
        h = datetime.fromtimestamp(b.ts, timezone.utc).hour
        d = por_hora.setdefault(h, {"barras": 0, "swaps": 0, "amplitude": 0.0})
        d["barras"] += 1
        d["swaps"] += b.swaps
        d["amplitude"] += b.amplitude

    primeiro, ultimo = bs[0].ts, bs[-1].ts
    baldes_possiveis = max(1, (ultimo - primeiro) // intervalo_s + 1)
    for h, d in por_hora.items():
        d["swaps_por_barra"] = round(d["swaps"] / d["barras"], 1)
        d["amplitude_media"] = round(d["amplitude"] / d["barras"], 4)
        del d["amplitude"]
    return {
        "barras_com_negocio": len(bs),
        "baldes_no_periodo": baldes_possiveis,
        "cobertura": round(len(bs) / baldes_possiveis, 4),
        "de_ts": primeiro, "ate_ts": ultimo,
        "por_hora_utc": dict(sorted(por_hora.items())),
    }


__all__ = ["Barra", "serie_de_ticks", "barras", "atr", "true_range",
           "retornos_log", "volatilidade_realizada", "calendario_de_atividade",
           "limpar_cache"]
