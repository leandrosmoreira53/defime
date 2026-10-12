"""`defime.heat_pools` — o heatmap das pools RWA (o plano do heatmap, Fase 3).

A fixture e o `rwa.json` REAL de 24/09 03:27 CEST (52 pools, primeiro ponto
da serie). A concentracao e testada com serie sintetica, porque a real ainda
tem um ponto so — que e justamente o caso "medindo".
"""
import json
import math
from pathlib import Path

import pytest

from defime import heat_pools as H

FIX = json.loads((Path(__file__).parent / "fixtures" / "heat_pools_rwa.json")
                 .read_text(encoding="utf-8"))
AGORA = FIX["gerado_em"]
DADOS = FIX["dados"]


def _por_id(pools, prefixo):
    return next(p for p in pools if p.pool_id.lower().startswith(prefixo))


@pytest.fixture(scope="module")
def pools():
    return H.normalizar(DADOS, AGORA)


# ------------------------------------------------------------ o dado real

def test_uma_pool_por_linha_e_o_tile_e_a_pool(pools):
    assert len(pools) == len(DADOS["rwa"]) == 52
    spy_usdg = [p for p in pools if p.simbolo == "SPY" and p.usdg]
    assert len(spy_usdg) >= 3                       # tres SPY viram tres tiles, nao um


def test_apr_e_giro_vezes_taxa_vezes_365(pools):
    """A identidade que a legenda publica: APR nao e evidencia independente."""
    n = 0
    for p in pools:
        if p.apr_24h is None or p.lp_pct is None or p.giro is None:
            continue
        assert p.apr_24h == pytest.approx(p.giro * p.lp_pct * 365, rel=1e-9)
        n += 1
    assert n >= 30


def test_up_v3_com_taxa_nominal_sai_do_mapa_padrao(pools):
    nvda = _por_id(pools, "0x18a5af4e44")           # 1.061% de APR com a taxa NOMINAL
    assert nvda.lp_pct is None and "taxa_nominal" in nvda.selos
    assert nvda.fora_do_padrao and "nao legivel" in nvda.fora_do_padrao
    dentro, fora = H.filtrar(pools)
    assert nvda not in dentro
    assert any(nvda.rotulo in v for v in fora.values())
    dentro2, _ = H.filtrar(pools, incluir_outros=True)
    assert nvda in dentro2                          # atras do botao, nunca sumida


def test_hook_nao_tem_apr_e_nao_vira_zero(pools):
    spy = _por_id(pools, "0x8674c1c554")
    assert "sem_taxa" in spy.selos and spy.apr_24h is None
    assert spy.fora_do_padrao == "taxa dinamica (hook)"
    m = H.montar(H.filtrar(pools, incluir_outros=True)[0], "apr")
    t = next(t for t in m["tiles"] if t["pool"] is spy)
    assert t["valor"] is None and t["intensidade"] is None     # cinza, nao "0%"
    assert spy.rotulo in m["sem_valor"]


def test_pares_sem_usdg_ficam_atras_do_botao(pools):
    dentro, fora = H.filtrar(pools)
    assert all(p.usdg for p in dentro)
    assert len(fora.get("par sem USDG", [])) == sum(not p.usdg for p in pools) > 0


def test_mapa_padrao_de_hoje(pools):
    """45 USDG menos 5 sem taxa legivel (2 hooks, 2 up-v3, 1 alandale) = 40."""
    dentro, fora = H.filtrar(pools)
    assert len(dentro) == 40
    assert sum(len(v) for v in fora.values()) == 12


def test_pltr_ainda_sem_serie_nao_ganha_selo_de_surto(pools):
    """Com um ponto so a concentracao e "medindo" (None), nunca "ok"."""
    pltr = _por_id(pools, "0xee430ee100")
    assert pltr.pontos_serie == 1 and pltr.concentracao is None
    assert "surto" not in pltr.selos


def test_rotulo_distingue_as_pools_do_mesmo_ativo(pools):
    rot = {p.pool_id[:10]: p.rotulo for p in pools}
    assert rot["0xe5923c8a"] == "SPY · 0,05% · V4"
    assert rot["0xddcbba36"] == "SPY/WETH · 0,05% · V3"         # o par so quando nao e USDG
    assert rot["0x38453c11"] == "SPY · 0,01% · RAMSES V3"       # o AMM so quando nao e Uniswap
    assert rot["0x960f79d8"] == "META · 0,3% · RAMSES V3"       # nao "V3": o LP ali fica com 0,044%
    assert rot["0x18a5af4e"] == "NVDA · 0,3% · UP V3"


