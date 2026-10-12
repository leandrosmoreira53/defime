"""O heatmap: geometria do treemap e as recusas de publicar.

**Os testes de geometria sao os que valem.** Um treemap com erro sutil —
retangulos que se sobrepoem, area que nao fecha, tile fora do container —
continua parecendo um heatmap. Nao ha como ver a olho que a area do NVDA
deveria ser 8% maior. Por isso as invariantes sao verificadas como
invariantes, sobre entradas variadas, e nao contra um layout esperado escrito
a mao.
"""

from __future__ import annotations

import pytest

from defime import heatmap as H

L, A = 100.0, 62.0


def _serie(simbolo, tvl, vol=1000.0, preco=10.0, var=1.0, horas=24.0,
           motivo=None):
    return {"simbolo": simbolo, "tvl_usd": tvl, "volume_24h_usd": vol,
            "preco": preco, "variacao_24h_pct": var, "horas_cobertas": horas,
            "motivo": motivo}


# --------------------------------------------------------------- geometria

CASOS = [
    [10.0],
    [10.0, 10.0],
    [50.0, 30.0, 20.0],
    [100.0, 24.0, 12.0, 8.0, 6.0, 3.0, 1.0],
    [5.0] * 16,
    [1000.0, 1.0, 1.0, 1.0],                  # um domina tudo
    [3.0, 3.0, 3.0, 3.0, 3.0, 3.0, 3.0, 3.0, 3.0, 3.0, 3.0],
]


@pytest.mark.parametrize("valores", CASOS)
def test_as_areas_somam_o_container(valores):
    caixas = H.squarify(sorted(valores, reverse=True), 0, 0, L, A)
    area = sum(w * h for _, _, w, h in caixas)
    assert area == pytest.approx(L * A, rel=1e-9)


@pytest.mark.parametrize("valores", CASOS)
def test_a_area_de_cada_tile_e_proporcional_ao_valor(valores):
    vs = sorted(valores, reverse=True)
    caixas = H.squarify(vs, 0, 0, L, A)
    escala = (L * A) / sum(vs)
    for v, (_, _, w, h) in zip(vs, caixas):
        assert w * h == pytest.approx(v * escala, rel=1e-9)


@pytest.mark.parametrize("valores", CASOS)
def test_nenhum_tile_sai_do_container(valores):
    for x, y, w, h in H.squarify(sorted(valores, reverse=True), 0, 0, L, A):
        assert x >= -1e-9 and y >= -1e-9
        assert x + w <= L + 1e-9
        assert y + h <= A + 1e-9


@pytest.mark.parametrize("valores", CASOS)
def test_nenhum_par_de_tiles_se_sobrepoe(valores):
    caixas = H.squarify(sorted(valores, reverse=True), 0, 0, L, A)
    for i, (x1, y1, w1, h1) in enumerate(caixas):
        for x2, y2, w2, h2 in caixas[i + 1:]:
            # sem sobreposicao se um esta inteiramente de um lado do outro
            separados = (x1 + w1 <= x2 + 1e-9 or x2 + w2 <= x1 + 1e-9
                         or y1 + h1 <= y2 + 1e-9 or y2 + h2 <= y1 + 1e-9)
            assert separados, f"sobrepostos: {caixas[i]} e {(x2,y2,w2,h2)}"


def test_o_squarified_ganha_do_corte_ingenuo_em_razao_de_aspecto():
    """Se o layout nao produzisse tiles proximos do quadrado, nao haveria
    motivo para o algoritmo existir — uma fatia de 1 px de altura nao cabe o
    simbolo do token."""
    vs = sorted([100.0, 60.0, 40.0, 25.0, 15.0, 10.0, 6.0, 4.0], reverse=True)
    caixas = H.squarify(vs, 0, 0, L, A)
    pior = max(max(w, h) / min(w, h) for _, _, w, h in caixas)
    ingenuo = max(L, A / len(vs)) / min(L, A / len(vs))   # fatias horizontais
    assert pior < ingenuo
    assert pior < 6.0


def test_container_ou_lista_vazia_nao_levanta():
    assert H.squarify([], 0, 0, L, A) == []
    assert all(w == 0 for _, _, w, _ in H.squarify([1.0, 2.0], 0, 0, 0, A))
    assert all(h == 0 for _, _, _, h in H.squarify([1.0], 0, 0, L, 0))
    assert all(w == 0 for _, _, w, _ in H.squarify([0.0, 0.0], 0, 0, L, A))


# ------------------------------------------------- agregacao por TOKEN

def test_pools_do_mesmo_token_viram_um_tile_com_tvl_somado():
    """Tres pools de SPY sao um ativo, nao tres."""
    t = H.agregar({
        "0xaaa": _serie("SPY", 4_000_000, vol=200_000, preco=660.0, var=0.5),
        "0xbbb": _serie("SPY", 1_000_000, vol=900_000, preco=659.0, var=0.9),
        "0xccc": _serie("MU", 900_000, vol=300_000, preco=1000.0, var=3.0),
    })
    spy = next(x for x in t if x.simbolo == "SPY")
    assert spy.tvl_usd == 5_000_000
    assert spy.volume_24h_usd == 1_100_000
    assert spy.pools == 2


