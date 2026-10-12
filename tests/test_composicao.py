"""Compositor de faixa — `core/composicao.py`.

**O portao C1 do PLAN_COMPOSICAO nao e sintetico.** A ancora e a posicao viva
POS_A (UPS/USDG, V4), com L e ticks lidos do contrato em 2026-09-06 e
congelados aqui como fixture. Se a composicao calculada nao reproduzir os
amounts que a chain reporta, o codigo esta errado — nao o golden.

Nenhum teste toca a rede.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))
sys.path.insert(0, str(RAIZ / "src"))

from defime.core import composicao as C        # noqa: E402
from defime.core.v3math import (amounts_for_liquidity,        # noqa: E402
                                tick_to_sqrt_price_x96)

# --------------------------------------------------------------- fixtures
# Lidos da chain em 2026-09-06 13:4x UTC, pelo RPC primario:
#   V4.estado(rpc, pool)            -> sqrtPriceX96, tick, liquidez ativa
#   getPositionLiquidity(POS_A)   -> L
# Os ticks 229920..230010 sao os que o documento de regras do projeto ja registra para esta
# posicao, e o preco de $102,6342-$103,5620 que eles produzem e o que a tela
# da Uniswap mostra.
UPS = C.Mercado(usdg_token0=True, dec_stable=6, dec_ativo=18,
                tick_spacing=30, fee_pips=3000, taxa_swap=0.003499)
UPS_SQ = 7807532532238229535809884384106176
UPS_TICK = 229976
# **O preco EXATO implicado pelo sqrt real**, nao `preco_do_tick(229976)`.
# A primeira versao deste fixture gravou o preco do TICK ao lado dos amounts
# do SQRT, e o portao C1 reprovou com 2,6% de erro — duas resolucoes da mesma
# grandeza, a familia "dois instantes" com outra roupa.
UPS_PRECO = 102.974943527452
UPS_TL, UPS_TU = 229920, 230010
UPS_L = 331586956728651626
# amounts que esse L produz naquele sqrtPriceX96, pela mesma `v3math` que
# reconcilia com o contrato ao wei
UPS_A0_RAW, UPS_A1_RAW = 5571919952, 92749381509296182582
# **O valor EXATO desses amounts ao preco EXATO.** A primeira versao usou
# 15123.60, calculado com o preco do TICK — terceira camada do mesmo erro de
# resolucao. Com este, a composicao reproduz os amounts com erro relativo ZERO.
UPS_VALOR = 15122.782275


# ------------------------------------------------------ preco e orientacao

def test_preco_do_tick_bate_com_a_faixa_que_a_uniswap_mostra():
    """**A orientacao e FATO, nao escolha.** Com a stable em token0 os ticks
    invertem: o tick MAIOR e o preco MENOR. Supor o contrario ja fez GME e
    SLV valerem zero neste projeto.

    Os ticks 229920..230010 da POS_A tem de dar $102,6342-$103,5620, que e
    o que o documento de regras do projeto registra e a tela da Uniswap publica."""
    a = C.preco_do_tick(UPS, UPS_TL)
    b = C.preco_do_tick(UPS, UPS_TU)
    assert max(a, b) == pytest.approx(103.5620, abs=0.0001)
    assert min(a, b) == pytest.approx(102.6342, abs=0.0001)
    # o tick maior e o preco menor — a inversao, explicita
    assert b < a


def test_tick_do_preco_e_o_inverso_exato():
    for tick in (229920, 229976, 230010, -6931, 0, 12345):
        p = C.preco_do_tick(UPS, tick)
        assert C.tick_do_preco(UPS, p) == tick


def test_orientacao_INVERTIDA_muda_o_preco_de_cabeca_para_baixo():
    """Em 11 das 18 pools desta chain supor a stable em token0 inverte o
    preco. O teste existe para que a suposicao nunca volte silenciosa."""
    outra = C.Mercado(usdg_token0=False, dec_stable=6, dec_ativo=18,
                      tick_spacing=30, fee_pips=3000)
    assert C.preco_do_tick(UPS, 229976) != C.preco_do_tick(outra, 229976)
    assert C.preco_do_tick(UPS, 229976) == pytest.approx(
        1 / C.preco_do_tick(outra, 229976) * (10 ** 24), rel=1e-9)


# ------------------------------------------- PORTAO C1: a posicao real

def test_PORTAO_C1_reproduz_os_amounts_da_posicao_viva_1831178():
    """A ancora do plano: mesma faixa, mesmo capital, mesmos amounts.

    Erro exigido < 0,01% contra o que a chain reporta."""
    faixa = C.Faixa(tick_lower=UPS_TL, tick_upper=UPS_TU,
                    piso=C.preco_do_tick(UPS, UPS_TU),
                    teto=C.preco_do_tick(UPS, UPS_TL),
                    piso_pedido=102.6342, teto_pedido=103.5620)
    c = C.compor(UPS, faixa, UPS_PRECO, capital=UPS_VALOR)

    esperado_usdg = UPS_A0_RAW / 10 ** 6
    esperado_ups = UPS_A1_RAW / 10 ** 18
    assert c.qtd_stable == pytest.approx(esperado_usdg, rel=1e-4)
    assert c.qtd_ativo == pytest.approx(esperado_ups, rel=1e-4)
    # e o L reconstruido bate com o do contrato
    assert c.liquidez == pytest.approx(UPS_L, rel=1e-4)


def test_PORTAO_C1_a_composicao_bate_com_o_que_o_painel_publica():
    """36,84% stable / 63,16% ativo, medido na mesma posicao."""
    faixa = C.faixa_executavel(UPS, 102.6342, 103.5620)
    c = C.compor(UPS, faixa, UPS_PRECO, capital=UPS_VALOR)
    assert 100 * c.share_stable == pytest.approx(36.84, abs=0.1)
    assert 100 * c.share_ativo == pytest.approx(63.16, abs=0.1)
    assert c.total == pytest.approx(UPS_VALOR, rel=1e-6)


def test_sem_capital_as_PROPORCOES_ja_estao_certas():
    """Quem ainda nao decidiu o tamanho pergunta a proporcao. Ela nao depende
    do capital — so as quantidades dependem."""
    faixa = C.faixa_executavel(UPS, 102.6342, 103.5620)
    sem = C.compor(UPS, faixa, UPS_PRECO)
    com = C.compor(UPS, faixa, UPS_PRECO, capital=UPS_VALOR)
    assert sem.share_stable == pytest.approx(com.share_stable, rel=1e-6)


# ------------------------------------------- PORTAO C2: faixa executavel

def test_PORTAO_C2_o_tick_e_multiplo_do_spacing_DAQUELA_pool():
    """A UPS tem spacing 30, nao 60 — e e isso que faz "3x spacing" valer ali
    metade do que vale nas outras pools da chain. Ler o spacing de um default
    ja pos a RDDT em primeiro lugar com uma faixa que nao existe nela."""
    for lo, hi in ((100.0, 105.0), (102.61, 103.57), (90.0, 120.0)):
        f = C.faixa_executavel(UPS, lo, hi)
        assert f.tick_lower % UPS.tick_spacing == 0
        assert f.tick_upper % UPS.tick_spacing == 0


def test_PORTAO_C2_a_faixa_executavel_CONTEM_a_pedida():
    """Alinhar para o tick mais proximo poderia ESTREITAR a faixa sem avisar,
    e estreitar e a unica das duas direcoes que muda o risco calada. O
    alinhamento e para fora nos dois lados."""
    f = C.faixa_executavel(UPS, 102.70, 103.40)
    assert f.piso <= 102.70
    assert f.teto >= 103.40


def test_PORTAO_C2_publica_a_pedida_ao_lado_da_executavel():
    f = C.faixa_executavel(UPS, 102.70, 103.40)
    assert f.piso_pedido == 102.70 and f.teto_pedido == 103.40
    assert f.piso != f.piso_pedido or f.teto != f.teto_pedido
    assert abs(f.desvio_piso_pct) < 1.0


def test_faixa_invertida_levanta():
    with pytest.raises(ValueError):
        C.faixa_executavel(UPS, 105.0, 100.0)


# --------------------------------------------- os tres regimes de preco

def test_abaixo_da_faixa_a_posicao_e_100_por_cento_de_UM_ativo():
    """Fora do range a fee para e a posicao fica inteira num lado. Com a
    stable em token0 e o preco ABAIXO do piso, sobra o ATIVO."""
    f = C.faixa_executavel(UPS, 102.6342, 103.5620)
    c = C.compor(UPS, f, 101.0, capital=1000.0)
    assert c.estado == "abaixo"
    assert c.share_ativo == pytest.approx(1.0, abs=1e-6)
    assert c.qtd_stable == pytest.approx(0.0, abs=1e-9)
    assert c.nivel_pct < 0            # sai do intervalo, nao e clipado


def test_acima_da_faixa_a_posicao_vira_a_STABLE():
    f = C.faixa_executavel(UPS, 102.6342, 103.5620)
    c = C.compor(UPS, f, 105.0, capital=1000.0)
    assert c.estado == "acima"
    assert c.share_stable == pytest.approx(1.0, abs=1e-6)
    assert c.nivel_pct > 100


def test_nivel_nao_e_clipado_nas_pontas():
    """Prender em 0% e 100% empilharia no mesmo valor o que a coluna existe
    para separar — a mesma decisao da coluna `nivel` da pagina da pool."""
    f = C.faixa_executavel(UPS, 102.6342, 103.5620)
    assert C.compor(UPS, f, 100.0).nivel_pct < 0
    assert C.compor(UPS, f, 110.0).nivel_pct > 100


def test_no_meio_da_faixa_a_composicao_e_equilibrada():
    f = C.faixa_executavel(UPS, 100.0, 106.0)
    meio = (f.piso + f.teto) / 2
    c = C.compor(UPS, f, meio, capital=1000.0)
    assert 0.4 < c.share_stable < 0.6


# ------------------------------------- PORTAO C3: o custo do swap medido

def test_PORTAO_C3_o_custo_do_swap_e_dominado_pela_taxa_de_INTERFACE():
    """Medido em 04/09: dos $243,17 de execucao, **$204,18 (84%)** foram o
    skim de 1,6% para `0x147cf09e...`. A taxa da pool e o resto.

    Uma calculadora que cobrasse so a taxa nominal erraria por fator ~5,8 —
    que e exatamente o fator pelo qual a estimativa antiga deste projeto
    subestimou a migracao de 03/09 ($62,84 estimados, $363,85 reais)."""
    f = C.faixa_executavel(UPS, 102.6342, 103.5620)
    c = C.compor(UPS, f, UPS_PRECO, capital=15000.0)
    s = C.swap_para_abrir(UPS, c, UPS_PRECO)
    assert s.taxa_interface_usd > s.taxa_pool_usd * 4
    fatia = s.taxa_interface_usd / s.custo_usd
    assert 0.80 < fatia < 0.86


def test_PORTAO_C3_reproduz_a_ordem_de_grandeza_da_migracao_de_0409():
    """Abrir a faixa partindo de 100% stable com $15.000 exige comprar ~63%
    em ativo. A 1,95% por perna isso da ~$185 — a mesma ordem dos ~$148
    registrados para recentrar, e nada perto dos $62,84 da conta antiga."""
    f = C.faixa_executavel(UPS, 102.6342, 103.5620)
    c = C.compor(UPS, f, UPS_PRECO, capital=15000.0)
    s = C.swap_para_abrir(UPS, c, UPS_PRECO)
    assert 150.0 < s.custo_usd < 220.0
    assert s.custo_pct == pytest.approx(100 * s.custo_usd / 15000.0, rel=1e-9)


def test_quem_ja_TEM_o_ativo_nao_paga_swap_nenhum():
    """O custo e do que FALTA, nao do capital. Chegar com a composicao pronta
    custa zero — e e por isso que o swap entra como parcela separada."""
    f = C.faixa_executavel(UPS, 102.6342, 103.5620)
    c = C.compor(UPS, f, UPS_PRECO, capital=15000.0)
    s = C.swap_para_abrir(UPS, c, UPS_PRECO,
                          tem_stable=c.qtd_stable, tem_ativo=c.qtd_ativo)
    assert s.custo_usd == pytest.approx(0.0, abs=1e-9)
    assert s.sentido == "nenhum"


def test_a_taxa_do_swap_pode_ser_MAIOR_que_a_declarada():
    """Na V4 o campo `fee` do evento e a taxa TOTAL: a UPS declara 3000 e o
    swap paga 3499, porque o protocolo cobra sobre a entrada. Registrar a
    declarada como se fosse a paga subestima o custo."""
    declarada = C.Mercado(usdg_token0=True, dec_stable=6, dec_ativo=18,
                          tick_spacing=30, fee_pips=3000)
    assert declarada.taxa_swap_frac == pytest.approx(0.003)
    assert UPS.taxa_swap_frac == pytest.approx(0.003499)
    assert UPS.taxa_swap_frac > declarada.taxa_swap_frac


# ------------------------------------------------ decimais e outras pools

def test_pools_com_decimais_diferentes():
    """6, 8 e 18 casas. O erro de decimal nao levanta excecao — ele publica
    `fatia 0,000%`, que foi o que a projecao de entrada fez em 05/09 nas
    cinco pools antes de a inversao passar pela mesma funcao do livro."""
    for dec_ativo in (6, 8, 18):
        m = C.Mercado(usdg_token0=True, dec_stable=6, dec_ativo=dec_ativo,
                      tick_spacing=60, fee_pips=3000)
        p = C.preco_do_tick(m, 0)
        f = C.faixa_executavel(m, p * 0.99, p * 1.01)
        c = C.compor(m, f, p, capital=1000.0)
        assert c.total == pytest.approx(1000.0, rel=1e-6)
        assert 0.0 < c.share_ativo < 1.0


def test_capital_invalido_levanta_em_vez_de_devolver_zero():
    f = C.faixa_executavel(UPS, 102.6342, 103.5620)
    with pytest.raises(ValueError):
        C.compor(UPS, f, UPS_PRECO, capital=0.0)


def test_a_composicao_usa_a_MESMA_v3math_do_painel():
    """Nao ha segunda implementacao da conta: o resultado tem de bater com
    `amounts_for_liquidity` chamada direto. Duas versoes da mesma aritmetica
    e a familia da `_horas_fora` duplicada — uma testada, a de producao
    nao."""
    f = C.faixa_executavel(UPS, 102.6342, 103.5620)
    c = C.compor(UPS, f, UPS_PRECO, capital=UPS_VALOR)
    a0, a1 = amounts_for_liquidity(
        C._sqrt_do_preco(UPS, UPS_PRECO),
        tick_to_sqrt_price_x96(f.tick_lower),
        tick_to_sqrt_price_x96(f.tick_upper), c.liquidez)
    assert c.qtd_stable == pytest.approx(a0 / 10 ** 6, rel=1e-9)
    assert c.qtd_ativo == pytest.approx(a1 / 10 ** 18, rel=1e-9)


# --------------------------------- a RESOLUCAO do preco (achado de 06/09)

def test_o_sqrt_da_chain_e_o_preco_arredondado_ao_tick_DIVERGEM():
    """**Duas resolucoes da mesma grandeza, e a estreita amplifica.**

    O preco da pool e continuo; o tick e discreto. Na POS_A o
    `sqrtPriceX96` real estava a ~1/3 de tick da borda, e passar o preco do
    TICK no lugar dele deu 37,79% stable contra os 36,84% verdadeiros — 2,6%
    de erro na quantidade de USDG, numa faixa de 0,9%.

    Foi assim que o portao C1 reprovou na primeira execucao. O teste existe
    para que a conversao continua nao volte a virar `preco -> tick -> sqrt`
    sem alguem perceber."""
    f = C.Faixa(UPS_TL, UPS_TU, C.preco_do_tick(UPS, UPS_TU),
                C.preco_do_tick(UPS, UPS_TL), 102.6342, 103.5620)
    exato = C.compor(UPS, f, capital=UPS_VALOR, sqrt_price=UPS_SQ)
    pelo_tick = C.compor(UPS, f, C.preco_do_tick(UPS, UPS_TICK),
                         capital=UPS_VALOR)
    assert exato.qtd_stable == pytest.approx(UPS_A0_RAW / 10 ** 6, rel=1e-6)
    # o erro que o arredondamento introduz e material: ~1pp de composicao
    erro_pp = abs(100 * (pelo_tick.share_stable - exato.share_stable))
    assert 0.5 < erro_pp < 2.0


def test_preco_E_sqrt_juntos_sao_RECUSADOS():
    """Aceitar os dois permitiria os amounts saindo de um e a avaliacao em
    dolar do outro — o par inconsistente que reprovou o portao C1. Um OU o
    outro; com o sqrt, o preco e derivado dele."""
    f = C.faixa_executavel(UPS, 102.6342, 103.5620)
    with pytest.raises(ValueError):
        C.compor(UPS, f, UPS_PRECO, capital=100.0, sqrt_price=UPS_SQ)
    with pytest.raises(ValueError):
        C.compor(UPS, f, capital=100.0)


def test_com_o_sqrt_da_chain_o_preco_sai_DERIVADO():
    f = C.faixa_executavel(UPS, 102.6342, 103.5620)
    c = C.compor(UPS, f, capital=UPS_VALOR, sqrt_price=UPS_SQ)
    assert c.qtd_stable == pytest.approx(UPS_A0_RAW / 10 ** 6, rel=1e-6)
    assert C.preco_do_sqrt(UPS, UPS_SQ) == pytest.approx(UPS_PRECO, rel=1e-9)


def test_a_conversao_continua_reconstroi_o_sqrt_da_chain():
    """Ida e volta preco->sqrt sem passar pelo tick: erro < 1e-12."""
    exato = 10 ** 12 / (UPS_SQ / (1 << 96)) ** 2
    assert C._sqrt_do_preco(UPS, exato) == pytest.approx(UPS_SQ, rel=1e-12)