def test_rotulos_nao_repetem_nem_com_os_outros_pares(pools):
    """Dois tiles com o mesmo nome seriam lidos como a mesma pool. Com o par
    (quando nao e USDG) e o AMM (quando nao e Uniswap) no rotulo, as 52 de
    24/09 saem todas distintas."""
    dentro, _ = H.filtrar(pools, incluir_outros=True)
    rot = [p.rotulo for p in dentro]
    assert len(rot) == 52 and len(set(rot)) == len(rot)


# ------------------------------------------------------------ concentracao

def _serie(horas, pico=None):
    s = [[AGORA - i * 3600, 100.0] for i in range(horas)]
    if pico is not None:
        s[0][1] = pico
    return s


def test_concentracao_precisa_de_18_pontos():
    assert H.concentracao(_serie(17), 2400.0, AGORA) == (None, 17)
    c, n = H.concentracao(_serie(18), 1800.0, AGORA)
    assert n == 18 and c == pytest.approx(100 / 1800)


def test_ponto_fora_da_janela_nao_conta():
    s = _serie(18) + [[AGORA - 30 * 3600, 99_999.0]]
    c, n = H.concentracao(s, 1800.0, AGORA)
    assert n == 18 and c == pytest.approx(100 / 1800)


def test_surto_pelo_caso_da_pltr():
    """PLTR 1% V4 em 24/09: 71% do dia numa hora."""
    d = {"rwa": [{"pool_id": "0xp", "nome": "PLTR / USDG", "tvl": 975_000, "fee": 1.0,
                  "lp": 0.999, "vol_1h": 860.0, "vol_24h": 2_612_000.0, "apr_24h": 977.0,
                  "versao": "v4", "serie_vol_1h": _serie(20, pico=0.71 * 2_612_000)}]}
    p = H.normalizar(d, AGORA)[0]
    assert p.concentracao == pytest.approx(0.71) and "surto" in p.selos
    assert p.aquecendo == pytest.approx(860 * 24 / 2_612_000)     # 0,008x: o surto passou


def test_rdd_logo_abaixo_do_limiar_nao_e_surto():
    d = {"rwa": [{"pool_id": "0xr", "nome": "RDDT / USDG", "tvl": 1e6, "lp": 0.833,
                  "vol_1h": 1.0, "vol_24h": 1000.0, "serie_vol_1h": _serie(24, pico=490.0)}]}
    assert "surto" not in H.normalizar(d, AGORA)[0].selos


# ------------------------------------------------------------ outros selos

def _uma(**kw):
    r = {"pool_id": "0x1", "nome": "MU / USDG", "tvl": 500_000.0, "fee": 0.3, "lp": 0.25,
         "vol_1h": 100.0, "vol_24h": 2400.0, "apr_24h": 1.0, "versao": "v3"}
    r.update(kw)
    return H.normalizar({"rwa": [r]}, AGORA)[0]


def test_pool_nova_ganha_selo():
    import datetime as dt
    ontem = dt.datetime.fromtimestamp(AGORA - 86400, dt.timezone.utc).isoformat()
    assert "nova" in _uma(criada=ontem).selos
    assert "nova" not in _uma(criada="2026-07-21T11:02:06+00:00").selos


def test_tvl_divergente_ganha_selo_e_sem_o_segundo_lado_nao():
    assert "tvl_diverge" in _uma(tvl_idx=300_000.0).selos         # -40%
    assert "tvl_diverge" not in _uma(tvl_idx=450_000.0).selos     # -10%
    p = _uma(tvl_idx=None)
    assert p.div_tvl_pct is None and "tvl_diverge" not in p.selos


def test_tvl_baixo_sai_contado():
    dentro, fora = H.filtrar([_uma(tvl=5_000.0)])
    assert dentro == [] and list(fora) == ["TVL abaixo de $10.000"]


def test_sem_volume_24h_nao_inventa_giro_nem_aquecimento():
    p = _uma(vol_24h=None, vol_1h=None)
    assert p.giro is None and p.aquecendo is None


# ------------------------------------------------------------ escalas

def test_escala_log_satura_nos_limites_escritos():
    e = H.escala_log([10.0 * 2 ** i for i in range(11)])
    assert e["piso"] == 20.0 and e["teto"] == 10.0 * 2 ** 9
    assert H.intensidade(1.0, e) == 0.0 and H.intensidade(1e9, e) == 1.0
    meio = math.sqrt(e["piso"] * e["teto"])
    assert H.intensidade(meio, e) == pytest.approx(0.5)


def test_pool_parada_na_ultima_hora_nao_esticca_a_escala():
    """24/09: com as pools a 0,00x o p90 ia a 36x e a META a 3,8x ficava palida."""
    e = H.escala_divergente([0.001] * 9 + [3.8])
    assert e["teto_log2"] == H.DIV_TETO_MAX_LOG2
    assert H.intensidade(3.8, e) > 0.95


