"""Compositor de faixa: quanto comprar de cada token ANTES de abrir.

**A pergunta que o resto do projeto nunca respondeu.** Todo o sistema mede
posicao que JA existe — fee, divergencia, tempo em range. Este modulo responde
o passo anterior: dada uma faixa e um capital, quanto de stable e quanto de
ativo a posicao vai exigir, e **qual swap tira voce de onde esta para la**.

Tres coisas que este modulo faz e que uma calculadora generica nao faz:

1. **Publica a faixa EXECUTAVEL ao lado da pedida.** O tick tem passo, e a
   pool arredonda. Calcular a composicao no preco digitado e responder sobre
   uma faixa que nao existe naquela pool. Toda conta final usa o tick
   alinhado.
2. **Trata a orientacao como FATO, nao como escolha.** Com a stable em token0
   os ticks invertem — o tick maior e o preco MENOR. Supor isso ja fez GME e
   SLV valerem zero neste projeto, e inverte o preco em 11 das 18 pools desta
   chain.
3. **Cobra o swap pelo custo MEDIDO desta chain**, nao pela taxa nominal. Sao
   1,60% de taxa de interface (medido identico nos 10 swaps de 04/09, de $24 a
   $2.846) mais a taxa da pool. Abrir faixa centrada partindo de um lado so
   custa ~$148 em $15.000 — e e esse numero que decide, nao a proporcao.

**`core/` e puro** (inviolavel 3): este modulo nao le banco, nao toca rede e
nao importa o catalogo do painel. Os parametros da pool chegam como
primitivos em `Mercado`, e quem os busca e o chamador.

A aritmetica de tick e amount e a de `core.v3math`, a MESMA que reconcilia ao
wei contra o contrato — nao ha segunda implementacao aqui. O que este modulo
acrescenta e a inversao (capital -> L) e a leitura em dolar.
"""

from __future__ import annotations

from dataclasses import dataclass

from .v3math import (align_to_spacing, amounts_for_liquidity,
                     tick_to_sqrt_price_x96)

# **O piso de execucao tem nome e endereco.** Medido em 2026-09-04 nas 10
# pernas da migracao RBLX -> UPS: o `PoolManager` entrega o token de saida em
# duas transferencias, e a segunda — para `0x147cf09e...` — e 1,6000% do total
# nas dez, sem excecao, comprando ou vendendo, em pool de 0,12% ou de 0,35%.
# Nao e slippage: a ordem de $2.846 num book de $151k nao moveu o preco.
TAXA_INTERFACE = 0.016

# L de referencia para a inversao capital -> liquidez. Qualquer valor serve
# (a conta e uma proporcao); 1e18 mantem os inteiros longe do arredondamento.
L_REF = 10 ** 18


@dataclass(frozen=True)
class Mercado:
    """Os parametros da pool que a composicao precisa.

    Primitivos de proposito: `core/` nao importa registro de pools. Quem monta
    isto e o chamador, lendo de `pool_param` — nunca supondo, porque as tres
    suposicoes obvias ja custaram caro aqui (taxa fixa em 0,30% fez a GOOGL
    render +$1.691 numa pool que ganhou $457; spacing fixo em 60 pos a RDDT em
    primeiro com uma faixa que nao existe; stable em token0 zerou GME e SLV).
    """

    usdg_token0: bool
    dec_stable: int
    dec_ativo: int
    tick_spacing: int
    fee_pips: int
    # Taxa que o SWAP paga, em fracao. Pode ser MAIOR que `fee_pips`: na V4 o
    # campo `fee` do evento e a taxa total (lpFee + protocolFee sobre a
    # entrada) — na UPS o contrato declara 3000 e o swap paga 3499. `None`
    # cai na declarada, que e o piso.
    taxa_swap: float | None = None

    @property
    def taxa_swap_frac(self) -> float:
        return self.taxa_swap if self.taxa_swap is not None else self.fee_pips / 1e6


