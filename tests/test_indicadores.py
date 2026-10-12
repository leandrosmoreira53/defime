"""Os indicadores do grafico — alinhamento, identidades e a fronteira.

**O teste que mais importa aqui nao e de valor, e de ALINHAMENTO.** Um
indicador com um indice de deslocamento continua desenhando uma curva bonita
sobre os candles; o erro nao levanta nada e nao ha como ve-lo na tela. Por
isso toda funcao e obrigada a devolver o mesmo comprimento da entrada.

**E o segundo e de FRONTEIRA:** `macro/indicadores.py` declara que e leitura e
nunca decisao. Isso e uma afirmacao sobre o codigo, e afirmacao sobre o codigo
que so existe em prosa e a familia de bug numero dois deste projeto. O teste
le a arvore de `decision/` e reprova o import.
"""

from __future__ import annotations

import ast
import math
import statistics as st
from pathlib import Path

import pytest

from defime.macro import indicadores as I
from defime.macro import tecnica as T
from defime.macro.acoes import Barra

RAIZ = Path(__file__).resolve().parents[1]


def _serie(n=300, semente=7):
    """Passeio deterministico, sem `random`: o mesmo teste em toda maquina."""
    v, x = [], 100.0
    for i in range(n):
        x *= 1 + 0.004 * math.sin(i * semente * 0.7) + 0.0012 * math.cos(i * 1.9)
        v.append(x)
    return v


def _ohlc(n=300):
    cl = _serie(n)
    hi = [c * 1.006 for c in cl]
    lo = [c * 0.994 for c in cl]
    return hi, lo, cl


# ------------------------------------------------------------- alinhamento

def test_toda_funcao_devolve_o_comprimento_da_entrada():
    hi, lo, cl = _ohlc(300)
    simples = [I.sma(cl, 20), I.ema(cl, 20), I.rsi(cl), I.atr(hi, lo, cl)]
    for s in simples:
        assert len(s) == len(cl)
    for dic in (I.stoch_rsi(cl), I.macd(cl), I.bollinger(cl),
                I.donchian(hi, lo), I.gaussiano(hi, lo, cl)):
        for nome, s in dic.items():
            assert len(s) == len(cl), nome


@pytest.mark.parametrize("n", [0, 1, 5, 13])
def test_serie_curta_nao_levanta_e_sai_toda_nula(n):
    """Ausencia de aquecimento e ausencia de medicao, nunca zero."""
    hi, lo, cl = _ohlc(n)
    for s in (I.sma(cl, 20), I.ema(cl, 20), I.rsi(cl), I.atr(hi, lo, cl)):
        assert all(x is None for x in s)
    for dic in (I.stoch_rsi(cl), I.macd(cl), I.bollinger(cl),
                I.donchian(hi, lo), I.gaussiano(hi, lo, cl)):
        for nome, s in dic.items():
            assert all(x is None for x in s), nome


def test_o_aquecimento_e_exatamente_o_declarado():
    cl = _serie(300)
    assert I.sma(cl, 20).index(next(x for x in I.sma(cl, 20)
                                    if x is not None)) == 19
    assert sum(x is None for x in I.rsi(cl, 14)) == 14
    m = I.macd(cl)
    assert sum(x is None for x in m["macd"]) == I.MACD_LENTA - 1
    # o sinal aquece a partir de onde o MACD comeca, nao do inicio da serie
    assert (sum(x is None for x in m["sinal"])
            == I.MACD_LENTA - 1 + I.MACD_SINAL - 1)


# ----------------------------------------------------------------- valores

def test_sma_e_a_media_da_janela():
    v = [1.0, 2, 3, 4, 5, 6]
    assert I.sma(v, 3) == [None, None, 2.0, 3.0, 4.0, 5.0]


def test_ema_comeca_na_sma_e_nao_no_primeiro_ponto():
    v = _serie(60)
    e = I.ema(v, 10)
    assert e[9] == pytest.approx(sum(v[:10]) / 10)
    k = 2 / 11
    assert e[10] == pytest.approx(v[10] * k + e[9] * (1 - k))


def test_rsi_de_serie_so_de_alta_e_cem():
    """Perda media zero e medicao, nao divisao por zero."""
    assert I.rsi([100.0 + i for i in range(40)])[-1] == 100.0


def test_rsi_fica_sempre_entre_zero_e_cem():
    for semente in (3, 7, 11, 29):
        r = I.rsi(_serie(400, semente))
        assert all(0.0 <= x <= 100.0 for x in r if x is not None)


def test_rsi_usa_wilder_e_nao_media_simples():
    """Trocar a suavizacao muda o RSI em varios pontos sem levantar nada."""
    cl = _serie(200)
    n = 14
    g = [max(cl[i] - cl[i - 1], 0.0) for i in range(1, len(cl))]
    p = [max(cl[i - 1] - cl[i], 0.0) for i in range(1, len(cl))]
    mg, mp = sum(g[:n]) / n, sum(p[:n]) / n
    for i in range(n, len(g)):
        mg = (mg * (n - 1) + g[i]) / n
        mp = (mp * (n - 1) + p[i]) / n
    assert I.rsi(cl)[-1] == pytest.approx(100 - 100 / (1 + mg / mp))
    ingenuo = 100 - 100 / (1 + st.mean(g[-n:]) / st.mean(p[-n:]))
    assert abs(I.rsi(cl)[-1] - ingenuo) > 1e-6


