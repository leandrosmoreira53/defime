"""O estado tecnico que o MCP le: o fim da serie da PAGINA, sem conta nova.

O teste que mais vale e o de conformidade: cada numero do `tecnica.json` tem de
ser o ultimo ponto do array que a pagina desenha. Se um dia alguem "otimizar"
o resumo recalculando um indicador aqui, os dois divergem no primeiro ajuste de
parametro — e o sintoma seria o MCP e a pagina discordando sem ninguem saber
qual esta certo, a familia do `compor.js`.
"""

from __future__ import annotations

import ast
import datetime as dt
import json
import math
import statistics
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from defime.jobs import grafico as J                                    # noqa: E402
from defime import estado as E                                   # noqa: E402
from defime.macro import indicadores as I                        # noqa: E402
from defime.macro import resumo_tecnico as RT                    # noqa: E402
from defime.macro.acoes import Barra                             # noqa: E402

_PREGAO_UTC = dt.datetime(2024, 1, 2, 14, 0, tzinfo=dt.UTC)


def _barras(n=600, inicio=None, leilao=False):
    inicio = int((inicio or _PREGAO_UTC).timestamp())
    out, x = [], 100.0
    for i in range(n):
        x *= 1 + 0.003 * math.sin(i * 0.37) + 0.0009 * math.cos(i * 1.7)
        dia, hora = divmod(i, 7)
        ts = inicio + dia * 86400 + hora * 3600
        out.append(Barra(ts=ts, abertura=x * 0.999, maxima=x * 1.004,
                         minima=x * 0.996, fechamento=x, volume=1000 + i))
    if leilao:
        b = out[-1]
        out.append(Barra(ts=b.ts + 3600, abertura=b.fechamento, maxima=b.fechamento,
                         minima=b.fechamento, fechamento=b.fechamento, volume=0))
    return out


def _depois(barras, h=12):
    return barras[-1].ts + h * 3600


# ------------------------------------------------------------ conformidade

def test_cada_numero_e_o_ultimo_ponto_da_serie_da_pagina():
    barras = _barras(1500)
    series = J.series_de(barras)
    agora = _depois(barras)
    for tf in RT.TIMEFRAMES:
        s = series[tf]
        r = RT.resumir(s, agora=agora, tf=tf, ts_ultima_1h=barras[-1].ts)
        assert r["barra"]["c"] == s["c"][-1]
        assert r["rsi14"] == s["rsi"][-1]
        assert r["stoch_rsi"] == {"k": s["stk"][-1], "d": s["std"][-1]}
        assert (r["macd"]["linha"], r["macd"]["sinal"], r["macd"]["hist"]) == (
            s["macd"][-1], s["sig"][-1], s["hist"][-1])
        assert (r["bollinger"]["sup"], r["bollinger"]["meio"], r["bollinger"]["inf"]) == (
            s["bbs"][-1], s["bbm"][-1], s["bbi"][-1])
        assert (r["donchian"]["sup"], r["donchian"]["inf"]) == (s["dcs"][-1], s["dci"][-1])
        assert (r["gaussiano"]["sup"], r["gaussiano"]["meio"], r["gaussiano"]["inf"]) == (
            s["gcs"][-1], s["gcm"][-1], s["gci"][-1])
        for k in ("ema9", "ema21", "ema50", "sma200"):
            assert r["medias"][k]["valor"] == s[k][-1]


def test_atr_e_o_do_modulo_de_indicadores_nas_duas_convencoes():
    barras = _barras(400)
    s = J.series_de(barras)["1h"]
    r = RT.resumir(s, agora=_depois(barras), tf="1h")
    assert r["atr"]["wilder"] == round(I.atr(s["h"], s["l"], s["c"])[-1], 6)
    assert r["atr"]["media"] == round(I.atr(s["h"], s["l"], s["c"], modo="media")[-1], 6)
    assert r["atr"]["wilder"] != r["atr"]["media"]


