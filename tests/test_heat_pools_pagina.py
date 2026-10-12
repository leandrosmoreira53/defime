"""`/heat/pools/` — as duas fontes (agregador e indexador opcional), o ranking por fee/h e a pagina."""
import json
import re
import sys
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ / "jobs"))
sys.path.insert(0, str(RAIZ))

from defime import heat_pools as HP                               # noqa: E402

from defime.web import heat_pools_render as R                                     # noqa: E402

EST = Path(__file__).parent / "fixtures" / "heat_pools_estado"
RWA = json.loads((EST / "rwa.json").read_text(encoding="utf-8"))
ATIVOS = json.loads((EST / "pools_ativos.json").read_text(encoding="utf-8"))["dados"]["ativos"]
AGORA = RWA["gerado_em"]
QUBT = "0x227bbce9a81b3694b01754298a983be3f9e44a93"


@pytest.fixture(scope="module")
def pools():
    return HP.complementar(HP.normalizar(RWA["dados"], AGORA), ATIVOS, AGORA)


# ------------------------------------------------------------ duas fontes

def test_pool_que_o_agregador_nao_lista_entra_pelo_indexador(pools):
    """24/09: o agregador nao listava a QUBT/USDG; ela entra pelo indexador, com volume estimado."""
    assert QUBT not in {r["pool_id"].lower() for r in RWA["dados"]["rwa"]}
    q = next(p for p in pools if p.pool_id == QUBT)
    assert q.fonte == "indexador" and "estimado" in q.selos
    assert q.vol_1h is None and q.aquecendo is None and q.concentracao is None


def test_volume_do_indexador_e_estimado_pelas_horas_amostradas(pools):
    q = next(p for p in pools if p.pool_id == QUBT)
    d = ATIVOS["QUBT"]["pools"][0]
    assert q.vol_24h == pytest.approx(d["volume_janela_usd"] / d["horas_amostradas"] * 24)
    assert q.apr_24h == d["apr_lp_pct"] and q.book_1pct == d["book_1pct_usd"]


def test_pool_das_duas_fontes_nao_duplica_e_ganha_a_conferencia(pools):
    ids = [p.pool_id.lower() for p in pools]
    assert len(ids) == len(set(ids))
    usos = [p for p in pools if p.pool_id.lower() == "0x02175608f1b5e6b5ed221ccfdc7be197d111d915"]
    assert len(usos) == 1 and usos[0].fonte == "agregador"
    assert usos[0].apr_idx is not None and usos[0].book_1pct


def test_indexador_sem_apr_nao_entra_no_padrao():
    ativos = {"XYZ": {"pools": [{"pool": "0xabc", "apr_lp_pct": None, "motivo_apr": "fee ilegivel",
                                 "horas_amostradas": 10, "volume_janela_usd": 100.0,
                                 "fee_lp_janela_usd": None, "tvl_usd": 50_000.0,
                                 "versao": "v3", "taxa_medida_pct": 0.3}]}}
    p = HP.complementar([], ativos, AGORA)[0]
    assert p.fora_do_padrao and "sem_taxa" in p.selos
    assert HP.filtrar([p])[0] == []


# ------------------------------------------------------------ o top

def _p(**kw):
    base = dict(pool_id="0x1", par="X / USDG", simbolo="X", usdg=True, versao="v3", dex=None,
                fee_pct=1.0, lp_pct=0.833, tvl=1e6, tvl_idx=None, vol_1h=None, vol_24h=2.4e6,
                apr_24h=None, apr_7d=None, apr_30d=None, idade_d=30.0)
    base.update(kw)
    return HP.Pool(**base)


def test_volume_7d_inverte_exatamente_o_apr_do_rwa():
    p = _p(vol_24h=1e6)
    p.apr_7d = 5e5 * 0.833 / 100 / 1e6 * 365 * 100          # 7 d a $500k/dia
    assert HP.volume_dia_7d(p) == pytest.approx(5e5)