def preco_do_tick(m: Mercado, tick: int) -> float:
    """Preco do ativo em dolar, respeitando a orientacao da pool.

    A razao nativa do Uniswap e `token1/token0`; com a stable em token0 ela e
    "ativo por dolar" e o preco em dolar e o INVERSO. Mesma definicao de
    `defime.jobs.heatmap.preco_do_tick` — nao uma segunda.
    """
    expo = m.dec_ativo - m.dec_stable
    return (1.0001 ** (-tick if m.usdg_token0 else tick)) * (10 ** expo)


def tick_continuo(m: Mercado, preco: float) -> float:
    """O tick de um preco ANTES de arredondar — em `float`, de proposito.

    Existe separado porque o arredondamento e do chamador, e escolher errado
    ja custou uma faixa: `faixa_executavel` precisa de piso e teto para FORA,
    e arredondar para o mais proximo aqui movia a borda para DENTRO antes de
    o alinhamento ao spacing ter chance de corrigir.
    """
    import math
    if preco <= 0:
        raise ValueError("preco tem de ser positivo")
    expo = m.dec_ativo - m.dec_stable
    bruto = math.log(preco / (10 ** expo)) / math.log(1.0001)
    return -bruto if m.usdg_token0 else bruto


def tick_do_preco(m: Mercado, preco: float) -> int:
    """Inverso de `preco_do_tick`, sem arredondar ao spacing ainda."""
    return int(round(tick_continuo(m, preco)))


@dataclass(frozen=True)
class Faixa:
    """A faixa pedida e a que a pool aceita.

    As duas saem juntas porque a diferenca e informacao: quem digitou
    114,00-129,00 numa pool de spacing 60 abre 113,98-129,04, e toda conta
    daqui para a frente usa a segunda.
    """

    tick_lower: int
    tick_upper: int
    piso: float           # executavel
    teto: float
    piso_pedido: float
    teto_pedido: float

    @property
    def largura_pct(self) -> float:
        return 100.0 * (self.teto - self.piso) / ((self.teto + self.piso) / 2)

    @property
    def desvio_piso_pct(self) -> float:
        return 100.0 * (self.piso - self.piso_pedido) / self.piso_pedido

    @property
    def desvio_teto_pct(self) -> float:
        return 100.0 * (self.teto - self.teto_pedido) / self.teto_pedido