def test_resumo_nao_reimplementa_indicador():
    """Le o ultimo ponto; quem calcula e `indicadores`. Por AST, nao por prosa."""
    arv = ast.parse((RAIZ / "src/defime/macro/resumo_tecnico.py").read_text(encoding="utf-8"))
    proibidas = {"sma", "ema", "rsi", "stoch_rsi", "macd", "bollinger", "donchian",
                 "gaussiano", "atr", "_wilder"}
    definidas = {n.name for n in ast.walk(arv) if isinstance(n, ast.FunctionDef)}
    assert not (definidas & proibidas)


def test_decision_nao_importa_o_resumo():
    for p in (RAIZ / "src/defime/decision").rglob("*.py"):
        assert "resumo_tecnico" not in p.read_text(encoding="utf-8"), p


# ------------------------------------------------------------- ausencias

def test_sma200_sem_aquecimento_e_none_com_motivo_nunca_zero():
    barras = _barras(120)                   # ~17 dias: a SMA 200 do diario nao aquece
    s = J.series_de(barras)["1d"]
    r = RT.resumir(s, agora=_depois(barras), tf="1d")
    assert r["medias"]["sma200"] == {"valor": None, "dist_pct": None}
    nomes = {n["indicador"]: n["motivo"] for n in r["nulos"]}
    assert "SMA 200" in nomes and "inteira" in nomes["SMA 200"]


def test_serie_vazia_nao_vira_timeframe():
    e = RT.estado_do_simbolo("X", {"1h": {"t": []}}, None, agora=0)
    assert e["timeframes"] == {}


def test_barra_do_leilao_e_dita_e_nao_vira_volume_relativo_zero():
    barras = _barras(300, leilao=True)
    s = J.series_de(barras)["1h"]
    r = RT.resumir(s, agora=_depois(barras), tf="1h")
    assert r["barra"]["leilao_de_fechamento"] is True
    assert r["volume"]["relativo"] is None
    assert any("leilao" in n["motivo"] for n in r["nulos"])


def test_volume_relativo_usa_as_barras_anteriores():
    barras = _barras(300)
    s = J.series_de(barras)["1h"]
    r = RT.resumir(s, agora=_depois(barras), tf="1h")
    ant = s["v"][-21:-1]
    assert r["volume"]["mediana_anteriores"] == statistics.median(ant)
    assert r["barra"]["leilao_de_fechamento"] is False


# -------------------------------------------------------------- derivados

def test_derivados_batem_com_a_formula_declarada():
    barras = _barras(900)
    s = J.series_de(barras)["1h"]
    r = RT.resumir(s, agora=_depois(barras), tf="1h")
    c = s["c"][-1]
    assert r["medias"]["ema50"]["dist_pct"] == round((c / s["ema50"][-1] - 1) * 100, 2)
    assert r["bollinger"]["pct_b"] == round((c - s["bbi"][-1]) / (s["bbs"][-1] - s["bbi"][-1]), 3)
    assert r["barra"]["var_pct"] == round((c / s["c"][-2] - 1) * 100, 2)
    assert 0 <= r["donchian"]["posicao"] <= 1


def test_barras_desde_troca_de_sinal():
    assert RT._desde_troca([-1, -2, 1, 2, 3]) == 3
    assert RT._desde_troca([1, 2, -1]) == 1
    assert RT._desde_troca([1, 0, 2, 3]) == 2           # zero quebra a sequencia
    assert RT._desde_troca([None, None]) is None
    assert RT._desde_troca([]) is None


# ------------------------------------------------------- barra em formacao

def _ts(y, m, d, h, mi=0):
    return int(dt.datetime(y, m, d, h, mi, tzinfo=dt.UTC).timestamp())