def test_surto_de_um_dia_nao_decide_o_top():
    """O caso da PLTR 1% V4: 24 h muito acima da semana. Vale a semana."""
    surto = _p(pool_id="0xs", vol_24h=2.6e6)
    surto.apr_7d = 3e5 * 0.833 / 100 / 1e6 * 365 * 100
    firme = _p(pool_id="0xf", vol_24h=1e6)
    firme.apr_7d = 1e6 * 0.833 / 100 / 1e6 * 365 * 100
    t = HP.top_fee_hora([surto, firme], {"0xs": 1e5, "0xf": 1e5}, 1e4)
    assert [ln["pool"].pool_id for ln in t["linhas"]] == ["0xf", "0xs"]
    assert t["linhas"][1]["janela"] == "7 d"


def test_pool_esvaziando_tambem_usa_o_menor():
    p = _p(vol_24h=1e5)
    p.apr_7d = 1e6 * 0.833 / 100 / 1e6 * 365 * 100
    assert HP.volume_para_decidir(p) == (1e5, "24 h")


def test_o_custo_do_swap_usa_a_taxa_cheia_nao_a_do_lp():
    vistos = []
    HP.top_fee_hora([_p()], {"0x1": 1e5}, 1e4, custo=lambda c, b, t: vistos.append(t) or 1.0)
    assert vistos == [pytest.approx(0.01)]                  # 1%, nao 0,833%




# ------------------------------------------------------------ a pagina

@pytest.fixture(scope="module")
def pagina():
    from defime.jobs import heat_pools as J
    html, resumo = J.montar(EST, str(RAIZ / "tests" / "fixtures" / "sem_banco.db"), "", agora=AGORA + 600)
    return html, resumo


def test_pagina_sai_com_oito_mapas_e_as_duas_fontes(pagina):
    html, resumo = pagina
    assert html.count("<svg ") == 8
    assert resumo["indexador"] > 0 and resumo["agregador"] == 52
    assert "QUBT · 0,3% · V3" in html                      # a pool atual, nomeada


def test_grade_uniforme_nao_diz_area_igual_tvl(pagina):
    """Legenda que afirma o que o desenho nao faz: 'area = TVL' sobre tiles
    do mesmo tamanho."""
    html, _ = pagina
    m = re.search(r'id="hp-nota">([^<]*)<', html)
    assert m and "MESMO tamanho" in m.group(1) and "área = TVL" not in m.group(1)


def test_estado_velho_e_dito(tmp_path):
    from defime.jobs import heat_pools as J
    for f in EST.iterdir():
        (tmp_path / f.name).write_bytes(f.read_bytes())
    html, _ = J.montar(tmp_path, str(RAIZ / "tests" / "fixtures" / "sem_banco.db"), "", agora=AGORA + 4 * 3600)
    assert "o job do /rwa/ não rodou" in html


def test_sem_rwa_nao_publica_mapa_vazio(tmp_path):
    from defime.jobs import heat_pools as J
    with pytest.raises(RuntimeError, match="sem rwa.json"):
        J.montar(tmp_path, str(RAIZ / "tests" / "fixtures" / "sem_banco.db"), "", agora=AGORA)


def test_o_mapa_sai_sem_o_indexador(tmp_path):
    from defime.jobs import heat_pools as J
    (tmp_path / "rwa.json").write_bytes((EST / "rwa.json").read_bytes())
    html, resumo = J.montar(tmp_path, str(tmp_path / "sem_banco.db"), "", agora=AGORA)
    assert html.count("<svg ") == 8 and resumo["indexador"] == 0
    assert "pools_ativos indisponível" in html


def test_cor_sem_valor_e_cinza_e_nao_a_do_menor():
    assert R.cor(None, "log") != R.cor(0.0, "log")
    assert R.cor(1.0, "divergente") != R.cor(-1.0, "divergente")


def test_job_nao_faz_rede():
    import ast
    src = (RAIZ / "src" / "defime" / "jobs" / "heat_pools.py").read_text(encoding="utf-8")
    nomes = {a.name.split(".")[0] for n in ast.walk(ast.parse(src))
             if isinstance(n, (ast.Import, ast.ImportFrom))
             for a in (n.names if isinstance(n, ast.Import) else [ast.alias(n.module or "")])}
    assert not nomes & {"requests", "httpx", "urllib", "socket", "web3"}
    assert "RpcClient" not in src