def faixa_executavel(m: Mercado, piso: float, teto: float) -> Faixa:
    """Preco -> tick -> alinha ao spacing -> preco de volta.

    **O alinhamento e para FORA nos dois lados**, nao para o mais proximo: a
    faixa executavel contem a pedida. Arredondar para dentro entregaria uma
    posicao mais estreita do que a pessoa pediu, e estreitar sem avisar e a
    unica das duas direcoes que muda o risco sem o leitor saber.

    **E "para fora" comeca ANTES do spacing.** A primeira versao disto
    arredondava o tick para o mais proximo e so depois alinhava, e o portao
    de conformidade com o JS pegou o caso: pedir $38,95-$44,98 na RBLX
    (spacing 60) devolvia `238260..239700` = **$38,9477-$44,9797**, com o teto
    $0,0003 ABAIXO do pedido. O tick continuo de $44,98 e 238259,94; `round`
    o levou a 238260, que ja e multiplo de 60 — entao `align("down")` nao
    tinha o que corrigir e o estreitamento passava calado.

    Nao e caso de laboratorio: `238260..239700` sao os ticks da posicao POS_E
    que o documento de regras do projeto registra como "$38,95-$44,98". Agora o piso sai do PISO
    (`floor`) do tick continuo e o teto do TETO (`ceil`), e a conferencia
    final e explicita — porque a regra estava escrita em prosa e o codigo nao
    a cumpria, que e a familia de defeito que este projeto ja pagou seis
    vezes.

    **O preco de conter tem de ser dito, porque nem sempre e pequeno.** Na
    RBLX o pedido $38,95-$44,98 sai `238200..239700` = $38,9477-$45,2504: o
    teto sobe 0,6% — um spacing inteiro — para cobrir os $0,0003 que
    faltavam. A escolha e deliberada: alargar aparece na tela (a faixa
    executavel e a largura saem ao lado da pedida) e erra na direcao que NAO
    aumenta o risco, enquanto estreitar erraria calado na que aumenta. Quem
    quiser a faixa mais justa move o numero digitado e ve o resultado na
    hora — que e o que a pagina `/compor/` existe para permitir.
    """
    import math
    if piso >= teto:
        raise ValueError("piso tem de ser menor que o teto")
    c_a, c_b = tick_continuo(m, piso), tick_continuo(m, teto)
    lo = align_to_spacing(math.floor(min(c_a, c_b)), m.tick_spacing, "down")
    hi = align_to_spacing(math.ceil(max(c_a, c_b)), m.tick_spacing, "up")

    # Guarda explicita: um passo de spacing para fora enquanto a faixa nao
    # contiver a pedida. O `floor`/`ceil` acima ja garante isso analiticamente
    # — isto existe para o dia em que o erro de `float` no log encostar na
    # borda, e para que a promessa do docstring seja VERIFICADA e nao
    # afirmada. O limite de passos impede laco infinito se alguem inverter um
    # sinal aqui.
    # Com a stable em token0 o preco CAI com o tick: `lo` produz o teto e `hi`
    # produz o piso. Escrito com o nome do que se quer mover (baixar o piso,
    # subir o teto) e nao com o nome do tick, senao a orientacao inverte o
    # sentido do conserto sem ninguem notar.
    for _ in range(4):
        p_a, p_b = preco_do_tick(m, lo), preco_do_tick(m, hi)
        if min(p_a, p_b) <= piso and max(p_a, p_b) >= teto:
            break
        if min(p_a, p_b) > piso:                       # baixar o piso
            if m.usdg_token0:
                hi += m.tick_spacing
            else:
                lo -= m.tick_spacing
        if max(p_a, p_b) < teto:                       # subir o teto
            if m.usdg_token0:
                lo -= m.tick_spacing
            else:
                hi += m.tick_spacing

    p_a, p_b = preco_do_tick(m, lo), preco_do_tick(m, hi)
    return Faixa(tick_lower=lo, tick_upper=hi,
                 piso=min(p_a, p_b), teto=max(p_a, p_b),
                 piso_pedido=piso, teto_pedido=teto)


@dataclass(frozen=True)
class Composicao:
    """Quanto de cada token a faixa exige, e onde o preco esta nela."""

    qtd_stable: float
    qtd_ativo: float
    valor_stable: float
    valor_ativo: float
    liquidez: int
    estado: str            # "dentro" | "abaixo" | "acima"
    nivel_pct: float       # 0 = colado no piso, 100 = no teto; NAO e clipado
    dist_piso_pct: float
    dist_teto_pct: float

    @property
    def total(self) -> float:
        return self.valor_stable + self.valor_ativo

    @property
    def share_stable(self) -> float:
        return self.valor_stable / self.total if self.total else 0.0

    @property
    def share_ativo(self) -> float:
        return self.valor_ativo / self.total if self.total else 0.0


def preco_do_sqrt(m: Mercado, sqrt_price: int) -> float:
    """Preco em dolar de um `sqrtPriceX96`, sem passar pelo tick."""
    from decimal import Decimal, localcontext
    with localcontext() as ctx:       # NUNCA global; ver `_sqrt_do_preco`
        ctx.prec = 60
        razao = (Decimal(sqrt_price) / Decimal(1 << 96)) ** 2
        expo = Decimal(10) ** (m.dec_ativo - m.dec_stable)
        return float(expo / razao if m.usdg_token0 else razao * expo)


