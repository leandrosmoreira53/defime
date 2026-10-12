"""O FIM de cada serie do `/grafico/`, compacto, para o MCP ler.

    resumir(serie, ...)          -> um timeframe
    estado_do_simbolo(series, ...) -> os seis, com procedencia

**Nao calcula indicador nenhum.** Recebe o dicionario que `grafico_render.
_serie_de` montou para a pagina — os MESMOS arrays que viram `data-serie` no
HTML — e le o ultimo ponto de cada um. A unica excecao e o ATR, que a pagina
nao desenha: ele sai de `indicadores.atr`, nas duas convencoes nomeadas, sobre
os arrays da propria serie. Calcular RSI/MACD aqui, ou no MCP, seria a
terceira implementacao da mesma matematica (Python, JS da pagina, MCP) — a
familia da `_horas_fora` duplicada e do portao do `compor.js`.

O que e conta nossa, e nao leitura, e aritmetica de uma linha sobre valores ja
lidos, e vai nomeada em `DERIVADOS` com a formula: distancia do fechamento a
cada media, %b e largura do Bollinger, posicao no Donchian, volume relativo,
ha quantas barras o histograma do MACD trocou de sinal.

**Leitura, nao decisao.** RSI, MACD e cruzamento de medias estao reprovados
para decidir faixa (`tecnica.POR_QUE_NAO`), e o estado carrega isso escrito.
O teste de AST que proibe `decision/` de importar `indicadores` vale tambem
para este modulo.

**Indicador sem aquecimento sai `None` e entra em `nulos` com o motivo** —
nunca zero. A SMA 200 do semanal (156 barras) e o caso que sempre aparece.

**Barra em formacao e dita.** Com a coleta horaria do pregao (T1b), a ultima
barra de 1h pode estar pela metade, e o diario de hoje so fecha as ~20:00 UTC.
O 3d e o 5d contam PREGOES e o bloco corrente pode ter menos dias que o nome;
isso nao se sabe pela serie, entao sai `None` com o motivo em vez de um
palpite.
"""

from __future__ import annotations

import datetime as dt
import statistics
from typing import Any

from defime.macro import indicadores as I

TIMEFRAMES = ("1h", "4h", "1d", "3d", "5d", "1sem")

# Fechamento do pregao regular em UTC no horario de VERAO dos EUA (20:00) e no
# padrao (21:00). Usamos o mais TARDIO: dizer "pode estar incompleta" por uma
# hora a mais e inofensivo; dizer "fechada" com o pregao aberto nao e.
FECHAMENTO_UTC_H = 21

VOL_REL_JANELA = 20

PARAMETROS = {
    "ema": [9, 21, 50], "sma": [200],
    "bollinger": {"janela": I.BOLL_JANELA, "k": I.BOLL_K, "desvio": "populacional"},
    "donchian": {"janela": I.DONCHIAN_JANELA, "le": "maxima e minima"},
    "gaussiano": {"polos": I.GC_POLOS, "periodo": I.GC_PERIODO, "mult": I.GC_MULT},
    "rsi": {"janela": I.RSI_JANELA, "suavizacao": "Wilder"},
    "stoch_rsi": {"janela": I.STOCH_JANELA, "k": I.STOCH_K, "d": I.STOCH_D},
    "macd": {"rapida": I.MACD_RAPIDA, "lenta": I.MACD_LENTA, "sinal": I.MACD_SINAL},
    "atr": {"janela": I.ATR_JANELA, "convencoes": ["wilder", "media"]},
    "volume_relativo": {"janela": VOL_REL_JANELA, "base": "mediana das barras ANTERIORES"},
}

DERIVADOS = [
    {"campo": "medias.*.dist_pct", "formula": "(fechamento / media - 1) x 100"},
    {"campo": "bollinger.pct_b", "formula": "(fechamento - inf) / (sup - inf)"},
    {"campo": "bollinger.largura_pct", "formula": "(sup - inf) / meio x 100"},
    {"campo": "donchian.posicao", "formula": "(fechamento - inf) / (sup - inf); 0 = minima do canal"},
    {"campo": "macd.barras_desde_troca_de_sinal",
     "formula": "barras consecutivas, contando a ultima, com o histograma do mesmo sinal"},
    {"campo": "volume.relativo",
     "formula": f"volume / mediana dos {VOL_REL_JANELA} volumes anteriores"},
    {"campo": "atr.*_pct", "formula": "ATR / fechamento x 100"},
    {"campo": "barra.var_pct", "formula": "(fechamento / fechamento anterior - 1) x 100"},
]

