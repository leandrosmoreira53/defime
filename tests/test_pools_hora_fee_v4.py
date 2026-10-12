"""A fee da V4 em `pool_hora` usa a taxa do LP, nao o 5/6 da V3.

Os numeros sao os de 24/09: `fee` total do evento (pool_param.taxa medida) e o
lpFee da PoolKey, contra a taxa do LP lida do contrato pelo coletor RWA.
"""
import ast
import sys
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[1]

from defime.jobs.pools_hora import taxa_lp_v4, V4_TAXA_DINAMICA   # noqa: E402


@pytest.mark.parametrize("total, lp, esperado", [
    (3499, 3000, 0.002999),     # UPS, TSLA, GOOGL, AAPL, SPY 0,30%: pf 500
    (10990, 10000, 0.00999),    # PLTR, SPCX 1%: pf 1000
    (2899, 2500, 0.002499),     # MSTR 0,25%: pf 400 (contrato: 0,2499%)
    (625, 500, 0.0005),         # SPY 0,05%: pf 125
    (3000, 3000, 0.003),        # sem taxa de protocolo
])
def test_taxa_do_lp_bate_com_o_contrato(total, lp, esperado):
    assert taxa_lp_v4(total, lp) == pytest.approx(esperado, rel=2e-4)


def test_o_5_sextos_da_v3_errava_nas_v4():
    # A conta antiga, total x 5/6, contra a do LP: -8,3% na de 1%.
    antigo = 10990 / 1e6 * 5 / 6
    assert antigo / taxa_lp_v4(10990, 10000) - 1 == pytest.approx(-0.0833, abs=1e-3)


def test_taxa_dinamica_nao_tem_numero():
    assert taxa_lp_v4(2500, V4_TAXA_DINAMICA) is None


def test_sem_chave_nao_supoe():
    assert taxa_lp_v4(3499, None) is None


def test_total_menor_que_o_lpfee_e_chave_errada():
    assert taxa_lp_v4(2000, 3000) is None


def test_v4_sem_taxa_do_lp_zera_para_None_e_nao_para_0():
    """Uma hora com UM swap sem taxa legivel nao publica fee parcial."""
    src = (RAIZ / "src" / "defime" / "jobs" / "pools_hora.py").read_text(encoding="utf-8")
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef) and n.name == "_volume")
    corpo = ast.unparse(fn)
    # a V3 tambem deixou o 5/6 suposto em 24/09 (tests/test_taxa_lp.py)
    assert "fracao_lp_v3" in corpo and "taxa_lp_v4" in corpo and "5 / 6" not in corpo
    assert "fee = None" in corpo