def test_preco_e_variacao_saem_da_pool_dominante_e_ela_e_nomeada():
    """Media de preco entre pools de liquidez diferente e um numero que
    ninguem negocia. O tile tem de poder ser rastreado ate uma linha."""
    t = H.agregar({
        "0xgrande": _serie("SPY", 4_000_000, preco=660.0, var=0.5),
        "0xrasa": _serie("SPY", 1_000.0, preco=612.0, var=9.9),
    })
    spy = t[0]
    assert spy.preco == 660.0
    assert spy.variacao_24h_pct == 0.5
    assert spy.pool_dominante == "0xgrande"


def test_token_sem_simbolo_resolvido_fica_de_fora():
    t = H.agregar({"0xaaa": _serie("?", 9_000_000),
                   "0xbbb": _serie("", 9_000_000),
                   "0xccc": _serie("MU", 10.0)})
    assert [x.simbolo for x in t] == ["MU"]


# ------------------------------------------- a variacao que nao se publica

def test_serie_curta_nao_vira_variacao_de_24h():
    """Um pedaco do dia rotulado como o dia e a familia do APR de 1,6 hora."""
    t = H.agregar({"0xa": _serie("MU", 1e6, var=4.0, horas=6.0)})
    assert t[0].variacao_24h_pct is None
    assert "6.0 h" in t[0].motivo_sem_variacao


def test_variacao_ausente_nao_vira_zero():
    """Zero afirmaria que o token nao andou."""
    t = H.agregar({"0xa": _serie("MU", 1e6, var=None, motivo="sem preco")})
    assert t[0].variacao_24h_pct is None
    assert t[0].motivo_sem_variacao == "sem preco"


def test_serie_no_limite_publica():
    t = H.agregar({"0xa": _serie("MU", 1e6, var=2.0, horas=H.JANELA_MIN_H)})
    assert t[0].variacao_24h_pct == 2.0


# ----------------------------------------------------------- escala de cor

def test_o_teto_da_cor_ignora_o_espeto_e_tem_piso():
    tokens = H.agregar({f"0x{i}": _serie(f"T{i}", 1e6, var=0.3)
                        for i in range(9)}
                       | {"0xz": _serie("ESPETO", 1e6, var=40.0)})
    teto = H.teto_de_cor(tokens)
    assert teto == pytest.approx(H.COR_TETO_MIN)      # piso, nao os 40%
    assert H.intensidade(40.0, teto) == 1.0           # satura, nao estoura
    assert H.intensidade(0.3, teto) < 0.2


def test_dia_agitado_levanta_o_teto_acima_do_piso():
    tokens = H.agregar({f"0x{i}": _serie(f"T{i}", 1e6, var=8.0)
                        for i in range(10)})
    assert H.teto_de_cor(tokens) == pytest.approx(8.0)


def test_intensidade_de_variacao_ausente_e_zero_e_nao_levanta():
    assert H.intensidade(None, 3.0) == 0.0
    assert H.intensidade(1.0, 0.0) == 0.0


# ---------------------------------------------------------------- montar

def _tokens():
    return H.agregar({
        "0xa": _serie("NVDA", 5_762_685, vol=6_931_293, var=0.41),
        "0xb": _serie("SPY", 4_375_338, vol=212_021, var=0.13),
        "0xc": _serie("USO", 1_639_630, vol=2_107_877, var=-2.90),
        "0xd": _serie("MU", 966_841, vol=324_979, var=3.89),
    })


def test_a_area_por_tvl_e_por_volume_reordenam_os_tiles():
    """A troca de lugar entre as duas visoes e informacao: pool grande e
    parada contra pequena e agitada."""
    por_tvl = H.montar(_tokens(), por="tvl")
    por_vol = H.montar(_tokens(), por="volume")
    assert [t.simbolo for t in por_tvl["tiles"]][:2] == ["NVDA", "SPY"]
    assert [t.simbolo for t in por_vol["tiles"]][:2] == ["NVDA", "USO"]


def test_token_sem_o_campo_escolhido_sai_CONTADO_e_nao_sumido():
    tokens = H.agregar({"0xa": _serie("MU", 1e6, vol=0.0),
                        "0xb": _serie("NVDA", 1e6, vol=5e5)})
    m = H.montar(tokens, por="volume")
    assert [t.simbolo for t in m["tiles"]] == ["NVDA"]
    assert m["fora"] == ["MU"]


def test_montar_publica_o_teto_da_cor_para_a_legenda():
    m = H.montar(_tokens(), por="tvl")
    assert m["teto_cor_pct"] >= H.COR_TETO_MIN
    assert m["total_usd"] == pytest.approx(
        sum(t.tvl_usd for t in m["tiles"]))


def test_criterio_desconhecido_levanta_em_vez_de_escolher_um():
    with pytest.raises(ValueError):
        H.montar(_tokens(), por="fee")


def test_o_layout_de_montar_respeita_as_mesmas_invariantes():
    m = H.montar(_tokens(), por="tvl", largura=L, altura=A)
    assert sum(t.largura * t.altura for t in m["tiles"]) == pytest.approx(
        L * A, rel=1e-9)
    for t in m["tiles"]:
        assert t.x + t.largura <= L + 1e-9
        assert t.y + t.altura <= A + 1e-9