def test_barra_de_1h_aberta_e_dita_e_contamina_as_agregadas():
    ult = _ts(2026, 9, 23, 17, 30)                      # quarta, pregao
    agora = _ts(2026, 9, 23, 17, 50)
    assert RT._fechada("1h", ult, agora, ult) == (False, None)
    for tf in ("4h", "1d", "1sem"):
        f, motivo = RT._fechada(tf, ult, agora, ult)
        assert f is False and "1 h" in motivo


def test_diario_de_hoje_so_fecha_depois_do_pregao():
    ult = _ts(2026, 9, 23, 19, 30)
    assert RT._fechada("1d", ult, _ts(2026, 9, 23, 20, 45), ult)[0] is False
    assert RT._fechada("1d", ult, _ts(2026, 9, 24, 3, 50), ult)[0] is True


def test_semana_fecha_so_na_sexta_depois_do_pregao():
    ult = _ts(2026, 9, 23, 19, 30)                      # quarta
    assert RT._fechada("1sem", ult, _ts(2026, 9, 24, 3, 50), ult)[0] is False
    sexta = _ts(2026, 9, 25, 19, 30)
    assert RT._fechada("1sem", sexta, _ts(2026, 9, 26, 3, 50), sexta)[0] is True


def test_3d_e_5d_nao_afirmam_o_que_a_serie_nao_sabe():
    ult = _ts(2026, 9, 22, 19, 30)
    f, motivo = RT._fechada("3d", ult, _ts(2026, 9, 24, 3, 50), ult)
    assert f is None and "pregoes" in motivo


# ------------------------------------------------------------------ job

def test_estado_mescla_com_o_anterior_e_declara_a_fronteira():
    anterior = {"simbolos": {"QUBT": {"calculado_em": 1}, "MU": {"calculado_em": 1}}}
    e = J.estado_tecnico({"MU": {"calculado_em": 9}}, 9, anterior)
    assert e["simbolos"]["QUBT"]["calculado_em"] == 1       # nao apagou
    assert e["simbolos"]["MU"]["calculado_em"] == 9
    assert e["entra_na_decisao"] is False and "RSI / estocastico" in e["por_que_nao"]


def test_estado_do_simbolo_serializa_e_e_compacto():
    barras = _barras(5000)
    series = J.series_de(barras)
    e = RT.estado_do_simbolo("MU", series, barras[-1].ts, agora=_depois(barras))
    corpo = json.dumps(e)
    assert set(e["timeframes"]) == set(RT.TIMEFRAMES)
    assert len(corpo) < 25_000                  # 30 simbolos cabem em < 1 MB
    assert e["timeframes"]["1h"]["n_barras"] == 5000
    assert e["timeframes"]["1h"]["n_barras_na_serie_da_pagina"] == J.JANELA_1H


def test_job_grava_tecnica_json(tmp_path, monkeypatch):
    import sqlite3
    bd = tmp_path / "b.db"
    c = sqlite3.connect(bd)
    c.execute("create table acoes_ohlc(simbolo, intervalo, ts, abertura, maxima, "
              "minima, fechamento, volume)")
    c.executemany("insert into acoes_ohlc values (?,?,?,?,?,?,?,?)",
                  [("MU", "1h", b.ts, b.abertura, b.maxima, b.minima, b.fechamento,
                    b.volume) for b in _barras(400)])
    c.commit()
    c.close()
    d = tmp_path / "estado"
    d.mkdir()
    monkeypatch.setenv("DEFIME_ESTADO_DIR", str(d))
    monkeypatch.setattr(J, "gravar_batimento", lambda *a, **k: None)
    monkeypatch.setattr(sys, "argv", ["grafico", "--simbolos", "MU", "--bd", str(bd),
                                      "--saida", str(tmp_path / "g")])
    assert J.main() == 0
    raw = json.loads((d / "tecnica.json").read_text(encoding="utf-8"))
    assert "MU" in raw["dados"]["simbolos"] and raw["gerado_em"] > 0
    assert E.diretorio() == d
