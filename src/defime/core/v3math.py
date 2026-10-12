"""Matematica Uniswap V3 — funcoes puras, inteiros em toda parte.

Regra do documento de regras do projeto: `float` so na apresentacao. Aqui e int e Decimal de alta
precisao. Erro de arredondamento nesta camada contamina absolutamente tudo.

Duas implementacoes independentes de tick->sqrtPrice:
  - tick_to_sqrt_price_x96()      TickMath exato do Uniswap (cadeia de constantes)
  - tick_to_sqrt_price_ref()      referencia em Decimal de 60 digitos
Elas se validam mutuamente nos testes. Uma constante errada na cadeia seria
invisivel sozinha, mas nao sobrevive ao cruzamento com a referencia nem com o
sqrtPriceX96 real lido da pool.
"""

from __future__ import annotations

from decimal import Decimal, getcontext

getcontext().prec = 80

MIN_TICK = -887272
MAX_TICK = 887272
Q96 = 1 << 96
Q128 = 1 << 128
Q256_MAX = (1 << 256) - 1

MIN_SQRT_RATIO = 4295128739
MAX_SQRT_RATIO = 1461446703485210103287273052203988822378723970342

# TickMath.getSqrtRatioAtTick — um fator por bit de |tick|.
_TICK_CONSTANTS = [
    (0x1, 0xFFFCB933BD6FAD37AA2D162D1A594001),
    (0x2, 0xFFF97272373D413259A46990580E213A),
    (0x4, 0xFFF2E50F5F656932EF12357CF3C7FDCC),
    (0x8, 0xFFE5CACA7E10E4E61C3624EAA0941CD0),
    (0x10, 0xFFCB9843D60F6159C9DB58835C926644),
    (0x20, 0xFF973B41FA98C081472E6896DFB254C0),
    (0x40, 0xFF2EA16466C96A3843EC78B326B52861),
    (0x80, 0xFE5DEE046A99A2A811C461F1969C3053),
    (0x100, 0xFCBE86C7900A88AEDCFFC83B479AA3A4),
    (0x200, 0xF987A7253AC413176F2B074CF7815E54),
    (0x400, 0xF3392B0822B70005940C7A398E4B70F3),
    (0x800, 0xE7159475A2C29B7443B29C7FA6E889D9),
    (0x1000, 0xD097F3BDFD2022B8845AD8F792AA5825),
    (0x2000, 0xA9F746462D870FDF8A65DC1F90E061E5),
    (0x4000, 0x70D869A156D2A1B890BB3DF62BAF32F7),
    (0x8000, 0x31BE135F97D08FD981231505542FCFA6),
    (0x10000, 0x9AA508B5B7A84E1C677DE54F3E99BC9),
    (0x20000, 0x5D6AF8DEDB81196699C329225EE604),
    (0x40000, 0x2216E584F5FA1EA926041BEDFE98),
    (0x80000, 0x48A170391F7DC42444E8FA2),
]


def tick_to_sqrt_price_x96(tick: int) -> int:
    """TickMath.getSqrtRatioAtTick. Inteiro exato, igual ao contrato."""
    if not MIN_TICK <= tick <= MAX_TICK:
        raise ValueError(f"tick fora do dominio: {tick}")
    abs_tick = abs(tick)

    ratio = (0xFFFCB933BD6FAD37AA2D162D1A594001 if abs_tick & 0x1
             else 0x100000000000000000000000000000000)
    for bit, factor in _TICK_CONSTANTS[1:]:
        if abs_tick & bit:
            ratio = (ratio * factor) >> 128

    if tick > 0:
        ratio = Q256_MAX // ratio

    # arredonda para cima ao truncar de Q128.128 para Q64.96
    return (ratio >> 32) + (1 if ratio % (1 << 32) else 0)


