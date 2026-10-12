"""O gas desta chain: quanto e, e quando ele passa a decidir alguma coisa.

**Por que este modulo existe, tendo o gas medido em $0,88 por rerange.** Em
07/09 o usuário observou que a taxa da rede tinha subido, e ele estava certo:
210 amostras de `baseFeePerGas` entre 17/07 e 07/09 dao mediana de **0,0201
gwei na semana 33 contra 0,4018 na semana 35** — vinte vezes em duas semanas,
com as transacoes por bloco dobrando. **Ninguem notou**, porque nada media.

O absoluto continua sendo ruido: no gas caro de hoje um swap custa $0,189, um
mint $0,456 e o rerange inteiro **$0,905**, contra ~$120 de taxa de interface na
mesma operacao. Mas "e ruido HOJE" e uma medicao, nao uma lei — e a diferenca
entre as duas e exatamente uma sentinela.

**Camada 2: puro.** Sem I/O, sem rede, sem banco. Quem le a chain e
o job do painel; quem le o banco tambem. Aqui so mora a aritmetica e os dois
criterios.

**Guarda gwei, nao dolar.** O gwei vem do bloco e e exato e permanente; o dolar
e o gwei vezes uma ESCOLHA de preco do ETH, que muda e pode faltar. Foi a mesma
licao que a fee pendente pagou em 07/09, quando $14,86 de "divergencia" contra
a Revert eram inteiramente o preco do UPS. Numero convertido e guardado congela
a escolha; numero convertido na leitura a declara.
"""

from __future__ import annotations

import statistics

# Gas MEDIDO nos recibos da propria carteira (Test Lab, 02/09) e conferido
# contra transacoes de terceiros em 07/09: mint 491.148 e swap 203.786 lidos ao
# vivo, contra 484.697 e ~180.000 registrados antes. Sao estimativas de ordem,
# nao constantes do protocolo — o consumo varia com o caminho do swap e com
# quantos ticks ele atravessa.
GAS_MINT = 484_697
GAS_BURN = 285_762
GAS_SWAP = 203_786
GAS_RERANGE = GAS_BURN + GAS_SWAP + GAS_MINT

# 1,6000% em dez de dez swaps medidos na migracao de 04/09, comprando ou
# vendendo, de $24 a $2.846. Nao e slippage e nao e taxa da pool: e o que o
# `PoolManager` entrega a `0x147cf09e...` em toda passagem pela interface.
TAXA_INTERFACE = 0.016

# **O limiar e DERIVADO, e essa e a decisao que mais importa aqui.** Escolher
# "alertar acima de X gwei" seria escolher um numero: o gwei nao diz nada
# sozinho, porque o que decide e o gas contra o CUSTO DA MESMA OPERACAO. Abaixo
# de 1% da taxa de interface daquele rerange, nenhuma escolha de horario muda
# uma decisao — a economia inteira cabe no arredondamento do resto da conta.
# Acima dele o gas comeca a existir, e ai o alerta e informacao.
#
# Medido em 07/09: o rerange da UPS esta em 0,7% — perto, e e por isso que a
# sentinela se paga hoje em vez de daqui a seis meses.
FRACAO_QUE_IMPORTA = 0.01

# Para o portao de recomendacao de horario. **Os dois criterios sao
# necessarios**: n>=30 e o minimo que o proprio plano do analista pede, e a
# razao de 1,5x existe porque abaixo dela a "melhor hora" nao paga o custo de
# esperar por ela. Medido em 07/09, ultimos 14 dias: razao 1,09x com n=14 por
# faixa — reprova nos dois.
MIN_AMOSTRAS_POR_FAIXA = 30
RAZAO_MINIMA = 1.5


def custo_usd(base_gwei: float, gas: int, eth_usd: float) -> float:
    """gwei x gas x preco do ETH. Linear nos tres, e o preco e declarado."""
    return base_gwei * 1e-9 * gas * eth_usd