def compor(m: Mercado, faixa: Faixa, preco: float | None = None,
           capital: float | None = None,
           sqrt_price: int | None = None) -> Composicao:
    """A composicao da faixa ao preco dado.

    Sem `capital`, devolve as quantidades de um L de referencia — as
    PROPORCOES ja estao certas, que e o que se pergunta quando ainda nao se
    decidiu o tamanho. Com `capital`, escala.

    **`sqrt_price` quando ele existe, e nao o preco.** O preco da pool e
    CONTINUO; o tick e discreto. Passar o preco arredondado ao tick custa,
    numa faixa de 0,9%, quase **um ponto percentual de composicao** — medido
    na POS_A em 06/09: o `sqrtPriceX96` real estava a 1/3 de tick da borda
    e a leitura saia 37,79% stable contra os 36,84% verdadeiros, com 2,6% de
    erro na quantidade de USDG.

    E a familia "dois instantes" com outra roupa: aqui nao sao dois momentos,
    sao duas RESOLUCOES da mesma grandeza. Quem le da chain passa o
    `sqrt_price` e acerta ao wei; quem simula um preco hipotetico recebe a
    conversao continua, que e o melhor que existe sem uma pool naquele preco.

    **A escala sai da mesma funcao que monta o livro.** A primeira versao
    disto (em o job do painel, 05/09) derivou L de
    `cap / (preco * largura / 2)` e ignorou os decimais dos tokens: publicou
    `fatia 0,000% · APR 0%` nas cinco pools. Inverter `amounts_for_liquidity`
    sobre um L de referencia nao tem como errar decimal — e a mesma funcao nos
    dois sentidos.
    """
    # **Os dois juntos sao recusados, e isso e estrutural.** Passar
    # `sqrt_price` e `preco` permitiria um par inconsistente — os amounts
    # saindo de um e a avaliacao em dolar do outro —, que e exatamente o
    # defeito que o portao C1 pegou. Um OU o outro: com o sqrt, o preco e
    # DERIVADO dele.
    if (preco is None) == (sqrt_price is None):
        raise ValueError("informe `preco` OU `sqrt_price`, nunca os dois")
    if sqrt_price is not None:
        sq = sqrt_price
        preco = preco_do_sqrt(m, sqrt_price)
    else:
        sq = _sqrt_do_preco(m, preco)
    sq_lo = tick_to_sqrt_price_x96(faixa.tick_lower)
    sq_hi = tick_to_sqrt_price_x96(faixa.tick_upper)

    a0, a1 = amounts_for_liquidity(sq, sq_lo, sq_hi, L_REF)
    est_ref, atv_ref = (a0, a1) if m.usdg_token0 else (a1, a0)
    qs = est_ref / 10 ** m.dec_stable
    qa = atv_ref / 10 ** m.dec_ativo
    usd_ref = qs + qa * preco

    liq = L_REF
    if capital is not None:
        if capital <= 0:
            raise ValueError("capital tem de ser positivo")
        if usd_ref <= 0:
            raise ValueError("faixa sem valor no preco atual")
        liq = int(L_REF * capital / usd_ref)
        a0, a1 = amounts_for_liquidity(sq, sq_lo, sq_hi, liq)
        est, atv = (a0, a1) if m.usdg_token0 else (a1, a0)
        qs = est / 10 ** m.dec_stable
        qa = atv / 10 ** m.dec_ativo

    # **O estado sai dos AMOUNTS, nao da comparacao de precos.** Os dois
    # concordam no miolo e podem discordar na borda, porque o preco em dolar
    # e `float` e os amounts vem da comparacao exata de `sqrtPriceX96`. Quem
    # manda e quem produziu as quantidades — senao a pagina poderia dizer
    # "dentro" numa posicao que ja esta 100% de um lado.
    #
    # A regra e independente de orientacao: a LP VENDE o ativo enquanto o
    # preco sobe, entao acima da faixa nao sobra ativo, e abaixo nao sobra
    # stable.
    if qa <= 0:
        estado = "acima"
    elif qs <= 0:
        estado = "abaixo"
    else:
        estado = "dentro"

    larg = faixa.teto - faixa.piso
    return Composicao(
        qtd_stable=qs, qtd_ativo=qa,
        valor_stable=qs, valor_ativo=qa * preco,
        liquidez=liq, estado=estado,
        # Fora da faixa o nivel SAI do intervalo e nao e clipado: prender em
        # 0 e 100 empilharia no mesmo valor o que a coluna existe para
        # separar. Mesma decisao da coluna `nivel` da pagina da pool.
        nivel_pct=100.0 * (preco - faixa.piso) / larg if larg else 0.0,
        dist_piso_pct=100.0 * (preco - faixa.piso) / preco,
        dist_teto_pct=100.0 * (faixa.teto - preco) / preco)