def tick_to_sqrt_price_ref(tick: int) -> int:
    """Referencia independente: sqrt(1.0001^tick) * 2^96 em Decimal."""
    ratio = Decimal("1.0001") ** Decimal(tick)
    return int(ratio.sqrt() * Decimal(Q96))


def sqrt_price_x96_to_tick(sqrt_price_x96: int) -> int:
    """Maior tick cujo sqrtRatio <= sqrt_price_x96 (mesma semantica do contrato).

    Busca binaria sobre a funcao exata: monotona por construcao, entao o
    resultado e exato, nao aproximado.
    """
    if not MIN_SQRT_RATIO <= sqrt_price_x96 < MAX_SQRT_RATIO:
        raise ValueError(f"sqrtPriceX96 fora do dominio: {sqrt_price_x96}")
    lo, hi = MIN_TICK, MAX_TICK
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if tick_to_sqrt_price_x96(mid) <= sqrt_price_x96:
            lo = mid
        else:
            hi = mid - 1
    return lo


# ---- preco ----------------------------------------------------------------

def sqrt_price_x96_to_price(sqrt_price_x96: int, dec0: int, dec1: int) -> Decimal:
    """Preco humano de token1 por token0.

    O ajuste de decimais entra AQUI, antes de qualquer inversao. Ver C5 do
    plano do projeto: nesta pool dec0=6 e dec1=18, fator 10^-12. Inverter antes de
    ajustar da erro silencioso.
    """
    raw = (Decimal(sqrt_price_x96) / Decimal(Q96)) ** 2
    return raw * (Decimal(10) ** (dec0 - dec1))


def price_to_usd(price_t1_per_t0: Decimal) -> Decimal:
    """Para USDG/USO: o preco nativo e USO por USDG; o util em USD e o inverso."""
    if price_t1_per_t0 == 0:
        raise ZeroDivisionError("preco zero")
    return Decimal(1) / price_t1_per_t0


def tick_to_price(tick: int, dec0: int, dec1: int) -> Decimal:
    return sqrt_price_x96_to_price(tick_to_sqrt_price_x96(tick), dec0, dec1)


def tick_to_usd(tick: int, dec0: int, dec1: int) -> Decimal:
    return price_to_usd(tick_to_price(tick, dec0, dec1))