# O que cada chave da serie da pagina e, para `nulos` dizer o nome legivel.
_NOMES = {"ema9": "EMA 9", "ema21": "EMA 21", "ema50": "EMA 50",
          "sma200": "SMA 200", "rsi": "RSI 14", "stk": "Stoch RSI K",
          "std": "Stoch RSI D", "macd": "MACD", "sig": "MACD sinal",
          "hist": "MACD histograma", "bbm": "Bollinger", "dcs": "Donchian",
          "gcm": "Gaussian"}


def _r(x: float | None, n: int) -> float | None:
    return None if x is None else round(x, n)


def _ultimo(serie: dict, k: str) -> Any:
    arr = serie.get(k)
    return arr[-1] if arr else None


def _dist(c: float | None, ref: float | None) -> float | None:
    if c is None or not ref:
        return None
    return round((c / ref - 1) * 100, 2)


def _posicao(c, inf, sup) -> float | None:
    if None in (c, inf, sup) or sup == inf:
        return None
    return round((c - inf) / (sup - inf), 3)


def _desde_troca(hist: list) -> int | None:
    """Barras consecutivas (contando a ultima) com o histograma do mesmo sinal.

    Zero exato conta como troca: nao e nem positivo nem negativo, e dizer "ha
    40 barras positivo" atravessando um zero afirmaria continuidade que nao
    houve.
    """
    if not hist or hist[-1] is None or hist[-1] == 0:
        return None
    pos = hist[-1] > 0
    n = 0
    for x in reversed(hist):
        if x is None or x == 0 or (x > 0) != pos:
            break
        n += 1
    return n


_MOTIVO_LEILAO = ("ultima barra e o print do leilao de fechamento (volume zero, "
                  "O=H=L=C): o provedor a publica e a pagina a desenha, mas volume "
                  "relativo zero ali seria artefato, nao falta de negocio")


def _e_leilao(o, h, low, c, v) -> bool:
    """A barra das 20:00 UTC do Yahoo: um preco so e volume zero.

    Ela ja inventou um desconto estrutural de -0,82% neste projeto, quando o
    pareamento de basis casava so com ela. Aqui ela fica na serie (e o que a
    pagina desenha, e os indicadores da pagina a incluem), mas e DITA.
    """
    return (v == 0) and o == h == low == c


def _fechada(tf: str, ts: int, agora: int, ts_ultima_1h: int | None) -> tuple[bool | None, str | None]:
    """A ultima barra ja fechou? `None` quando a serie nao permite saber."""
    if tf == "1h":
        return agora >= ts + 3600, None
    if ts_ultima_1h is not None and agora < ts_ultima_1h + 3600:
        # Toda barra agregada contem a ultima de 1h; se ela esta aberta, todas
        # estao.
        return False, "contem a barra de 1 h em formacao"
    ag = dt.datetime.fromtimestamp(agora, dt.UTC)
    dia = dt.datetime.fromtimestamp(ts_ultima_1h if ts_ultima_1h else ts, dt.UTC)
    hoje_aberto = dia.date() == ag.date() and ag.hour < FECHAMENTO_UTC_H
    if tf in ("1d", "4h"):
        if hoje_aberto:
            return False, "pregao de hoje ainda nao fechou"
        return True, None
    if tf == "1sem":
        mesma = dia.isocalendar()[:2] == ag.isocalendar()[:2]
        if mesma and (ag.weekday() < 4 or (ag.weekday() == 4 and ag.hour < FECHAMENTO_UTC_H)):
            return False, "semana corrente ainda nao fechou"
        return True, None
    return None, ("3d e 5d contam pregoes: pela serie nao se sabe se o bloco "
                  "corrente ja tem todos os dias")


