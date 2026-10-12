"""Matematica V3. Sem rede. Inclui testes de propriedade."""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given, settings as hyp_settings
from hypothesis import strategies as st

from defime.core.v3math import (MAX_TICK, MIN_TICK, Q96, align_to_spacing,
                                amounts_for_liquidity, fees_from_growth,
                                liquidity_for_amounts, price_to_usd,
                                sqrt_price_x96_to_price, sqrt_price_x96_to_tick,
                                tick_to_sqrt_price_ref, tick_to_sqrt_price_x96,
                                tick_to_usd)

# Valores reais lidos da pool USO/USDG na Fase 0/1. Sao o vetor de verdade.
POOL_SQRT = 7057716242524677591777035769655174
POOL_TICK = 227957
DEC0, DEC1 = 6, 18                      # USDG, USO
NFT_TICK_LOWER, NFT_TICK_UPPER = 227700, 228960   # posição de referência POS_B


def test_tickmath_bate_com_referencia_decimal():
    """Cadeia de constantes vs sqrt(1.0001^tick) em Decimal.

    Uma constante errada em _TICK_CONSTANTS seria invisivel sozinha. Aqui ela
    aparece na hora.
    """
    for tick in (-500000, -227940, -60, 0, 60, 227700, 227957, 228960, 500000):
        exato = tick_to_sqrt_price_x96(tick)
        ref = tick_to_sqrt_price_ref(tick)
        assert abs(exato - ref) / ref < 1e-15, f"divergencia no tick {tick}"


def test_sqrt_price_real_da_pool_cai_no_tick_certo():
    assert (tick_to_sqrt_price_x96(POOL_TICK) <= POOL_SQRT
            < tick_to_sqrt_price_x96(POOL_TICK + 1))
    assert sqrt_price_x96_to_tick(POOL_SQRT) == POOL_TICK


def test_preco_em_usd_bate_com_a_revert():
    preco = price_to_usd(sqrt_price_x96_to_price(POOL_SQRT, DEC0, DEC1))
    assert abs(preco - Decimal("126.017")) < Decimal("0.01")


def test_range_da_nft_bate_com_a_revert():
    """113,996 .. 129,303 conforme a UI e a API da Revert."""
    baixo = tick_to_usd(NFT_TICK_UPPER, DEC0, DEC1)
    alto = tick_to_usd(NFT_TICK_LOWER, DEC0, DEC1)
    assert abs(baixo - Decimal("113.996")) < Decimal("0.001")
    assert abs(alto - Decimal("129.303")) < Decimal("0.001")


def test_decimais_diferentes_importam():
    """C5 do plano do projeto: dec0=6, dec1=18 -> fator 10^-12.

    Ignorar o ajuste erra por 12 ordens de grandeza.
    """
    certo = sqrt_price_x96_to_price(POOL_SQRT, 6, 18)
    ignorando = sqrt_price_x96_to_price(POOL_SQRT, 18, 18)
    assert ignorando / certo == Decimal(10) ** 12


@given(st.integers(min_value=MIN_TICK + 1, max_value=MAX_TICK - 1))
@hyp_settings(max_examples=200, deadline=None)
def test_tick_ida_e_volta(tick):
    """sqrt_price_x96_to_tick(tick_to_sqrt_price_x96(t)) == t"""
    assert sqrt_price_x96_to_tick(tick_to_sqrt_price_x96(tick)) == tick


@given(st.integers(min_value=-200000, max_value=200000),
       st.integers(min_value=1, max_value=80),          # em multiplos de spacing
       st.integers(min_value=10 ** 15, max_value=10 ** 22))
