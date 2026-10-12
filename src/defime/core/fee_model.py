"""Duas correcoes que a regra simples exigiu. Camada 2 — sem rede.

1. SIGMA robusto a microestrutura
   A serie vem dos ticks dos Swaps e a banda de fee de 0,30% deixa o preco
   quicar sem informacao. Sintoma medido: RV anualizada de 82,3% em barras de
   1h contra 66,7% em 4h — difusao pura daria o mesmo numero em qualquer
   frequencia; vol que encolhe com o intervalo e ruido.

   Garman-Klass usa a AMPLITUDE da barra (max/min) em vez do fechamento, o que
   o torna muito menos sensivel a esse quique. Comparar os dois estimadores da
   a magnitude do ruido — e o proprio diagnostico.

2. FEE como funcao da LARGURA
   Para capital fixo, alargar o range dilui a liquidez por tick. A fee que se
   recebe e proporcional a FATIA da liquidez ativa onde o preco esta:

       fee_esperada ~ SOMA_por_faixa( tempo_ali x L_minha / L_total_ali )

   Tratar fee como constante fazia alargar parecer de graca. Era o segundo
   vies que empurrava a regra para ranges largos demais.
"""

from __future__ import annotations

import math
from decimal import Decimal

from .book import TickBook
from .prices import Barra
from .v3math import (amounts_for_liquidity, liquidity_for_amounts,
                     tick_to_sqrt_price_x96)


# ---------------------------------------------------------------- sigma

def sigma_garman_klass(barras_: list[Barra], intervalo_s: int,
                       anualizar: bool = False) -> float | None:
    """Estimador de Garman-Klass sobre OHLC. Robusto a quique de bid/ask.

    var = 0,5*(ln(H/L))^2 - (2ln2 - 1)*(ln(C/O))^2
    """
    amostras = []
    for b in barras_:
        if min(b.abertura, b.maxima, b.minima, b.fechamento) <= 0:
            continue
        hl = math.log(b.maxima / b.minima)
        co = math.log(b.fechamento / b.abertura)
        v = 0.5 * hl * hl - (2 * math.log(2) - 1) * co * co
        if v > 0:
            amostras.append(v)
    if len(amostras) < 3:
        return None
    sigma = math.sqrt(sum(amostras) / len(amostras))
    if not anualizar:
        return sigma
    return sigma * math.sqrt((365 * 24 * 3600) / intervalo_s)


def diagnostico_microestrutura(barras_1h: list[Barra],
                               barras_4h: list[Barra]) -> dict:
    """Mede o ruido comparando estimadores e frequencias.

    Se `razao_1h_4h` >> 1 no estimador de fechamento e ~1 no Garman-Klass, o
    excesso e microestrutura e o GK deve ser preferido.
    """
    from .prices import volatilidade_realizada
    cc1 = volatilidade_realizada(barras_1h, 3600)
    cc4 = volatilidade_realizada(barras_4h, 14400)
    gk1 = sigma_garman_klass(barras_1h, 3600, anualizar=True)
    gk4 = sigma_garman_klass(barras_4h, 14400, anualizar=True)
    return {
        "close_to_close_1h": cc1, "close_to_close_4h": cc4,
        "garman_klass_1h": gk1, "garman_klass_4h": gk4,
        "razao_cc_1h_4h": (cc1 / cc4) if cc1 and cc4 else None,
        "razao_gk_1h_4h": (gk1 / gk4) if gk1 and gk4 else None,
        "excesso_de_ruido_1h": (cc1 / gk1) if cc1 and gk1 else None,
    }


# ---------------------------------------------------------------- fee/largura

def fatia_de_liquidez(book: TickBook, tick_lower: int, tick_upper: int,
                      liquidez_minha: int,
                      remover_propria: bool = True) -> list[dict]:
    """Por faixa dentro do range, qual fatia da liquidez ativa seria minha."""
    net = dict(book.net)
    if remover_propria:
        net[tick_lower] = net.get(tick_lower, 0) - liquidez_minha
        net[tick_upper] = net.get(tick_upper, 0) + liquidez_minha
        net = {t: v for t, v in net.items() if v != 0}

    fronteiras = sorted({t for t in net if tick_lower < t < tick_upper}
                        | {tick_lower, tick_upper})
    faixas = []
    for i in range(len(fronteiras) - 1):
        a, b = fronteiras[i], fronteiras[i + 1]
        outros = sum(v for t, v in net.items() if t <= a)
        outros = max(0, outros)
        total = outros + liquidez_minha
        faixas.append({
            "tick_de": a, "tick_ate": b, "largura_ticks": b - a,
            "liquidez_outros": outros,
            "fatia_minha": (liquidez_minha / total) if total else 0.0,
        })
    return faixas


def fee_relativa_esperada(book: TickBook, sqrt_price: int,
                          tick_lower: int, tick_upper: int,
                          capital_liquidez_ref: int,
                          tick_atual: int) -> float:
    """Fee esperada por unidade de tempo em range, relativa.

    Reune os dois efeitos que a regra simples ignorava:
      - capital fixo espalhado em range largo => L menor por tick
      - L menor => fatia menor da liquidez ativa => menos fee

    Devolve numero comparavel entre candidatos, nao valor absoluto.
    """
    faixas = fatia_de_liquidez(book, tick_lower, tick_upper, capital_liquidez_ref)
    if not faixas:
        return 0.0
    # a faixa onde o preco esta hoje domina a fee de curto prazo
    for f in faixas:
        if f["tick_de"] <= tick_atual < f["tick_ate"]:
            return f["fatia_minha"]
    return sum(f["fatia_minha"] for f in faixas) / len(faixas)


def liquidez_para_capital(sqrt_price: int, tick_lower: int, tick_upper: int,
                          capital_usd: Decimal, preco_usd: Decimal,
                          dec0: int, dec1: int) -> int:
    """Quanta liquidez L o mesmo capital compra num dado range.

    E aqui que a largura entra: range mais largo => L menor para o mesmo
    dinheiro. Era exatamente o efeito ausente do modelo anterior.
    """
    sqrt_l = tick_to_sqrt_price_x96(tick_lower)
    sqrt_u = tick_to_sqrt_price_x96(tick_upper)
    ref = 10 ** 18
    r0, r1 = amounts_for_liquidity(sqrt_price, sqrt_l, sqrt_u, ref)
    valor_ref = (Decimal(r0) / Decimal(10 ** dec0)
                 + (Decimal(r1) / Decimal(10 ** dec1)) * preco_usd)
    if valor_ref <= 0:
        return 0
    return int(Decimal(ref) * capital_usd / valor_ref)


__all__ = ["sigma_garman_klass", "diagnostico_microestrutura",
           "fatia_de_liquidez", "fee_relativa_esperada",
           "liquidez_para_capital"]