def resumir(serie: dict, ultimas: int = 10, agora: int | None = None,
            tf: str = "1d", ts_ultima_1h: int | None = None) -> dict | None:
    """O ultimo ponto de cada indicador de UM timeframe. `None` se vazio."""
    t = serie.get("t") or []
    if not t:
        return None
    agora = int(agora if agora is not None else dt.datetime.now(dt.UTC).timestamp())
    c = serie["c"]
    i = len(t) - 1
    fech = c[i]
    ant = c[i - 1] if i >= 1 else None
    fechada, motivo_fechada = _fechada(tf, t[i], agora, ts_ultima_1h)

    medias = {}
    for k in ("ema9", "ema21", "ema50", "sma200"):
        v = _ultimo(serie, k)
        medias[k] = {"valor": v, "dist_pct": _dist(fech, v)}

    bs, bm, bi = (_ultimo(serie, k) for k in ("bbs", "bbm", "bbi"))
    ds, di = _ultimo(serie, "dcs"), _ultimo(serie, "dci")
    gs, gm, gi = (_ultimo(serie, k) for k in ("gcs", "gcm", "gci"))
    hist = serie.get("hist") or []

    h, low, v = serie["h"], serie["l"], serie["v"]
    atr_w = I.atr(h, low, c, modo="wilder")[-1]
    atr_m = I.atr(h, low, c, modo="media")[-1]
    ant_vol = [x for x in v[max(0, i - VOL_REL_JANELA):i] if x is not None]
    med_vol = statistics.median(ant_vol) if len(ant_vol) >= VOL_REL_JANELA else None
    leilao = _e_leilao(serie["o"][i], h[i], low[i], fech, v[i])

    nulos = []
    for k, nome in _NOMES.items():
        if _ultimo(serie, k) is None:
            inteira = k in (serie.get("_vazias") or [])
            nulos.append({"indicador": nome, "motivo": (
                f"serie inteira sem valor: {len(t)} barras nao aquecem o indicador"
                if inteira else "ultimo ponto sem valor (aquecimento)")})
    if med_vol is None:
        nulos.append({"indicador": "volume relativo",
                      "motivo": f"menos de {VOL_REL_JANELA} barras anteriores"})
    elif leilao:
        nulos.append({"indicador": "volume relativo", "motivo": _MOTIVO_LEILAO})

    ult = []
    for j in range(max(0, len(t) - ultimas), len(t)):
        ult.append({"t": t[j], "c": c[j], "rsi": serie["rsi"][j],
                    "macd_hist": hist[j] if hist else None})

    return {
        "barra": {"ts": t[i], "o": serie["o"][i], "h": h[i], "l": low[i], "c": fech,
                  "v": v[i], "var_pct": _dist(fech, ant),
                  "fechada": fechada, "motivo": motivo_fechada,
                  "leilao_de_fechamento": leilao},
        "medias": medias,
        "bollinger": {"sup": bs, "meio": bm, "inf": bi, "pct_b": _posicao(fech, bi, bs),
                      "largura_pct": _r((bs - bi) / bm * 100, 2) if None not in (bs, bi) and bm else None},
        "donchian": {"sup": ds, "inf": di, "posicao": _posicao(fech, di, ds)},
        "gaussiano": {"sup": gs, "meio": gm, "inf": gi},
        "rsi14": _ultimo(serie, "rsi"),
        "stoch_rsi": {"k": _ultimo(serie, "stk"), "d": _ultimo(serie, "std")},
        "macd": {"linha": _ultimo(serie, "macd"), "sinal": _ultimo(serie, "sig"),
                 "hist": hist[-1] if hist else None,
                 "hist_anterior": hist[-2] if len(hist) >= 2 else None,
                 "barras_desde_troca_de_sinal": _desde_troca(hist)},
        "atr": {"wilder": _r(atr_w, 6), "media": _r(atr_m, 6),
                "wilder_pct": _r(atr_w / fech * 100, 2) if atr_w is not None and fech else None,
                "media_pct": _r(atr_m / fech * 100, 2) if atr_m is not None and fech else None},
        "volume": {"v": v[i], "mediana_anteriores": med_vol,
                   "relativo": _r(v[i] / med_vol, 2) if med_vol and not leilao else None},
        "ultimas": ult,
        "nulos": nulos,
        "n_barras": serie.get("_cortada_de") or len(t),
        "n_barras_na_serie_da_pagina": len(t),
    }


def estado_do_simbolo(simbolo: str, series: dict, ts_ultima_1h: int | None,
                      agora: int | None = None, ultimas: int = 10) -> dict:
    """Os seis timeframes de um simbolo, com a procedencia."""
    agora = int(agora if agora is not None else dt.datetime.now(dt.UTC).timestamp())
    tfs = {}
    for tf in TIMEFRAMES:
        r = resumir(series.get(tf) or {}, ultimas=ultimas, agora=agora, tf=tf,
                    ts_ultima_1h=ts_ultima_1h)
        if r is not None:
            tfs[tf] = r
    return {"simbolo": simbolo, "calculado_em": agora,
            "fonte": "bolsa (Nasdaq/NYSE via Yahoo), barras de 1 h; o resto agregado",
            "ultima_barra_1h_ts": ts_ultima_1h, "timeframes": tfs}


__all__ = ["resumir", "estado_do_simbolo", "TIMEFRAMES", "PARAMETROS", "DERIVADOS"]