def test_escala_divergente_centrada_em_1x_com_piso_de_2x():
    e = H.escala_divergente([1.1, 0.9, 1.2])                      # dia calmo
    assert e["teto_log2"] == 1.0
    assert H.intensidade(1.0, e) == 0.0
    assert H.intensidade(2.0, e) == 1.0 and H.intensidade(0.5, e) == -1.0
    assert H.intensidade(0.0, e) == -1.0 and H.intensidade(None, e) is None


# ------------------------------------------------------------ layout

@pytest.mark.parametrize("aba", list(H.ABAS))
def test_layout_preenche_o_container_sem_sobrepor(pools, aba):
    dentro, _ = H.filtrar(pools)
    m = H.montar(dentro, aba)
    ts = m["tiles"]
    assert sum(t["l"] * t["a"] for t in ts) == pytest.approx(100 * 62)
    for i, a in enumerate(ts):
        assert -1e-9 <= a["x"] and a["x"] + a["l"] <= 100 + 1e-9
        for b in ts[i + 1:]:
            sx = min(a["x"] + a["l"], b["x"] + b["l"]) - max(a["x"], b["x"])
            sy = min(a["y"] + a["a"], b["y"] + b["a"]) - max(a["y"], b["y"])
            assert sx <= 1e-9 or sy <= 1e-9


def test_area_proporcional_ao_campo_da_aba(pools):
    dentro, _ = H.filtrar(pools)
    m = H.montar(dentro, "volume")
    assert not m["grade_uniforme"]
    total = sum(t["pool"].vol_24h for t in m["tiles"])
    for t in m["tiles"]:
        assert t["l"] * t["a"] == pytest.approx(t["pool"].vol_24h / total * 6200, rel=1e-6)


def test_razao_extrema_vira_grade_uniforme():
    ps = [_uma(pool_id="0xa", tvl=1e7), _uma(pool_id="0xb", tvl=1e4)]
    m = H.montar(ps, "apr")
    assert m["grade_uniforme"]
    assert m["tiles"][0]["l"] * m["tiles"][0]["a"] == pytest.approx(3100)


def test_aba_desconhecida_levanta():
    with pytest.raises(ValueError):
        H.montar([], "tvl")


# ------------------------------------------------------------ top para o capital

def test_top_usa_a_forma_fechada_da_fatia():
    p = _uma(vol_24h=240_000.0)                   # $10k/h
    t = H.top_fee_hora([p], {"0x1": 90_000.0}, 10_000.0)
    ln = t["linhas"][0]
    assert ln["fatia"] == pytest.approx(0.10)
    assert ln["fee_h"] == pytest.approx(10_000 * 0.0025 * 0.10)


def test_top_paga_em_com_custo_injetado():
    p = _uma(vol_24h=240_000.0)
    t = H.top_fee_hora([p], {"0x1": 90_000.0}, 10_000.0,
                       custo=lambda c, b, tx: 50.0, fee_h_atual=0.5)
    ln = t["linhas"][0]
    assert ln["ganho_h"] == pytest.approx(2.0) and ln["paga_em_h"] == pytest.approx(25.0)


def test_top_nao_paga_se_nao_ganha():
    t = H.top_fee_hora([_uma(vol_24h=240_000.0)], {"0x1": 90_000.0}, 10_000.0,
                       custo=lambda c, b, tx: 50.0, fee_h_atual=99.0)
    assert t["linhas"][0]["paga_em_h"] is None


def test_top_deixa_fora_e_conta():
    import datetime as dt
    ontem = dt.datetime.fromtimestamp(AGORA - 86400, dt.timezone.utc).isoformat()
    ps = [_uma(pool_id="0xsembook"), _uma(pool_id="0xnova", criada=ontem),
          _uma(pool_id="0xhook", lp=None, marca="hook")]
    t = H.top_fee_hora(ps, {"0xnova": 1e5, "0xhook": 1e5}, 10_000.0)
    assert t["linhas"] == [] and t["avaliadas"] == 0
    assert set(t["fora"]) == {"sem book +-1% medido pelo indexador", "selo nova",
                              "sem taxa do LP legivel"}


def test_modulo_e_puro():
    import ast
    src = Path(H.__file__).read_text(encoding="utf-8")
    nomes = {a.name.split(".")[0] for n in ast.walk(ast.parse(src))
             if isinstance(n, (ast.Import, ast.ImportFrom))
             for a in (n.names if isinstance(n, ast.Import) else [ast.alias(n.module or "")])}
    assert not nomes & {"sqlite3", "requests", "httpx", "urllib", "socket", "os", "pathlib"}