@hyp_settings(max_examples=300, deadline=None)
def test_amounts_liquidity_ida_e_volta(tick_base, passos, liquidez):
    """L -> (amount0, amount1) -> L reproduz a liquidez.

    Criterio 3 do check-2.

    Nota de honestidade — este teste falhou duas vezes e as duas revelaram que
    a AFIRMACAO estava errada, nao o codigo:

      1a: sorteava largura a partir de 1 tick. Falhou (6,2e-9). Esta pool tem
          tickSpacing=60, entao largura de 1 tick nao existe no dominio real.
      2a: sorteava L a partir de 1e12. Falhou (1,96e-9). A perda por truncamento
          e ~Q96/(sqrtB-sqrtA) em valor ABSOLUTO e independe de L — logo o erro
          relativo cresce quando L encolhe. Um limite relativo so faz sentido
          com L realista.

      3a: subir o piso de L nao resolveu (falhou de novo com L=1e15, 1,6e-9).
          Estava perseguindo o numero magico em vez de derivar o limite.

    O limite CORRETO e absoluto e vem do algoritmo. Cada unidade truncada de
    amount custa, na volta:
        via token0:  (sqrtP*sqrtU/Q96) / (sqrtU - sqrtP)  de L
        via token1:  Q96 / (sqrtP - sqrtL)                de L
    E `liquidity_for_amounts` pega o MENOR dos dois caminhos, entao a perda e
    limitada pelo pior deles. E isso que se afirma aqui — nao um epsilon
    escolhido a dedo.
    """
    spacing = 60
    tl = align_to_spacing(tick_base, spacing, "down")
    tu = tl + passos * spacing
    tick_atual = tl + (passos * spacing) // 2
    sqrt_l = tick_to_sqrt_price_x96(tl)
    sqrt_u = tick_to_sqrt_price_x96(tu)
    sqrt_p = tick_to_sqrt_price_x96(tick_atual)

    a0, a1 = amounts_for_liquidity(sqrt_p, sqrt_l, sqrt_u, liquidez)
    de_volta = liquidity_for_amounts(sqrt_p, sqrt_l, sqrt_u, a0, a1)

    custo_por_unidade_t0 = ((sqrt_p * sqrt_u) // Q96) // max(1, sqrt_u - sqrt_p)
    custo_por_unidade_t1 = Q96 // max(1, sqrt_p - sqrt_l)
    limite = max(custo_por_unidade_t0, custo_por_unidade_t1) + 2

    assert de_volta <= liquidez, "reconstrucao nunca pode inflar a liquidez"
    assert liquidez - de_volta <= limite, (
        f"perda {liquidez - de_volta} acima do limite teorico {limite}")


@given(st.integers(min_value=-100000, max_value=100000),
       st.integers(min_value=1, max_value=59),
       st.integers(min_value=10 ** 9, max_value=10 ** 24))
@hyp_settings(max_examples=200, deadline=None)
def test_perda_por_truncamento_em_faixa_estreita(tick_base, largura, liquidez):
    """Fora do dominio da pool, a garantia que resta e a de seguranca.

    Nao ha limite relativo util em faixa de 1 tick, mas a propriedade que
    protege a pool continua valendo: a reconstrucao NUNCA infla a liquidez.
    """
    tl, tu = tick_base, tick_base + largura
    sqrt_l = tick_to_sqrt_price_x96(tl)
    sqrt_u = tick_to_sqrt_price_x96(tu)
    sqrt_p = tick_to_sqrt_price_x96(tl + largura // 2)

    a0, a1 = amounts_for_liquidity(sqrt_p, sqrt_l, sqrt_u, liquidez)
    de_volta = liquidity_for_amounts(sqrt_p, sqrt_l, sqrt_u, a0, a1)
    assert de_volta <= liquidez


def test_amounts_nos_tres_regimes():
    sqrt_l = tick_to_sqrt_price_x96(NFT_TICK_LOWER)
    sqrt_u = tick_to_sqrt_price_x96(NFT_TICK_UPPER)
    L = 18805629406356259

    abaixo = amounts_for_liquidity(tick_to_sqrt_price_x96(NFT_TICK_LOWER - 1000),
                                   sqrt_l, sqrt_u, L)
    dentro = amounts_for_liquidity(POOL_SQRT, sqrt_l, sqrt_u, L)
    acima = amounts_for_liquidity(tick_to_sqrt_price_x96(NFT_TICK_UPPER + 1000),
                                  sqrt_l, sqrt_u, L)

    assert abaixo[1] == 0 and abaixo[0] > 0      # so token0
    assert dentro[0] > 0 and dentro[1] > 0       # os dois
    assert acima[0] == 0 and acima[1] > 0        # so token1


def test_amounts_da_nft_batem_com_a_revert():
    """Reconciliacao offline contra o golden file capturado da Revert."""
    L = 18805629406356259
    a0, a1 = amounts_for_liquidity(POOL_SQRT,
                                   tick_to_sqrt_price_x96(NFT_TICK_LOWER),
                                   tick_to_sqrt_price_x96(NFT_TICK_UPPER), L)
    usdg = Decimal(a0) / Decimal(10 ** DEC0)
    uso = Decimal(a1) / Decimal(10 ** DEC1)
    assert abs(usdg - Decimal("10321.403")) < Decimal("0.001")
    assert abs(uso - Decimal("21.42108671")) < Decimal("0.0000001")


def test_align_to_spacing():
    assert align_to_spacing(227955, 60, "down") == 227940
    assert align_to_spacing(227955, 60, "up") == 228000
    assert align_to_spacing(227955, 60, "nearest") == 227940
    assert align_to_spacing(227970, 60, "nearest") == 227940
    assert align_to_spacing(-227955, 60, "down") == -228000
    assert align_to_spacing(228000, 60, "up") == 228000    # ja alinhado


def test_fees_com_wraparound_do_acumulador():
    """feeGrowth faz a volta em 2^256 de proposito. Sem modulo, da negativo."""
    mask = (1 << 256) - 1
    anterior = mask - 100
    agora = 50                       # deu a volta
    devido = fees_from_growth(agora, anterior, liquidity=1 << 128)
    assert devido > 0
    assert devido == ((agora - anterior) & mask) * (1 << 128) >> 128


def test_tick_fora_do_dominio_levanta():
    with pytest.raises(ValueError):
        tick_to_sqrt_price_x96(MAX_TICK + 1)
    with pytest.raises(ValueError):
        tick_to_sqrt_price_x96(MIN_TICK - 1)
