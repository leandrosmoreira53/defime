"""A vigilancia mede a assimetria; o score que tentava PREVE-la reprovou.

Este arquivo trava duas coisas diferentes:

  - que o calculo descritivo continua certo (fatia, cenarios, distancia);
  - que o `score_oportunidade` continua marcado como reprovado, para nao
    voltar como sinal de entrada em alguma refatoracao futura.
"""

import pytest

from defime.core import vigilancia as VG


def _v(token, L, tl, tu, cap=1000.0, larg=None, em=True):
    return VG.Vizinha(token_id=token, liquidez=L, tick_lower=tl, tick_upper=tu,
                      capital_usd=cap,
                      largura_pct=(larg if larg is not None
                                   else 100 * (1.0001 ** (tu - tl) - 1)),
                      em_range=em)


def test_a_fatia_usa_so_quem_esta_no_tick():
    """Somar as posicoes FORA no denominador daria fatia menor — e o erro de
    5,6x que a Fase 5 pagou com `L_pool` no lugar de `L_pool + L_nossa`."""
    v = VG.montar([_v(1, 100, 0, 100), _v(2, 300, 0, 100),
                   _v(3, 9999, 500, 600, em=False)], tick=50, nosso_token=1)
    assert v.liquidez_no_tick == 400
    assert v.fatia_atual == pytest.approx(0.25)


def test_o_cenario_de_saida_multiplica_a_fatia():
    v = VG.montar([_v(1, 100, 0, 100, larg=20.0),
                   _v(2, 900, 40, 60, larg=0.2)], tick=50, nosso_token=1)
    assert v.fatia_atual == pytest.approx(0.1)
    est = next(c for c in v.cenarios if "estreitas" in c.rotulo)
    assert est.fatia_depois == pytest.approx(1.0)
    assert est.multiplicador == pytest.approx(10.0)


def test_concentracao_estreita_ignora_quem_esta_fora():
    v = VG.montar([_v(1, 100, 0, 100, larg=20.0),
                   _v(2, 900, 40, 60, larg=0.2),
                   _v(3, 5000, 200, 210, larg=0.1, em=False)], tick=50)
    assert v.concentracao_estreita == pytest.approx(0.9)


def test_distancia_da_borda_escolhe_a_borda_MAIS_PROXIMA():
    v = _v(1, 100, 0, 100)
    d = VG.distancia_da_borda(v, tick=90)
    assert d["lado"] == "cima" and d["ticks"] == 10
    d = VG.distancia_da_borda(v, tick=10)
    assert d["lado"] == "baixo" and d["ticks"] == 10


def test_o_score_continua_marcado_como_REPROVADO():
    """Se alguem tirar o aviso, provavelmente esta prestes a usa-lo para
    decidir — que e exatamente o que o backtest desautorizou."""
    doc = VG.score_oportunidade.__doc__ or ""
    assert "REPROVADO" in doc
    assert "NAO USAR PARA DECIDIR" in doc
    assert "-0,248" in doc and "+0,537" in doc, (
        "os numeros do backtest saem do docstring: sem eles o aviso vira "
        "opiniao e alguem refaz o teste do zero")


# --- a guarda de cobertura do indice --------------------------------------

def test_indice_incompleto_nao_publica_fatia():
    """Medido em 2026-09-04 na RBLX: o `npm_link` estava 47.807 blocos atrás,
    a soma das posições conhecidas dava 241e15 contra 8.886e15 de
    `liquidity()`, e a página publicou a fatia do usuário como **13,60%**
    quando era **0,37%** — 37× para MAIS, que é a direção que anima em vez de
    alarmar."""
    v = VG.montar([_v(1, 100, 0, 100), _v(2, 300, 0, 100)], tick=50,
                  nosso_token=1, liquidez_da_chain=40_000)
    assert v.fatia_atual is None
    assert v.cenarios == []
    assert "não medida" in v.motivo_sem_fatia.replace("nao", "não")
    assert v.cobertura == pytest.approx(400 / 40_000)


def test_indice_completo_publica_normalmente():
    v = VG.montar([_v(1, 100, 0, 100), _v(2, 300, 0, 100)], tick=50,
                  nosso_token=1, liquidez_da_chain=400)
    assert v.fatia_atual == pytest.approx(0.25)
    assert v.cobertura == pytest.approx(1.0)
    assert v.motivo_sem_fatia is None


def test_desvio_de_1pct_ainda_passa():
    """Fee não coletada e arredondamento produzem desvio pequeno e legítimo.
    A banda é 98%–102%; apertá-la faria o portão disparar sempre, que é como
    um portão morre."""
    v = VG.montar([_v(1, 100, 0, 100), _v(2, 297, 0, 100)], tick=50,
                  nosso_token=1, liquidez_da_chain=400)
    assert v.fatia_atual is not None


def test_indice_ADIANTADO_tambem_reprova():
    """Conhecer MAIS liquidez que a chain também é índice quebrado — Burn não
    processado. A guarda compara com a verdade, não com um atraso."""
    v = VG.montar([_v(1, 100, 0, 100), _v(2, 300, 0, 100)], tick=50,
                  nosso_token=1, liquidez_da_chain=200)
    assert v.fatia_atual is None


def test_sem_a_verdade_nao_ha_guarda():
    """Contexto sintético e teste não têm `liquidity()` para conferir. Nesse
    caso a fatia sai, e `cobertura` fica `None` dizendo que ninguém conferiu."""
    v = VG.montar([_v(1, 100, 0, 100), _v(2, 300, 0, 100)], tick=50,
                  nosso_token=1)
    assert v.fatia_atual == pytest.approx(0.25)
    assert v.cobertura is None


def test_o_resumo_leva_a_cobertura():
    v = VG.montar([_v(1, 100, 0, 100)], tick=50, nosso_token=1,
                  liquidez_da_chain=10_000)
    r = v.resumo()
    assert r["nossa_fatia"] is None
    assert r["cobertura_do_indice"] == pytest.approx(0.01)
    assert r["motivo_sem_fatia"]