def _sqrt_do_preco(m: Mercado, preco: float) -> int:
    """sqrtPriceX96 de um preco em dolar, CONTINUO.

    **Nao passa pelo tick.** A primeira versao disto fazia
    `preco -> tick -> sqrt`, e o arredondamento ao tick custou 2,6% na
    quantidade de USDG da POS_A — porque o preco da pool nao mora na borda
    de um tick, e numa faixa estreita 1/3 de tick vale ~1pp de composicao.

    A razao nativa e `token1/token0`; com a stable em token0 ela e "ativo por
    dolar" e sai do inverso do preco. `Decimal` porque `float` perde digito
    antes do expoente de 12 casas que separa 6 de 18 decimais.
    """
    from decimal import Decimal, localcontext
    if preco <= 0:
        raise ValueError("preco tem de ser positivo")
    # **`localcontext`, nunca `getcontext().prec = ...`.** O contexto do
    # `Decimal` e GLOBAL ao processo: mexer nele daqui mudou a precisao de
    # `decision/cost.py`, que calcula `Decimal(1)/Decimal(6)`, e reprovou um
    # teste dele que passa SOZINHO e falha na suite inteira. `core/` ser puro
    # nao e so nao fazer I/O — e nao mudar o mundo para os outros modulos.
    with localcontext() as ctx:
        ctx.prec = 60
        expo = Decimal(10) ** (m.dec_ativo - m.dec_stable)
        razao = (expo / Decimal(str(preco)) if m.usdg_token0
                 else Decimal(str(preco)) / expo)
        return int(razao.sqrt() * (1 << 96))


@dataclass(frozen=True)
class Swap:
    """O que falta comprar/vender para chegar na composicao, e o custo."""

    vende_ativo: float      # >0 vende ativo, <0 compra ativo
    volume_usd: float
    custo_usd: float
    custo_pct: float
    taxa_interface_usd: float
    taxa_pool_usd: float

    @property
    def sentido(self) -> str:
        if abs(self.vende_ativo) < 1e-12:
            return "nenhum"
        return "vender ativo" if self.vende_ativo > 0 else "comprar ativo"


def swap_para_abrir(m: Mercado, c: Composicao, preco: float,
                    tem_stable: float = 0.0,
                    tem_ativo: float = 0.0) -> Swap:
    """De onde voce esta ate a composicao que a faixa exige.

    **O custo nao e a taxa da pool.** Medido em 04/09: sao 1,60% de taxa de
    interface (`TAXA_INTERFACE`) mais a taxa do swap, e a interface respondeu
    por **84%** do custo daquela migracao ($204,18 de $243,17). Terceiros
    pagam 0,32% nas mesmas pools e tamanhos no mesmo minuto porque nao passam
    por ela.

    Sem holdings informados, supoe partir de 100% stable — o caso de quem
    esta entrando com dolar.
    """
    if tem_stable == 0.0 and tem_ativo == 0.0:
        tem_stable = c.total
    falta_ativo = c.qtd_ativo - tem_ativo
    volume = abs(falta_ativo) * preco
    interface = volume * TAXA_INTERFACE
    pool = volume * m.taxa_swap_frac
    custo = interface + pool
    return Swap(vende_ativo=-falta_ativo, volume_usd=volume,
                custo_usd=custo,
                custo_pct=100.0 * custo / c.total if c.total else 0.0,
                taxa_interface_usd=interface, taxa_pool_usd=pool)


__all__ = ["Mercado", "Faixa", "Composicao", "Swap", "TAXA_INTERFACE",
           "L_REF", "preco_do_tick", "tick_do_preco", "tick_continuo", "faixa_executavel",
           "compor", "swap_para_abrir"]
