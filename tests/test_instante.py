"""Guarda de instante. Sem rede, sem banco.

Cada teste aqui reproduz UM dos cinco casos reais em que este projeto pareou
dados de momentos diferentes. Se a guarda nao pega esses cinco, ela nao serve
para nada — foram eles que a motivaram.
"""

from __future__ import annotations

import pytest

from defime.core.instante import (Instante, InstantesIncompativeis,
                                  compativel, exigir_compativel,
                                  exigir_mesmo_ponto_da_chain,
                                  idade_do_conjunto_s, mais_velho)


# ------------------------------------------------------ os casos reais

def test_caso_g1_preco_de_agora_contra_fechamento_de_sexta():
    """2026-08-23: pool a $134,30 contra referencia parada em $126,85.

    O G4 chamou de "basis anomalo de +5,83%" um basis que so era anomalo
    porque comparava agora com o fechamento de sexta.
    """
    pool = Instante(ts=1_787_487_343, bloco=44_277_078, rotulo="preco da pool")
    quote = Instante(ts=1_787_487_343 - 45 * 3600, rotulo="ultimo preco novo")
    with pytest.raises(InstantesIncompativeis) as e:
        exigir_compativel(pool, quote, tolerancia_s=1800)
    assert "preco da pool" in str(e.value)
    assert "ultimo preco novo" in str(e.value)


def test_caso_check2_reconstrucao_de_agora_contra_golden_congelado():
    """O check-2 acusava 100% de divergencia num bug que nao existia."""
    agora = Instante(ts=1_787_487_343, bloco=44_277_078, rotulo="reconstrucao")
    golden = Instante(ts=1_786_000_000, bloco=37_318_521, rotulo="golden file")
    with pytest.raises(InstantesIncompativeis):
        exigir_compativel(agora, golden, tolerancia_s=900)


def test_caso_check_history_1_mesmo_bloco_ordem_diferente():
    """0,170419%: um `Mint` no log_index 50 depois do ultimo `Swap` no 19.

    Aqui tolerancia em segundos NAO ajuda — os dois estao no mesmo bloco e no
    mesmo segundo. So a chave `(bloco, log_index)` separa.
    """
    swap = Instante(ts=1_780_000_000, bloco=40_113_181, log_index=19,
                    rotulo="ultimo Swap")
    mint = Instante(ts=1_780_000_000, bloco=40_113_181, log_index=50,
                    rotulo="Mint posterior")
    exigir_compativel(swap, mint, tolerancia_s=0)      # em segundos, passam
    with pytest.raises(InstantesIncompativeis) as e:
        exigir_mesmo_ponto_da_chain(swap, mint)
    assert "ORDEM importa" in str(e.value)


def test_caso_check4_blocos_diferentes_na_reconciliacao():
    a = Instante(ts=1_780_000_000, bloco=40_113_181, log_index=0, rotulo="livro")
    b = Instante(ts=1_780_000_000, bloco=40_113_500, log_index=0, rotulo="slot0")
    with pytest.raises(InstantesIncompativeis):
        exigir_mesmo_ponto_da_chain(a, b)


# ------------------------------------------------------------ tolerancia

def test_dentro_da_tolerancia_passa():
    a = Instante(ts=1_000_000, rotulo="a")
    b = Instante(ts=1_000_600, rotulo="b")
    exigir_compativel(a, b, tolerancia_s=900)
    assert compativel(a, b, tolerancia_s=900)


def test_tolerancia_e_obrigatoria_e_nao_tem_default():
    """"Proximo o bastante" depende do que se esta pareando.

    15min serve para basis e seria absurdo para reconciliar liquidez.
    """
    import inspect
    sig = inspect.signature(exigir_compativel)
    p = sig.parameters["tolerancia_s"]
    assert p.default is inspect.Parameter.empty
    assert p.kind is inspect.Parameter.KEYWORD_ONLY


def test_compara_todos_os_pares_e_nao_so_os_vizinhos():
    """A~B e B~C nao implica A~C, e e o par A-C que estraga a conta."""
    a = Instante(ts=0, rotulo="a")
    b = Instante(ts=900, rotulo="b")
    c = Instante(ts=1800, rotulo="c")
    with pytest.raises(InstantesIncompativeis) as e:
        exigir_compativel(a, b, c, tolerancia_s=900)
    assert "a" in str(e.value) and "c" in str(e.value)


def test_tolerancia_negativa_e_erro_de_uso():
    with pytest.raises(ValueError):
        exigir_compativel(Instante(ts=0), tolerancia_s=-1)


def test_um_instante_so_nunca_falha():
    exigir_compativel(Instante(ts=0, rotulo="unico"), tolerancia_s=0)


# ------------------------------------------------------------- frescor

def test_idade_do_conjunto_e_a_do_mais_velho():
    """O dado mais velho e o que limita o que se pode afirmar."""
    novo = Instante(ts=1_000_000, rotulo="novo")
    velho = Instante(ts=1_000_000 - 3600, rotulo="velho")
    assert mais_velho(novo, velho) is velho
    assert idade_do_conjunto_s(1_000_000, novo, velho) == 3600


def test_reconciliacao_sem_bloco_e_recusada():
    """Sem bloco nao da para saber a ordem; supor que da e o bug original."""
    with pytest.raises(InstantesIncompativeis) as e:
        exigir_mesmo_ponto_da_chain(Instante(ts=1, rotulo="sem bloco"),
                                    Instante(ts=1, bloco=10, rotulo="com bloco"))
    assert "exige bloco" in str(e.value)


def test_mesmo_ponto_exato_passa():
    a = Instante(ts=1, bloco=10, log_index=3, rotulo="a")
    b = Instante(ts=1, bloco=10, log_index=3, rotulo="b")
    exigir_mesmo_ponto_da_chain(a, b)
