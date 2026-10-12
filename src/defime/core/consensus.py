"""Fase 4 — consenso dos concorrentes, ponderado por EFICIENCIA. Sem rede.

O erro obvio seria ponderar por capital: "onde esta a maior parte do dinheiro".
Isso mede quem tem mais dinheiro, nao quem esta certo. Numa pool que cresceu
1000x em 29 dias, boa parte do capital chegou ontem e ainda nao provou nada.

O peso aqui e **fee realizada por dia por $10k**, que e um resultado, nao uma
intencao. Posicao sem idade suficiente para ter resultado nao vota — e o numero
de votantes vai no relatorio, porque um consenso de 3 posicoes nao e consenso.

Fee realizada e exata (vem de `npm.Collect` menos o principal de
`DecreaseLiquidity`). Nao depende do gargalo de `feeGrowthOutside`, e foi por
isso que o consenso pode ser construido antes das fees pendentes.
"""

from __future__ import annotations

from .competitors import Concorrente
from .v3math import tick_to_usd

IDADE_MINIMA_DIAS = 0.5
VOTANTES_MINIMOS = 10


def consenso(concorrentes: list[Concorrente], *,
             idade_minima_dias: float = IDADE_MINIMA_DIAS,
             quantil: float = 0.5,
             incluir_nossa: bool = False) -> dict:
    """Faixa em que a liquidez EFICIENTE se concentra.

    `quantil` define o corte de eficiencia: 0,5 = so a metade superior vota.
    """
    elegiveis = [c for c in concorrentes
                 if c.eficiencia_usd_dia_por_10k is not None
                 and c.idade_dias >= idade_minima_dias
                 and (incluir_nossa or not c.nossa)]
    if not elegiveis:
        return {"votantes": 0, "confiavel": False,
                "motivo": "nenhuma posicao com eficiencia medivel"}

    ordenados = sorted(elegiveis, key=lambda c: -(c.eficiencia_usd_dia_por_10k or 0))
    corte = max(1, int(len(ordenados) * quantil))
    votantes = ordenados[:corte]
    peso_total = sum(c.eficiencia_usd_dia_por_10k or 0.0 for c in votantes)
    if peso_total <= 0:
        return {"votantes": len(votantes), "confiavel": False,
                "motivo": "todas as eficiencias sao zero"}

    # borda ponderada, em tick (espaco linear em log-preco — o certo para media)
    baixo = sum(c.tick_lower * (c.eficiencia_usd_dia_por_10k or 0)
                for c in votantes) / peso_total
    alto = sum(c.tick_upper * (c.eficiencia_usd_dia_por_10k or 0)
               for c in votantes) / peso_total
    largura = sum(c.largura_ticks * (c.eficiencia_usd_dia_por_10k or 0)
                  for c in votantes) / peso_total
    assimetria = sum(c.assimetria * (c.eficiencia_usd_dia_por_10k or 0)
                     for c in votantes) / peso_total

    efs = sorted(c.eficiencia_usd_dia_por_10k or 0 for c in votantes)
    return {
        "votantes": len(votantes),
        "elegiveis": len(elegiveis),
        "confiavel": len(votantes) >= VOTANTES_MINIMOS,
        "tick_lower": int(round(baixo)),
        "tick_upper": int(round(alto)),
        "faixa_usd": [round(float(tick_to_usd(int(round(alto)), 6, 18)), 3),
                      round(float(tick_to_usd(int(round(baixo)), 6, 18)), 3)],
        "largura_ticks_media": int(round(largura)),
        "assimetria_media": round(assimetria, 3),
        "eficiencia_mediana_dos_votantes": round(efs[len(efs) // 2], 2),
        "eficiencia_maxima": round(efs[-1], 2),
        "criterio": f"metade superior por fee realizada/dia/$10k, "
                    f"idade >= {idade_minima_dias}d",
    }


def comparar_com_a_nossa(concorrentes: list[Concorrente],
                         cons: dict) -> dict | None:
    """Onde estamos em relacao ao consenso — em dolares, nao em adjetivos."""
    nossa = next((c for c in concorrentes if c.nossa), None)
    if nossa is None or not cons.get("tick_lower"):
        return None
    n_baixo, n_alto = nossa.faixa_usd
    c_baixo, c_alto = cons["faixa_usd"]
    ef_nossa = nossa.eficiencia_usd_dia_por_10k
    ef_cons = cons["eficiencia_mediana_dos_votantes"]
    return {
        "nossa_faixa_usd": [round(n_baixo, 3), round(n_alto, 3)],
        "consenso_faixa_usd": c_baixo and [c_baixo, c_alto],
        "deslocamento_borda_baixa_usd": round(c_baixo - n_baixo, 3),
        "deslocamento_borda_alta_usd": round(c_alto - n_alto, 3),
        "nossa_largura_ticks": nossa.largura_ticks,
        "consenso_largura_ticks": cons["largura_ticks_media"],
        "somos_mais_largos": nossa.largura_ticks > cons["largura_ticks_media"],
        "nossa_eficiencia": round(ef_nossa, 2) if ef_nossa is not None else None,
        "eficiencia_do_consenso": ef_cons,
        "razao": (round(ef_nossa / ef_cons, 2)
                  if ef_nossa is not None and ef_cons else None),
        "confiavel": cons.get("confiavel", False),
    }


__all__ = ["consenso", "comparar_com_a_nossa", "IDADE_MINIMA_DIAS",
           "VOTANTES_MINIMOS"]
