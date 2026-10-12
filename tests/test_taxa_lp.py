"""A fracao do LP na V3 sai do `feeProtocol` LIDO, nao do 5/6 suposto.

Ancora: `tests/fixtures/fee_protocol_v3.json`, o `slot0` das 29 V3 do banco
no bloco 71.028.140 — 0x44 nas tres de 0,05%, 0x66 nas outras 26.
"""
import ast
import json
import sys
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ / "jobs"))
sys.path.insert(0, str(RAIZ))

from defime.core.taxa_lp import (                                  # noqa: E402
    fee_protocol_do_slot0, fracao_lp_v3, fracao_protocolo_v3)

MEDIDO = json.loads((RAIZ / "tests" / "fixtures" / "fee_protocol_v3.json").read_text())


def test_0x66_e_5_sextos_e_0x44_e_3_quartos():
    assert fracao_lp_v3(0x66) == pytest.approx(5 / 6)
    assert fracao_lp_v3(0x44) == pytest.approx(3 / 4)
    assert fracao_lp_v3(0x00) == 1.0                     # protocolo desligado


def test_o_erro_que_existia_nas_de_0_05_era_de_11_por_cento():
    assert (5 / 6) / fracao_lp_v3(0x44) - 1 == pytest.approx(0.1111, abs=1e-4)


def test_cada_sentido_le_o_seu_nibble():
    fp = 0x64                                            # token0 1/4, token1 1/6
    assert fracao_protocolo_v3(fp, zero_for_one=True) == pytest.approx(1 / 4)
    assert fracao_protocolo_v3(fp, zero_for_one=False) == pytest.approx(1 / 6)


def test_sem_sentido_e_lados_diferentes_nao_escolhe_um():
    with pytest.raises(ValueError, match="lados diferentes"):
        fracao_lp_v3(0x64)


@pytest.mark.parametrize("fp", [0x33, 0x0B, 0x1F, 256, -1])
def test_byte_impossivel_levanta_em_vez_de_publicar(fp):
    """O contrato so aceita 0 ou 4..10 por nibble: fora disso e leitura errada."""
    with pytest.raises(ValueError):
        fracao_lp_v3(fp, True)


def test_le_a_sexta_palavra_do_slot0():
    palavras = [1234, 5678, 0, 1, 1, 0x44, 1]
    s0 = "0x" + "".join(f"{w:064x}" for w in palavras)
    assert fee_protocol_do_slot0(s0) == 0x44


def test_a_medicao_de_24_09_e_a_que_o_codigo_espera():
    por_taxa = {}
    for p in MEDIDO["pools"]:
        por_taxa.setdefault(p["taxa_pct"], set()).add(p["fee_protocol"])
    assert por_taxa[0.05] == {0x44} and por_taxa[0.3] == {0x66} and por_taxa[1.0] == {0x66}








