"""A fracao da taxa do swap que CHEGA ao LP numa pool Uniswap V3 — lida, nao suposta.

Puro: recebe o `feeProtocol` do `slot0` e o sentido do swap.

**O 5/6 era suposicao, e a medicao a derrubou em 24/09.** O projeto registrou
`feeProtocol = 102` lido em tres pools de 0,30% (USO, RDDT, COST, 31/08) e
passou a aplicar 5/6 a TODA V3. Lido nas 29 V3 do `pool_param` no bloco
71.028.140 (`tests/fixtures/fee_protocol_v3.json`):

    0,05%   3 pools (NVDA, SPCX, GOOGL)   feeProtocol 0x44   LP fica com 3/4
    0,30%  19 pools                       feeProtocol 0x66   LP fica com 5/6
    1%      7 pools                       feeProtocol 0x66   LP fica com 5/6

Nas de 0,05% o `pool_hora` publicava fee 11,1% ALTA (5/6 contra 3/4). A
mesma familia do 5/6 aplicado a V4 (corrigido no mesmo dia por
`pools_hora.taxa_lp_v4`): uma constante medida num subconjunto virou regra.

---

**O byte tem DOIS lados.** Na V3 `feeProtocol` guarda o denominador do
protocolo para o token0 no nibble baixo e para o token1 no alto; o protocolo
fica com `1/n` da taxa do swap cuja ENTRADA e aquele token (`n = 0` desliga).
Hoje os dois nibbles sao iguais em todas as pools medidas, mas o contrato
aceita diferentes (`setFeeProtocol(feeProtocol0, feeProtocol1)`), e somar os
dois lados com o nibble de um so seria a suposicao de novo, um nivel abaixo.

O contrato so aceita `n` em {0, 4..10}. Qualquer outro valor e leitura errada
(palavra trocada do `slot0`, orientacao invertida), e levanta: publicar uma
fracao de um byte mal lido e pior que nao publicar.
"""

from __future__ import annotations

NIBBLES_VALIDOS = {0, 4, 5, 6, 7, 8, 9, 10}


def _nibble(fee_protocol: int, zero_for_one: bool) -> int:
    if not 0 <= fee_protocol <= 0xFF:
        raise ValueError(f"feeProtocol fora de um byte: {fee_protocol}")
    n = fee_protocol & 0x0F if zero_for_one else fee_protocol >> 4
    if n not in NIBBLES_VALIDOS:
        raise ValueError(f"feeProtocol 0x{fee_protocol:02x}: nibble {n} fora de "
                         "{0, 4..10}; leitura errada do slot0?")
    return n


def fracao_protocolo_v3(fee_protocol: int, zero_for_one: bool) -> float:
    """O que o PROTOCOLO leva da taxa de um swap. `zero_for_one` = entra token0."""
    n = _nibble(fee_protocol, zero_for_one)
    return 1.0 / n if n else 0.0


def fracao_lp_v3(fee_protocol: int, zero_for_one: bool | None = None) -> float:
    """O que o LP leva da taxa de um swap (0x66 -> 5/6, 0x44 -> 3/4).

    Sem o sentido (`None`), so responde quando os dois lados sao iguais — que
    e o caso de toda pool medida em 24/09. Com lados diferentes, quem nao sabe
    o sentido nao tem como saber a fracao, e levanta em vez de escolher um."""
    if zero_for_one is None:
        a, b = fracao_protocolo_v3(fee_protocol, True), fracao_protocolo_v3(fee_protocol, False)
        if a != b:
            raise ValueError(f"feeProtocol 0x{fee_protocol:02x} tem lados diferentes: "
                             "a fracao depende do sentido do swap")
        return 1.0 - a
    return 1.0 - fracao_protocolo_v3(fee_protocol, zero_for_one)


def fee_protocol_do_slot0(s0: str) -> int:
    """A 6a palavra do `slot0()` da V3 (sqrtPrice, tick, obsIndex, obsCard,
    obsCardNext, **feeProtocol**, unlocked)."""
    h = s0[2:] if s0.startswith("0x") else s0
    return int(h[64 * 5:64 * 6], 16)


__all__ = ["fracao_lp_v3", "fracao_protocolo_v3", "fee_protocol_do_slot0", "NIBBLES_VALIDOS"]