def align_to_spacing(tick: int, spacing: int, mode: str = "nearest") -> int:
    """Alinha um tick ao tickSpacing. mode: nearest | down | up."""
    if spacing <= 0:
        raise ValueError("spacing precisa ser positivo")
    down = (tick // spacing) * spacing
    if mode == "down":
        return down
    up = down + spacing if tick % spacing else down
    if mode == "up":
        return up
    return down if (tick - down) <= (up - tick) else up


# ---- amounts <-> liquidity ------------------------------------------------

def _ordered(a: int, b: int) -> tuple[int, int]:
    return (a, b) if a <= b else (b, a)


def get_amount0_delta(sqrt_a: int, sqrt_b: int, liquidity: int,
                      round_up: bool = False) -> int:
    """amount0 = L * (sqrtB - sqrtA) / (sqrtA * sqrtB), em Q96."""
    sqrt_a, sqrt_b = _ordered(sqrt_a, sqrt_b)
    if sqrt_a == 0:
        raise ValueError("sqrtA zero")
    numerator1 = liquidity << 96
    numerator2 = sqrt_b - sqrt_a
    if round_up:
        inner = -(-(numerator1 * numerator2) // sqrt_b)   # ceil
        return -(-inner // sqrt_a)
    return (numerator1 * numerator2 // sqrt_b) // sqrt_a


def get_amount1_delta(sqrt_a: int, sqrt_b: int, liquidity: int,
                      round_up: bool = False) -> int:
    """amount1 = L * (sqrtB - sqrtA), em Q96."""
    sqrt_a, sqrt_b = _ordered(sqrt_a, sqrt_b)
    prod = liquidity * (sqrt_b - sqrt_a)
    return -(-prod // Q96) if round_up else prod // Q96


def amounts_for_liquidity(sqrt_price: int, sqrt_lower: int, sqrt_upper: int,
                          liquidity: int) -> tuple[int, int]:
    """(amount0, amount1) de uma posicao, dado o preco atual.

    Tres regimes: abaixo da faixa (so token0), dentro (os dois), acima (so token1).
    """
    sqrt_lower, sqrt_upper = _ordered(sqrt_lower, sqrt_upper)
    if sqrt_price <= sqrt_lower:
        return get_amount0_delta(sqrt_lower, sqrt_upper, liquidity), 0
    if sqrt_price < sqrt_upper:
        return (get_amount0_delta(sqrt_price, sqrt_upper, liquidity),
                get_amount1_delta(sqrt_lower, sqrt_price, liquidity))
    return 0, get_amount1_delta(sqrt_lower, sqrt_upper, liquidity)


def liquidity_for_amount0(sqrt_a: int, sqrt_b: int, amount0: int) -> int:
    sqrt_a, sqrt_b = _ordered(sqrt_a, sqrt_b)
    intermediate = (sqrt_a * sqrt_b) // Q96
    return (amount0 * intermediate) // (sqrt_b - sqrt_a)


def liquidity_for_amount1(sqrt_a: int, sqrt_b: int, amount1: int) -> int:
    sqrt_a, sqrt_b = _ordered(sqrt_a, sqrt_b)
    return (amount1 * Q96) // (sqrt_b - sqrt_a)


def liquidity_for_amounts(sqrt_price: int, sqrt_lower: int, sqrt_upper: int,
                          amount0: int, amount1: int) -> int:
    """Maior L que cabe nos dois amounts — o menor dos dois limites."""
    sqrt_lower, sqrt_upper = _ordered(sqrt_lower, sqrt_upper)
    if sqrt_price <= sqrt_lower:
        return liquidity_for_amount0(sqrt_lower, sqrt_upper, amount0)
    if sqrt_price < sqrt_upper:
        return min(liquidity_for_amount0(sqrt_price, sqrt_upper, amount0),
                   liquidity_for_amount1(sqrt_lower, sqrt_price, amount1))
    return liquidity_for_amount1(sqrt_lower, sqrt_upper, amount1)


# ---- fees -----------------------------------------------------------------

def fees_from_growth(growth_inside_now: int, growth_inside_last: int,
                     liquidity: int) -> int:
    """Fees devidas desde o ultimo checkpoint.

    feeGrowth e um acumulador Q128.128 que faz wraparound de proposito no
    contrato — a subtracao precisa ser modulo 2^256, senao da negativo enorme
    quando o acumulador da a volta.
    """
    delta = (growth_inside_now - growth_inside_last) & ((1 << 256) - 1)
    return (delta * liquidity) >> 128


def fee_growth_inside(fee_growth_global: int, lower_outside: int,
                      upper_outside: int, tick_current: int,
                      tick_lower: int, tick_upper: int) -> int:
    """feeGrowthInside de uma faixa, com aritmetica modulo 2^256."""
    mask = (1 << 256) - 1
    below = lower_outside if tick_current >= tick_lower else (fee_growth_global - lower_outside) & mask
    above = upper_outside if tick_current < tick_upper else (fee_growth_global - upper_outside) & mask
    return (fee_growth_global - below - above) & mask


__all__ = [
    "MIN_TICK", "MAX_TICK", "Q96", "Q128",
    "tick_to_sqrt_price_x96", "tick_to_sqrt_price_ref", "sqrt_price_x96_to_tick",
    "sqrt_price_x96_to_price", "price_to_usd", "tick_to_price", "tick_to_usd",
    "align_to_spacing", "amounts_for_liquidity", "liquidity_for_amounts",
    "get_amount0_delta", "get_amount1_delta",
    "liquidity_for_amount0", "liquidity_for_amount1",
    "fees_from_growth", "fee_growth_inside",
]