def sentinela(base_gwei: float, eth_usd: float | None,
              volume_swap_usd: float,
              gas: int = GAS_RERANGE) -> dict:
    """O gas ja importa nesta operacao?

    Devolve o custo, a taxa de interface da MESMA operacao e a fracao entre os
    dois — porque o numero solto nao responde nada. `alerta` fica `True` quando
    a fracao passa de `FRACAO_QUE_IMPORTA`.

    **Sem preco do ETH nao ha veredito**, e ele sai `None` em vez de zero ou de
    um palpite: a regra deste projeto e que ausencia se declara. O gwei
    continua publicado, porque ele foi medido.
    """
    if eth_usd is None or eth_usd <= 0:
        return {"base_gwei": base_gwei, "custo_usd": None,
                "interface_usd": None, "fracao": None, "alerta": None,
                "motivo": "sem preço do ETH — o gwei está medido, o dólar não"}
    c = custo_usd(base_gwei, gas, eth_usd)
    interface = volume_swap_usd * TAXA_INTERFACE
    fr = (c / interface) if interface > 0 else None
    return {"base_gwei": base_gwei, "custo_usd": c,
            "interface_usd": interface, "fracao": fr,
            "alerta": bool(fr is not None and fr > FRACAO_QUE_IMPORTA),
            "limiar": FRACAO_QUE_IMPORTA, "motivo": None}


def resumo(gweis: list[float]) -> dict | None:
    """p10 / mediana / p90 de uma amostra, e a economia entre os extremos.

    Percentil por indice e nao por interpolacao: e a mesma escolha que o
    `median_high` do `/compor/` — o limiar tem de ser um valor que aconteceu.
    """
    v = sorted(x for x in gweis if x is not None)
    if len(v) < 3:
        return None
    return {"n": len(v),
            "p10": v[int(0.10 * (len(v) - 1))],
            "mediana": statistics.median(v),
            "p90": v[int(0.90 * (len(v) - 1))],
            "min": v[0], "max": v[-1]}


def pode_recomendar_horario(por_faixa: dict[int, list[float]]) -> dict:
    """O padrao por hora e forte o bastante para virar recomendacao?

    **Existir tabela nao e existir padrao**, e este portao e a diferenca. Uma
    grade semana x hora sempre tem uma celula mais barata; publicar essa celula
    como "melhor horario" transforma ruido em conselho — foi como o ranking
    noturno publicou p90 de duas noites e a LULU foi ao topo com 6,7 horas de
    historia.

    Dois criterios, os dois necessarios:
      * `n >= MIN_AMOSTRAS_POR_FAIXA` em **todas** as faixas comparadas — a
        razao entre uma faixa medida e uma faixa chutada nao e razao;
      * `razao >= RAZAO_MINIMA` entre a mediana mais cara e a mais barata.

    Medido em 07/09 (14 dias, quatro faixas de 6h): razao **1,09x** com n=14.
    Reprova nos dois, e o veredito diz nos dois.
    """
    faixas = {k: sorted(v) for k, v in por_faixa.items() if v}
    if len(faixas) < 2:
        return {"pode": False, "razao": None, "n_min": 0,
                "motivo": "menos de duas faixas com amostra"}
    n_min = min(len(v) for v in faixas.values())
    meds = {k: statistics.median(v) for k, v in faixas.items()}
    barato = min(meds.values())
    caro = max(meds.values())
    razao = (caro / barato) if barato > 0 else None
    faltas = []
    if n_min < MIN_AMOSTRAS_POR_FAIXA:
        faltas.append("amostra mínima %d, tem %d na faixa mais pobre"
                      % (MIN_AMOSTRAS_POR_FAIXA, n_min))
    if razao is None or razao < RAZAO_MINIMA:
        faltas.append("razão caro/barato %s, mínimo %.2fx"
                      % ("%.2fx" % razao if razao else "indefinida",
                         RAZAO_MINIMA))
    return {"pode": not faltas, "razao": razao, "n_min": n_min,
            "mais_barata": min(meds, key=meds.get),
            "mais_cara": max(meds, key=meds.get),
            "motivo": " · ".join(faltas) or None}
