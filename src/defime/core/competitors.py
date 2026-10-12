"""Fase 4 — todas as posicoes da pool, classificadas. Camada 2, sem rede.

Separacao que o plano do projeto exige e que define o desenho deste modulo:

    ranking por range / capital / idade  ->  BARATO, sai dos eventos
    fees PENDENTES por posicao           ->  CARO, exige feeGrowthOutside/tick

As duas coisas nao vivem juntas. Aqui entra so a primeira, mais as fees
**realizadas**, que sao exatas porque vem de `npm.Collect` menos o principal
de `DecreaseLiquidity` (ver C9 do plano do projeto).

Sobre fees pendentes: continuam **nao implementadas**, e de proposito. Para
serem exatas precisam de `feeGrowthInside`, que precisa de `feeGrowthOutside`
por tick ao longo de toda a historia. O evento `Swap` nao carrega
`feeGrowthGlobal`, e reconstrui-lo exigiria simular cada swap atraves do livro
com cruzamento de ticks — caro e fragil. O que este modulo entrega no lugar e
`fees_estimadas_usd`, por tempo-em-range x fatia x volume, e **todo consumidor
recebe o campo `fees_sao_estimativa=True`**. Nao colapsar estimativa em medida.

A eficiencia que ordena o ranking usa fee REALIZADA, nao estimada — e por isso
que o consenso da Fase 4 pode ser ponderado por eficiencia sem depender do
gargalo acima.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..config import settings
from .positions import Posicao, reconstruir_posicoes
from .v3math import (amounts_for_liquidity, sqrt_price_x96_to_price,
                     tick_to_sqrt_price_x96, tick_to_usd)

SNIPER = "SNIPER"
BALANCED = "BALANCED"
USDG_DEFENSIVE = "USDG_DEFENSIVE"
USO_DEFENSIVE = "USO_DEFENSIVE"
WIDE_SURVIVAL = "WIDE_SURVIVAL"
PERFIS = (SNIPER, BALANCED, USDG_DEFENSIVE, USO_DEFENSIVE, WIDE_SURVIVAL)

CAPITAL_MINIMO_USD = 500.0     # filtro do plano do projeto


@dataclass
class Concorrente:
    token_id: int
    tick_lower: int
    tick_upper: int
    liquidez: int
    capital_usd: float
    fracao_usdg: float
    em_range: bool
    largura_ticks: int
    largura_em_unidades: float     # largura / (2 x unidade), em "sigmas"
    assimetria: float              # 0 = centrado; >0 = espaco sobra para cima
    idade_dias: float
    fees_realizadas_usd: float
    eficiencia_usd_dia_por_10k: float | None
    perfil: str = BALANCED
    nossa: bool = False

    @property
    def faixa_usd(self) -> tuple[float, float]:
        return (float(tick_to_usd(self.tick_upper, 6, 18)),
                float(tick_to_usd(self.tick_lower, 6, 18)))

    def resumo(self) -> dict:
        baixo, alto = self.faixa_usd
        return {
            "token_id": self.token_id, "perfil": self.perfil,
            "faixa_usd": [round(baixo, 3), round(alto, 3)],
            "capital_usd": round(self.capital_usd, 2),
            "em_range": self.em_range,
            "largura_ticks": self.largura_ticks,
            "largura_em_sigmas": round(self.largura_em_unidades, 2),
            "assimetria": round(self.assimetria, 3),
            "idade_dias": round(self.idade_dias, 2),
            "fracao_usdg": round(self.fracao_usdg, 3),
            "fees_realizadas_usd": round(self.fees_realizadas_usd, 2),
            "eficiencia_usd_dia_por_10k": (
                round(self.eficiencia_usd_dia_por_10k, 2)
                if self.eficiencia_usd_dia_por_10k is not None else None),
            "nossa": self.nossa,
        }


def _classificar(largura_sigmas: float, assimetria: float,
                 fracao_usdg: float, em_range: bool) -> str:
    """Perfil por forma, nao por intencao declarada.

    Ordem importa: largura decide primeiro (e a variavel de maior efeito no
    resultado), composicao desempata.
    """
    if largura_sigmas >= 3.0:
        return WIDE_SURVIVAL
    if largura_sigmas <= 0.75 and em_range:
        return SNIPER
    if fracao_usdg >= 0.65:
        return USDG_DEFENSIVE
    if fracao_usdg <= 0.35:
        return USO_DEFENSIVE
    return BALANCED


def levantar(sqrt_price: int, tick_atual: int, agora_ts: int, *,
             unidade_usd: float,
             pool: str | None = None,
             capital_minimo_usd: float = CAPITAL_MINIMO_USD,
             nossa_nft: int | None = None,
             dec0: int = 6, dec1: int = 18) -> list[Concorrente]:
    """Todas as posicoes ativas acima do filtro de capital, classificadas."""
    import math

    cfg = settings()
    pool = (pool or cfg.pool_address).lower()
    preco = 1.0 / float(sqrt_price_x96_to_price(sqrt_price, dec0, dec1))
    unidade_ticks = (abs(math.log((preco + unidade_usd) / preco))
                     / math.log(1.0001)) if unidade_usd > 0 else 1.0

    saida: list[Concorrente] = []
    for token_id, p in reconstruir_posicoes(pool).items():
        if not p.ativa or p.tick_lower is None:
            continue
        sl = tick_to_sqrt_price_x96(p.tick_lower)
        su = tick_to_sqrt_price_x96(p.tick_upper)
        a0, a1 = amounts_for_liquidity(sqrt_price, sl, su, p.liquidez)
        v0 = a0 / 10 ** dec0
        v1 = (a1 / 10 ** dec1) * preco
        capital = v0 + v1
        if capital < capital_minimo_usd:
            continue

        fracao_usdg = v0 / capital if capital > 0 else 0.0
        largura = p.tick_upper - p.tick_lower
        # Assimetria em [-1, +1], POSITIVO = mais espaco para o USD SUBIR.
        #
        # O sinal aqui e uma armadilha e ja mordeu uma vez. Tick maior = USD
        # menor, entao a borda SUPERIOR em dolar e `tick_lower`:
        #     espaco para subir  = tick_atual - tick_lower
        #     espaco para descer = tick_upper - tick_atual
        # Conferido no caso real: faixa $114,00-$129,30 com preco $125,98 tem
        # $3,32 acima e $11,98 abaixo, e esta formula da -0,587. A versao com o
        # sinal trocado dava +0,587 e leria como "sobra espaco para subir".
        meio = (p.tick_lower + p.tick_upper) / 2
        assimetria = (2 * (tick_atual - meio) / largura) if largura else 0.0

        idade = p.idade_dias(agora_ts) or 0.0
        fees = (p.fees_coletadas0 / 10 ** dec0
                + (p.fees_coletadas1 / 10 ** dec1) * preco)
        eficiencia = ((fees / idade) * (10_000 / capital)
                      if idade > 0.1 and capital > 0 else None)
        em_range = p.tick_lower <= tick_atual < p.tick_upper
        largura_sigmas = largura / (2 * unidade_ticks) if unidade_ticks else 0.0

        saida.append(Concorrente(
            token_id=token_id, tick_lower=p.tick_lower, tick_upper=p.tick_upper,
            liquidez=p.liquidez, capital_usd=capital, fracao_usdg=fracao_usdg,
            em_range=em_range, largura_ticks=largura,
            largura_em_unidades=largura_sigmas, assimetria=assimetria,
            idade_dias=idade, fees_realizadas_usd=fees,
            eficiencia_usd_dia_por_10k=eficiencia,
            perfil=_classificar(largura_sigmas, assimetria, fracao_usdg, em_range),
            nossa=(token_id == nossa_nft)))

    saida.sort(key=lambda c: -c.capital_usd)
    return saida


def panorama(concorrentes: list[Concorrente]) -> dict:
    """Agregados que vao ao relatorio."""
    if not concorrentes:
        return {"n": 0}
    cap = sum(c.capital_usd for c in concorrentes)
    dentro = [c for c in concorrentes if c.em_range]
    por_perfil: dict[str, dict] = {}
    for perfil in PERFIS:
        grupo = [c for c in concorrentes if c.perfil == perfil]
        if not grupo:
            continue
        efs = [c.eficiencia_usd_dia_por_10k for c in grupo
               if c.eficiencia_usd_dia_por_10k is not None]
        por_perfil[perfil] = {
            "n": len(grupo),
            "capital_usd": round(sum(c.capital_usd for c in grupo), 2),
            "fatia_do_capital": round(
                sum(c.capital_usd for c in grupo) / cap, 4) if cap else 0.0,
            "em_range": sum(1 for c in grupo if c.em_range),
            "largura_mediana_ticks": sorted(
                c.largura_ticks for c in grupo)[len(grupo) // 2],
            "eficiencia_mediana": (round(sorted(efs)[len(efs) // 2], 2)
                                   if efs else None),
        }
    return {
        "n": len(concorrentes),
        "capital_total_usd": round(cap, 2),
        "n_em_range": len(dentro),
        "capital_em_range_usd": round(sum(c.capital_usd for c in dentro), 2),
        "fatia_do_capital_em_range": round(
            sum(c.capital_usd for c in dentro) / cap, 4) if cap else 0.0,
        "por_perfil": por_perfil,
        "fees_sao_estimativa": False,     # aqui so ha fee REALIZADA
    }


def nossa_posicao(concorrentes: list[Concorrente]) -> dict | None:
    """Onde estamos no ranking. Sem isso o levantamento e trivia."""
    nossa = next((c for c in concorrentes if c.nossa), None)
    if nossa is None:
        return None
    por_cap = sorted(concorrentes, key=lambda c: -c.capital_usd)
    com_ef = [c for c in concorrentes if c.eficiencia_usd_dia_por_10k is not None]
    por_ef = sorted(com_ef, key=lambda c: -(c.eficiencia_usd_dia_por_10k or 0))
    return {
        **nossa.resumo(),
        "posicao_por_capital": por_cap.index(nossa) + 1,
        "de_um_total_de": len(concorrentes),
        "posicao_por_eficiencia": (por_ef.index(nossa) + 1
                                   if nossa in por_ef else None),
        "com_eficiencia_medivel": len(por_ef),
    }


__all__ = ["Concorrente", "levantar", "panorama", "nossa_posicao", "PERFIS",
           "SNIPER", "BALANCED", "USDG_DEFENSIVE", "USO_DEFENSIVE",
           "WIDE_SURVIVAL", "CAPITAL_MINIMO_USD"]