def test_stoch_rsi_fica_entre_zero_e_cem():
    s = I.stoch_rsi(_serie(400))
    for nome in ("k", "d"):
        assert all(0.0 <= x <= 100.0 for x in s[nome] if x is not None)


def test_stoch_rsi_de_janela_achatada_sai_nulo_e_nao_cinquenta():
    """Publicar o meio afirmaria neutralidade medida onde nao houve variacao."""
    cl = [100.0 + i for i in range(80)]         # RSI trava em 100
    s = I.stoch_rsi(cl)
    assert s["k"][-1] is None


def test_macd_e_a_diferenca_das_duas_emas_e_o_hist_a_do_sinal():
    cl = _serie(300)
    er, el = I.ema(cl, 12), I.ema(cl, 26)
    m = I.macd(cl)
    for i in range(len(cl)):
        if er[i] is not None and el[i] is not None:
            assert m["macd"][i] == pytest.approx(er[i] - el[i])
        if m["macd"][i] is not None and m["sinal"][i] is not None:
            assert m["hist"][i] == pytest.approx(m["macd"][i] - m["sinal"][i])


def test_bollinger_usa_desvio_populacional_e_e_simetrico():
    cl = _serie(200)
    b = I.bollinger(cl, 20, 2.0)
    i = len(cl) - 1
    dp = st.pstdev(cl[i - 19:i + 1])
    assert b["sup"][i] == pytest.approx(b["meio"][i] + 2 * dp)
    assert b["sup"][i] - b["meio"][i] == pytest.approx(b["meio"][i] - b["inf"][i])
    amostral = st.stdev(cl[i - 19:i + 1])
    assert abs(dp - amostral) > 1e-9        # nao sao a mesma conta


def test_donchian_le_o_extremo_e_nao_o_fechamento():
    """A posicao sai de range no TOQUE, entao o canal tem de ver a maxima."""
    cl = [100.0] * 30
    hi = [100.0] * 30
    lo = [100.0] * 30
    hi[25] = 130.0                      # espeto que nao fechou la em cima
    d = I.donchian(hi, lo, 20)
    assert d["sup"][-1] == 130.0


def test_canal_gaussiano_alarga_com_amplitude_sem_o_fechamento_andar():
    """E o que o distingue do Bollinger: a banda e True Range filtrado."""
    n = 400
    cl = [100.0] * n
    calmo_hi, calmo_lo = [100.5] * n, [99.5] * n
    agitado_hi, agitado_lo = list(calmo_hi), list(calmo_lo)
    for i in range(300, n):
        agitado_hi[i], agitado_lo[i] = 105.0, 95.0
    calmo = I.gaussiano(calmo_hi, calmo_lo, cl)
    agitado = I.gaussiano(agitado_hi, agitado_lo, cl)
    largura_c = calmo["sup"][-1] - calmo["inf"][-1]
    largura_a = agitado["sup"][-1] - agitado["inf"][-1]
    assert largura_a > largura_c * 2
    # e o Bollinger, que so olha o fechamento, nao mexe uma vergula
    b = I.bollinger(cl)
    assert b["sup"][-1] == pytest.approx(b["inf"][-1])


# ------------------------------------------------------- ATR: as duas contas

def test_atr_modo_media_bate_exatamente_com_tecnica_atr():
    """A identidade que impede duas versoes divergirem em silencio."""
    hi, lo, cl = _ohlc(300)
    barras = [Barra(ts=i * 3600, abertura=cl[i], maxima=hi[i], minima=lo[i],
                    fechamento=cl[i], volume=0) for i in range(len(cl))]
    assert I.atr(hi, lo, cl, modo="media")[-1] == pytest.approx(T.atr(barras))


def test_atr_wilder_e_media_nao_sao_a_mesma_conta():
    hi, lo, cl = _ohlc(300)
    assert I.atr(hi, lo, cl)[-1] != pytest.approx(
        I.atr(hi, lo, cl, modo="media")[-1])


def test_atr_com_modo_desconhecido_levanta_em_vez_de_escolher_um():
    hi, lo, cl = _ohlc(60)
    with pytest.raises(ValueError):
        I.atr(hi, lo, cl, modo="exponencial")


# -------------------------------------------------------------- a fronteira

def test_decision_nao_importa_indicadores():
    """`indicadores` e LEITURA. O `POR_QUE_NAO` de `tecnica.py` reprovou RSI,
    MACD e medias para dimensionar faixa; o que os mantem fora da decisao nao
    pode ser so a prosa do cabecalho."""
    ofensores = []
    for py in (RAIZ / "src" / "defime" / "decision").rglob("*.py"):
        arvore = ast.parse(py.read_text(encoding="utf-8"))
        for no in ast.walk(arvore):
            if isinstance(no, ast.Import):
                nomes = [a.name for a in no.names]
            elif isinstance(no, ast.ImportFrom):
                nomes = [f"{no.module or ''}.{a.name}" for a in no.names]
            else:
                continue
            if any("indicadores" in n for n in nomes):
                ofensores.append(f"{py.name}:{no.lineno}")
    assert not ofensores, (
        "decision/ importou macro.indicadores: " + ", ".join(ofensores))
