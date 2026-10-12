"""Heatmap de tokens no estilo treemap — area = tamanho, cor = variacao.

**Puro:** recebe series por pool, devolve tiles com retangulos calculados. Sem
banco, sem rede, sem I/O. Quem le o banco e o job do painel.

---

**O tile e o TOKEN e nao a POOL, e essa e a primeira decisao.** Esta chain tem
tres pools de SPY, duas de GOOGL e duas de RBLX; um heatmap com uma celula por
pool publicaria o mesmo ativo tres vezes com tres tamanhos, e quem olha le
"tres ativos". TVL e volume **somam** entre as pools do token.

**Mas preco e variacao NAO se somam nem se mediam: saem da pool DOMINANTE**,
a de maior TVL, e o tile diz qual foi. Media de preco entre pools de liquidez
diferente e um numero que nao existe em lugar nenhum — ninguem negocia nele —,
e media ponderada esconderia que a pool rasa pode estar deslocada. Com a pool
nomeada, o numero e rastreavel ate uma linha de `pool_hora`.

---

**A variacao e contra o ponto mais proximo de 24h atras, e o vao vai junto.**
A serie e horaria e pode ter buraco; se o ponto mais antigo disponivel tiver
menos de `JANELA_MIN_H` de idade, a variacao sai `None` — nunca zero, que se
leria como "o token nao andou". Tile sem variacao sai neutro e DIZ o motivo,
a mesma regra que a `/compor/` aplica ao `dentro %`.

---

**A escala de cor tem teto derivado e ESCRITO na legenda.** Um dia em que um
token anda 40% e o resto 0,5% pintaria tudo de cinza-esverdeado se a escala
fosse o maximo. O teto e o p90 das magnitudes, com piso de `COR_TETO_MIN` —
mesma familia da escala de p95 do grafico de APR, e pelo mesmo motivo: um
espeto nao decide o desenho, e o corte nao pode ser silencioso.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

# Quanto a serie precisa cobrir para a variacao de 24h ser publicavel. Abaixo
# disso a variacao mede um pedaco do dia e seria rotulada como se medisse o
# dia — a familia do "4,8 dias" e do APR de 1,6 hora.
JANELA_MIN_H = 18.0

# Teto minimo da escala de cor, em pontos percentuais. Sem ele, um dia calmo
# (todos abaixo de 0,3%) sairia com cores saturadas sugerindo movimento.
COR_TETO_MIN = 2.0


@dataclass
class Token:
    """Um ativo, com as pools dele ja somadas.

    `fonte` diz de onde vieram preco e variacao: `pool` (o token na chain,
    24/7) ou `bolsa` (a acao no pregao, via Yahoo). **Os dois nunca aparecem
    na mesma visao** — o basis entre eles tem desvio de 1,42% e chega a 3%,
    entao um mapa com tiles de fontes misturadas coloriria lado a lado dois
    numeros que nao sao comparaveis. Quem separa e o job, montando uma visao
    por fonte; este campo existe para o tile poder DIZER qual e a dele.
    """
    simbolo: str
    tvl_usd: float
    volume_24h_usd: float
    preco: float | None
    variacao_24h_pct: float | None
    pool_dominante: str
    pools: int
    horas_cobertas: float
    motivo_sem_variacao: str | None = None
    fonte: str = "pool"
    janela: str = "24 h"
    # De onde veio o PRECO: "contrato" (estado do bloco corrente) ou
    # "pool_hora" (a serie horaria, ate 3 h atras). Sao frescores diferentes
    # e o tile nao pode afirmar o primeiro quando tem o segundo.
    # **Idade do PRECO, em minutos, quando ela nao e obvia.** Na aba das
    # pools o preco vem do estado do contrato no bloco corrente e nao ha o que
    # declarar; na bolsa ele pode ser a barra de 1m de um minuto atras ou o
    # fechamento de sexta, e a diferenca entre as duas e a pagina inteira.
    preco_idade_min: float | None = None
    preco_fonte: str = "pool_hora"
    tvl_idade_min: float | None = None
    # preenchidos pelo layout
    x: float = 0.0
    y: float = 0.0
    largura: float = 0.0
    altura: float = 0.0
    extras: dict = field(default_factory=dict)


def agregar(por_pool: dict[str, dict]) -> list[Token]:
    """Junta as pools de cada token num tile so.

    `por_pool` e `{endereco: {simbolo, tvl_usd, volume_24h_usd, preco,
    variacao_24h_pct, horas_cobertas}}`. Pool sem simbolo resolvido fica de
    fora: um tile chamado `?` ocuparia area num heatmap que existe para ser
    lido de relance, e nomear por endereco nao ajudaria ninguem.
    """
    grupos: dict[str, list[tuple[str, dict]]] = {}
    for end, d in por_pool.items():
        s = (d.get("simbolo") or "").strip()
        if not s or s == "?":
            continue
        grupos.setdefault(s, []).append((end, d))

    saida = []
    for simbolo, itens in grupos.items():
        # A dominante e por TVL, e e dela que saem preco e variacao.
        dom_end, dom = max(itens, key=lambda kv: kv[1].get("tvl_usd") or 0)
        tvl = sum((d.get("tvl_usd") or 0) for _, d in itens)
        vol = sum((d.get("volume_24h_usd") or 0) for _, d in itens)
        horas = dom.get("horas_cobertas") or 0.0
        fonte = dom.get("fonte") or "pool"
        var = dom.get("variacao_24h_pct")
        motivo = None
        if var is None:
            motivo = dom.get("motivo") or "sem preco em duas pontas da janela"
        elif fonte == "pool" and horas < JANELA_MIN_H:
            # **A exigencia de 18 h vale so para a POOL, que negocia 24/7.**
            # Na bolsa um pregao inteiro tem 6h30 e a variacao diaria e
            # fechamento contra fechamento — aplicar o mesmo limiar la
            # descartaria TODA acao, medindo o calendario do mercado em vez
            # da cobertura da serie. Cada fonte tem a janela que ela pode ter,
            # e o tile publica qual e.
            var, motivo = None, (f"serie cobre {horas:.1f} h, "
                                 f"minimo {JANELA_MIN_H:.0f} h")
        saida.append(Token(
            simbolo=simbolo, tvl_usd=tvl, volume_24h_usd=vol,
            preco=dom.get("preco"), variacao_24h_pct=var,
            pool_dominante=dom_end, pools=len(itens),
            horas_cobertas=horas, motivo_sem_variacao=motivo,
            fonte=dom.get("fonte") or "pool",
            janela=dom.get("janela") or "24 h",
            preco_fonte=dom.get("preco_fonte") or "pool_hora",
            preco_idade_min=dom.get("preco_idade_min"),
            tvl_idade_min=dom.get("tvl_idade_min")))
    return saida


def teto_de_cor(tokens, minimo: float = COR_TETO_MIN) -> float:
    """p90 das magnitudes de variacao, com piso. Vai escrito na legenda."""
    mags = sorted(abs(t.variacao_24h_pct) for t in tokens
                  if t.variacao_24h_pct is not None)
    if not mags:
        return minimo
    p90 = mags[min(len(mags) - 1, int(0.9 * (len(mags) - 1)))]
    return max(minimo, p90)


def intensidade(variacao: float | None, teto: float) -> float:
    """0..1, saturando no teto. Acima dele a cor nao continua escurecendo —
    e por isso o teto vai publicado: sem ele, dois tokens muito diferentes
    sairiam com a mesma cor sem ninguem saber."""
    if variacao is None or teto <= 0:
        return 0.0
    return min(1.0, abs(variacao) / teto)


# ----------------------------------------------------------------- treemap

def squarify(valores: list[float], x: float, y: float,
             largura: float, altura: float) -> list[tuple]:
    """Treemap "squarified" (Bruls, Huizing, van Wijk, 2000).

    Devolve `[(x, y, largura, altura)]` na MESMA ordem da entrada. A entrada
    deve vir ordenada do maior para o menor — o algoritmo depende disso para
    produzir retangulos proximos do quadrado.

    **Por que nao fatiar em linhas iguais:** com valores muito desiguais (aqui
    o maior tem 24x o menor) o corte ingenuo produz tiras de 1 pixel de altura
    onde nao cabe nem o simbolo. O squarified escolhe, a cada passo, se vale
    acrescentar mais um item a linha corrente comparando a PIOR razao de
    aspecto antes e depois.

    Valor zero ou negativo nao vira retangulo de area zero perdido no canto:
    ele e descartado por quem chama, porque tile invisivel e pior que tile
    ausente — o leitor nao sabe que ele existe.
    """
    if not valores or largura <= 0 or altura <= 0:
        return [(x, y, 0.0, 0.0) for _ in valores]

    total = sum(valores)
    if total <= 0:
        return [(x, y, 0.0, 0.0) for _ in valores]

    # Escala os valores para a area disponivel: a soma das areas e a area do
    # container, por construcao, e ha teste exigindo isso.
    escala = (largura * altura) / total
    areas = [v * escala for v in valores]

    saida: list[tuple] = []
    cx, cy, cl, ca = x, y, largura, altura
    i = 0
    while i < len(areas):
        curta = min(cl, ca)
        linha = [areas[i]]
        j = i + 1
        while j < len(areas) and _pior(linha + [areas[j]], curta) <= _pior(
                linha, curta):
            linha.append(areas[j])
            j += 1
        soma = sum(linha)
        if curta == ca:                       # linha VERTICAL, cresce em x
            passo = soma / ca if ca else 0.0
            oy = cy
            for a in linha:
                h = (a / passo) if passo else 0.0
                saida.append((cx, oy, passo, h))
                oy += h
            cx += passo
            cl -= passo
        else:                                 # linha HORIZONTAL, cresce em y
            passo = soma / cl if cl else 0.0
            ox = cx
            for a in linha:
                w = (a / passo) if passo else 0.0
                saida.append((ox, cy, w, passo))
                ox += w
            cy += passo
            ca -= passo
        i = j
    return saida


def _pior(linha: list[float], curta: float) -> float:
    """A pior razao de aspecto da linha. E o criterio de parada do
    squarified: enquanto acrescentar um item MELHORA a pior razao, a linha
    continua."""
    if not linha or curta <= 0:
        return math.inf
    s = sum(linha)
    if s <= 0:
        return math.inf
    mx, mn = max(linha), min(linha)
    return max((curta * curta * mx) / (s * s), (s * s) / (curta * curta * mn))


def montar(tokens: list[Token], por: str = "tvl",
           largura: float = 100.0, altura: float = 62.0) -> dict:
    """Ordena, calcula o layout e devolve os tiles prontos para desenhar.

    `por` escolhe o que a AREA representa — e sao duas perguntas diferentes,
    por isso a pagina alterna em vez de escolher por nos:

      `tvl`     quanto capital esta parado ali (o analogo de market cap)
      `volume`  quanto passou nas ultimas 24 h (o analogo de volume)

    Uma pool grande e parada e uma pequena e agitada trocam de lugar entre as
    duas visoes, e essa troca e informacao.

      `igual`   todos os tiles do mesmo tamanho — a area deixa de significar
                alguma coisa e sobra a COR

    **`igual` existe por medicao, nao por estetica.** Na bolsa o volume
    financeiro do dia varia **623x** entre a maior e a menor das acoes
    acompanhadas (MU $28,5 bi contra QUBT $45,7 mi): num treemap proporcional
    a QUBT ficaria com 1/623 da area da MU e sumiria. Uma grade uniforme diz
    honestamente que ali o tamanho nao codifica nada, e o volume vai no
    `title` — melhor que um tile invisivel que existe e nao se ve.
    """
    if por not in ("tvl", "volume", "igual"):
        raise ValueError(f"por={por!r}: use 'tvl', 'volume' ou 'igual'")
    campo = {"tvl": "tvl_usd", "volume": "volume_24h_usd",
             "igual": "volume_24h_usd"}[por]

    vivos = [t for t in tokens if (getattr(t, campo) or 0) > 0]
    vivos.sort(key=lambda t: -(getattr(t, campo) or 0))
    pesos = ([1.0] * len(vivos) if por == "igual"
             else [getattr(t, campo) for t in vivos])
    caixas = squarify(pesos, 0, 0, largura, altura)
    for t, (bx, by, bl, ba) in zip(vivos, caixas):
        t.x, t.y, t.largura, t.altura = bx, by, bl, ba

    # Token sem o campo escolhido sai da figura e e CONTADO, nunca sumido em
    # silencio: numa visao por volume uma pool sem negocio no dia desaparece,
    # e "desapareceu" nao pode ser indistinguivel de "nao existe".
    fora = [t.simbolo for t in tokens if (getattr(t, campo) or 0) <= 0]
    teto = teto_de_cor(vivos)
    return {
        "por": por, "tiles": vivos, "fora": sorted(fora),
        "teto_cor_pct": teto,
        "total_usd": sum(getattr(t, campo) for t in vivos),
        "sem_variacao": sorted(t.simbolo for t in vivos
                               if t.variacao_24h_pct is None),
    }


__all__ = ["Token", "agregar", "montar", "squarify", "teto_de_cor",
           "intensidade", "JANELA_MIN_H", "COR_TETO_MIN"]
