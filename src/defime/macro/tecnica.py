"""Indicadores para dimensionar FAIXA — nao para adivinhar direcao.

Analise tecnica classica existe para responder "vai subir ou cair". Um LP de
faixa nao precisa dessa resposta: ele precisa saber **quanto o preco anda** e
**se ele volta**. Sao perguntas diferentes, e a maior parte dos indicadores
famosos nao responde nenhuma das duas.

Os que ficaram aqui responderam a um teste: existe uma decisao de faixa que
muda quando o numero muda? Os que nao passaram ficaram de fora com o motivo —
ver `POR_QUE_NAO` no fim do arquivo.

---

**O indicador mais importante deste modulo e o menos famoso: a EFICIENCIA.**

    ER = |P_fim - P_inicio| / soma(|variacao de cada barra|)

Perto de **zero**, o preco andou muito e voltou: a faixa arrecadou fee o tempo
todo e a divergencia se desfez. Perto de **um**, ele foi em linha reta: mesma
fee, e a divergencia ficou realizada contra voce. **Dois ativos com o MESMO
sigma dao resultados opostos para uma LP dependendo desta razao**, e nenhuma
medida de volatilidade a captura — sigma nao distingue ir-e-voltar de ir.

Foi por isso que a MU rendeu mais que a USO com volatilidade parecida.

---

**Tudo aqui e puro:** recebe barras, devolve numeros. Sem rede, sem banco.
"""

from __future__ import annotations

import math
import statistics as st
from dataclasses import dataclass

# Um ano de pregao em barras de 1h: 6h30 x 252 dias, arredondado para as ~6,5
# barras uteis que o provedor entrega por dia.
BARRAS_POR_ANO_1H = 1638


def _fech(barras) -> list[float]:
    return [b.fechamento for b in barras]


def retornos(barras, passo: int = 1, max_intervalo_s: int | None = None
             ) -> list[float]:
    """Log-retornos entre barras separadas por `passo`.

    **Barras nao contiguas sao descartadas.** Sem isso, o retorno que atravessa
    a noite ou o fim de semana entra na mesma amostra que o de uma hora, e o
    sigma sai inflado por juntar horizontes diferentes. E o mesmo cuidado que
    o bootstrap em blocos deste projeto teve que aprender: amostrar so onde
    cabe um bloco contiguo inteiro.
    """
    lim = max_intervalo_s if max_intervalo_s is not None else (passo + 2) * 3600
    saida = []
    for i in range(passo, len(barras)):
        if barras[i].ts - barras[i - passo].ts <= lim:
            saida.append(math.log(barras[i].fechamento
                                  / barras[i - passo].fechamento))
    return saida


def sigma_anualizado(barras, passo: int = 1) -> float | None:
    r = retornos(barras, passo)
    if len(r) < 30:
        return None
    return st.stdev(r) * math.sqrt(BARRAS_POR_ANO_1H / passo)


def eficiencia(barras) -> float | None:
    """Kaufman Efficiency Ratio. **A metrica mais importante para uma LP.**

    0 = vai e volta (a faixa cobra pedagio e o preco devolve o que levou)
    1 = linha reta  (mesma fee, divergencia realizada contra voce)

    Nao ha equivalente em volatilidade: dois ativos com sigma identico podem
    ter ER de 0,05 e 0,95, e o resultado da LP e oposto nos dois.
    """
    if len(barras) < 3:
        return None
    f = _fech(barras)
    caminho = sum(abs(f[i] - f[i - 1]) for i in range(1, len(f)))
    if caminho <= 0:
        return None
    return abs(f[-1] - f[0]) / caminho



def eficiencia_em_janelas(barras, passo: int, salto: int = 7) -> dict | None:
    """Eficiencia medida no HORIZONTE DA FAIXA, nao na amostra inteira.

    **Este e o numero que vale, e a distincao custou uma medicao errada.** O ER
    da serie completa e ~0 em qualquer ativo com anos de historico: o caminho
    percorrido em tres anos e enorme contra o deslocamento liquido, e todos
    empatam perto de zero. Medido assim, MU deu 0,068 e USO 0,023 — numeros
    reais e inuteis, porque nenhuma faixa vive tres anos.

    O que decide o resultado de uma posicao de sete dias e quanto do movimento
    DAQUELES sete dias foi liquido. Aqui isso sai por janela deslizante, e a
    distribuicao importa mais que a media: a mediana diz a semana tipica, e o
    p90 diz a semana que machuca.
    """
    if len(barras) < passo + 1:
        return None
    ers = []
    for i in range(0, len(barras) - passo, salto):
        e = eficiencia(barras[i:i + passo])
        if e is not None:
            ers.append(e)
    if len(ers) < 20:
        return None
    o = sorted(ers)
    n = len(o)
    return {"n": n, "mediana": st.median(ers), "media": st.mean(ers),
            "p10": o[int(0.10 * (n - 1))], "p25": o[int(0.25 * (n - 1))],
            "p75": o[int(0.75 * (n - 1))], "p90": o[int(0.90 * (n - 1))],
            "acima_de_0_5": sum(1 for x in ers if x > 0.5) / n}


def atr(barras, janela: int = 14) -> float | None:
    """Average True Range — a variacao MEDIA de uma barra, em dolar.

    True range inclui o gap contra o fechamento anterior, e nao so
    `maxima-minima`. Numa acao isso importa: 19,6% dos dias da MU abrem com
    salto acima de 3%, e um range que ignora o gap subestima o movimento
    exatamente nos dias em que ele decide.
    """
    if len(barras) < janela + 1:
        return None
    trs = []
    for i in range(1, len(barras)):
        b, ant = barras[i], barras[i - 1]
        trs.append(max(b.maxima - b.minima,
                       abs(b.maxima - ant.fechamento),
                       abs(b.minima - ant.fechamento)))
    return st.mean(trs[-janela:])


def largura_bollinger(barras, janela: int = 20, k: float = 2.0
                      ) -> float | None:
    """Largura das bandas em % do preco. Mede COMPRESSAO, nao nivel.

    Serve para uma coisa so aqui: banda estreita e regime comprimido, e
    compressao costuma preceder expansao. Abrir faixa estreita durante um
    aperto e o pior momento possivel.
    """
    f = _fech(barras)
    if len(f) < janela:
        return None
    jan = f[-janela:]
    m = st.mean(jan)
    if m <= 0 or len(jan) < 2:
        return None
    return 100 * 2 * k * st.stdev(jan) / m


def percentil_do_preco(barras, janela: int = 500) -> float | None:
    """Onde o preco de agora esta dentro da faixa dos ultimos `janela`.

    0 = na minima do periodo, 1 = na maxima. Nao e sinal de direcao: e
    assimetria de espaco. Abrir faixa centrada com o preco em 0,95 deixa 5%
    de espaco para cima e 95% para baixo, contra a historia recente.
    """
    if len(barras) < janela // 2:
        return None
    jan = barras[-janela:]
    lo = min(b.minima for b in jan)
    hi = max(b.maxima for b in jan)
    if hi <= lo:
        return None
    return (barras[-1].fechamento - lo) / (hi - lo)


def autocorrelacao(barras, defasagem: int = 1) -> float | None:
    """Correlacao do retorno com o retorno anterior.

    Negativa = reversao a media: o que sobe tende a devolver, e a faixa
    estreita e recompensada. Positiva = momentum, e ela e punida. Perto de
    zero (o caso normal) e passeio aleatorio, que e o que o modelo ja supoe.
    """
    r = retornos(barras)
    if len(r) < 60:
        return None
    a, b = r[:-defasagem], r[defasagem:]
    ma, mb = st.mean(a), st.mean(b)
    num = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    den = math.sqrt(sum((x - ma) ** 2 for x in a)
                    * sum((y - mb) ** 2 for y in b))
    return num / den if den else None


def maior_excursao(barras, passo: int) -> dict | None:
    """Quanto o preco anda SEM VOLTAR, em janelas de `passo` barras.

    Sigma mede dispersao; isto mede deslocamento maximo — que e o que estoura
    uma borda. Uma serie pode ter sigma baixo e uma excursao brutal, e e a
    excursao que decide se a faixa sobrevive.
    """
    if len(barras) < passo + 1:
        return None
    subidas, quedas = [], []
    for i in range(0, len(barras) - passo, max(1, passo // 4)):
        jan = barras[i:i + passo]
        p0 = jan[0].fechamento
        subidas.append(100 * (max(b.maxima for b in jan) / p0 - 1))
        quedas.append(100 * (min(b.minima for b in jan) / p0 - 1))
    if not subidas:
        return None
    return {"n": len(subidas),
            "subida_mediana": st.median(subidas),
            "subida_p95": sorted(subidas)[int(0.95 * (len(subidas) - 1))],
            "queda_mediana": st.median(quedas),
            "queda_p5": sorted(quedas)[int(0.05 * (len(quedas) - 1))]}


def sobrevivencia(barras, largura_pct: float, passo: int,
                  salto: int = 7) -> dict | None:
    """Fracao das janelas em que o preco NUNCA tocou a borda.

    Nao e modelo: e contagem do que aconteceu. A faixa nasce centrada no
    fechamento do inicio de cada janela e morre se qualquer maxima ou minima
    encostar. Usar fechamentos em vez de maxima/minima daria um numero
    otimista e errado — a posicao sai de range no toque, nao no fechamento.
    """
    if len(barras) < passo + 1:
        return None
    ok = tot = 0
    for i in range(0, len(barras) - passo, salto):
        p0 = barras[i].fechamento
        lo, hi = p0 * (1 - largura_pct / 200), p0 * (1 + largura_pct / 200)
        jan = barras[i:i + passo]
        ok += all(b.minima >= lo and b.maxima <= hi for b in jan)
        tot += 1
    return {"fracao": ok / tot, "n": tot} if tot else None


def gaps(barras) -> dict | None:
    """Distribuicao do salto entre o fechamento e a abertura seguinte.

    Existe porque a USO NAO tem gap (chain 24/7, overnight negociavel) e a
    acao tem. Medido na MU: desvio 2,84%, e 19,6% dos dias abrem com salto
    acima de 3%. Uma faixa estreita morre dormindo.
    """
    import datetime as dt
    por_dia: dict = {}
    for b in barras:
        por_dia.setdefault(
            dt.datetime.fromtimestamp(b.ts, dt.UTC).date(), []).append(b)
    dias = sorted(por_dia)
    g = [100 * (por_dia[dias[i]][0].abertura
                / por_dia[dias[i - 1]][-1].fechamento - 1)
         for i in range(1, len(dias))]
    if len(g) < 20:
        return None
    o = sorted(g)
    n = len(o)
    return {"n": n, "media": st.mean(g), "desvio": st.stdev(g),
            "p1": o[int(0.01 * (n - 1))], "p5": o[int(0.05 * (n - 1))],
            "p95": o[int(0.95 * (n - 1))], "p99": o[int(0.99 * (n - 1))],
            "acima_de_3pct": sum(1 for x in g if abs(x) > 3) / n,
            "acima_de_5pct": sum(1 for x in g if abs(x) > 5) / n}



# --------------------------------------------------------------- direcao
def tendencia(barras, janela: int | None = None) -> dict | None:
    """Inclinacao da reta em LOG-preco, com o R2 que diz se ela vale algo.

    Em log porque a faixa da V3 vive em ticks, que sao log-preco: uma
    inclinacao em dolar nao converte em ticks sem depender do nivel.

    **O R2 e mais importante que a inclinacao.** Toda serie tem uma reta de
    melhor ajuste; a pergunta e se os pontos estao perto dela. Com R2 de 0,1 a
    inclinacao existe no papel e nao descreve o preco.
    """
    b = barras[-janela:] if janela else barras
    if len(b) < 30:
        return None
    n = len(b)
    xs = list(range(n))
    ys = [math.log(x.fechamento) for x in b]
    mx, my = st.mean(xs), st.mean(ys)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 0:
        return None
    a = sxy / sxx
    prev = [my + a * (x - mx) for x in xs]
    ss_res = sum((y - p) ** 2 for y, p in zip(ys, prev))
    ss_tot = sum((y - my) ** 2 for y in ys)
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0.0
    return {"por_barra_pct": 100 * a, "por_dia_pct": 100 * a * 7,
            "por_semana_pct": 100 * a * 35, "r2": r2, "n": n}


def poder_da_tendencia(barras, olhar: int, prever: int,
                       salto: int = 7) -> dict | None:
    """A inclinacao passada PREVE o retorno seguinte? Testado, nao suposto.

    Correlaciona a inclinacao dos ultimos `olhar` com o retorno dos proximos
    `prever`, em janelas deslizantes. **Este e o teste que autoriza deslocar o
    centro da faixa.** Sem ele, deslocar e apostar — e apostar errado custa a
    divergencia inteira de um lado so.

    Correlacao perto de zero significa que a tendencia recente nao diz nada
    sobre a proxima semana, e a faixa deve ser CENTRADA.
    """
    if len(barras) < olhar + prever + 10:
        return None
    incs, futs = [], []
    for i in range(olhar, len(barras) - prever, salto):
        t = tendencia(barras[i - olhar:i])
        if not t:
            continue
        incs.append(t["por_barra_pct"])
        futs.append(100 * (barras[i + prever].fechamento
                           / barras[i].fechamento - 1))
    if len(incs) < 40:
        return None
    mi, mf = st.mean(incs), st.mean(futs)
    num = sum((a - mi) * (b - mf) for a, b in zip(incs, futs))
    den = math.sqrt(sum((a - mi) ** 2 for a in incs)
                    * sum((b - mf) ** 2 for b in futs))
    corr = num / den if den else 0.0
    # Sinal acertado: a inclinacao apontou para o lado que o preco foi.
    acertos = sum(1 for a, b in zip(incs, futs) if a * b > 0)
    return {"n": len(incs), "correlacao": corr, "r2": corr ** 2,
            "acerto_de_sinal": acertos / len(incs),
            "olhar_barras": olhar, "prever_barras": prever}


def centro_sugerido(barras, largura_pct: float, poder: dict | None,
                    limiar_acerto: float = 0.58) -> dict:
    """Onde por o MEIO da faixa. Centrado, a menos que a direcao se prove.

    O deslocamento so acontece se `poder_da_tendencia` mostrar acerto de sinal
    acima de `limiar_acerto` — 58% e o ponto em que, com algumas centenas de
    janelas, o resultado deixa de ser compativel com cara-ou-coroa. Abaixo
    disso a faixa e centrada e o motivo vai junto.

    E mesmo quando desloca, desloca **um quarto** da largura, nao a largura
    inteira: a evidencia e fraca por construcao (nenhum sinal de preco a 1h e
    forte), e um centro muito deslocado troca um risco por outro maior.
    """
    p = barras[-1].fechamento
    meia = largura_pct / 200
    if not poder or poder["acerto_de_sinal"] < limiar_acerto:
        return {"centro": p, "deslocamento_pct": 0.0, "centrada": True,
                "motivo": (f"acerto de sinal "
                           f"{100 * poder['acerto_de_sinal']:.1f}% < "
                           f"{100 * limiar_acerto:.0f}%: direcao sem poder"
                           if poder else "sem amostra para testar direcao"),
                "piso": p * (1 - meia), "teto": p * (1 + meia)}
    t = tendencia(barras, janela=poder["olhar_barras"])
    desl = 0.0
    if t:
        desl = math.copysign(min(abs(t["por_semana_pct"]), largura_pct / 4),
                             t["por_barra_pct"])
    centro = p * (1 + desl / 100)
    return {"centro": centro, "deslocamento_pct": desl, "centrada": False,
            "motivo": (f"acerto de sinal "
                       f"{100 * poder['acerto_de_sinal']:.1f}%, deslocando "
                       f"{desl:+.2f}% (teto de 1/4 da largura)"),
            "piso": centro * (1 - meia), "teto": centro * (1 + meia)}


@dataclass
class Perfil:
    """Tudo que decide largura de faixa, num objeto."""

    simbolo: str
    n_barras: int
    preco: float
    sigma_1h: float | None
    sigma_recente: float | None       # ultimo quarto da amostra
    eficiencia: float | None          # amostra inteira; ~0 sempre, pouco util
    eficiencia_recente: float | None
    eficiencia_janela: dict | None    # NO HORIZONTE DA FAIXA — e este que vale
    atr_pct: float | None
    bollinger_pct: float | None
    percentil: float | None
    autocorr: float | None
    excursao_7d: dict | None
    gaps: dict | None
    tendencia: dict | None
    tendencia_recente: dict | None
    poder_direcional: dict | None
    sobrevivencia_7d: dict            # largura -> fracao

    def resumo(self) -> dict:
        return {k: (round(v, 4) if isinstance(v, float) else v)
                for k, v in self.__dict__.items()}


LARGURAS_PADRAO = (5.0, 8.0, 11.0, 15.0, 20.0, 25.0, 30.0, 35.0, 50.0)


def perfilar(simbolo: str, barras, larguras=LARGURAS_PADRAO,
             dias_horizonte: int = 7, barras_por_dia: int = 7) -> Perfil:
    """O perfil completo de um ativo, do ponto de vista de quem abre faixa."""
    passo = dias_horizonte * barras_por_dia
    quarto = max(len(barras) // 4, 60)
    rec = barras[-quarto:]
    atr_v = atr(barras)
    return Perfil(
        simbolo=simbolo, n_barras=len(barras),
        preco=barras[-1].fechamento if barras else 0.0,
        sigma_1h=sigma_anualizado(barras),
        sigma_recente=sigma_anualizado(rec),
        eficiencia=eficiencia(barras), eficiencia_recente=eficiencia(rec),
        eficiencia_janela=eficiencia_em_janelas(barras, passo),
        atr_pct=(100 * atr_v / barras[-1].fechamento) if atr_v else None,
        bollinger_pct=largura_bollinger(barras),
        percentil=percentil_do_preco(barras),
        autocorr=autocorrelacao(barras),
        excursao_7d=maior_excursao(barras, passo),
        gaps=gaps(barras),
        tendencia=tendencia(barras),
        tendencia_recente=tendencia(barras, janela=quarto),
        poder_direcional=poder_da_tendencia(barras, olhar=passo,
                                            prever=passo),
        sobrevivencia_7d={l: (sobrevivencia(barras, l, passo) or {}).get(
            "fracao") for l in larguras})


# ---------------------------------------------------------------------------
# O que NAO entrou, e por que. Documentado para nao ser reinventado.
POR_QUE_NAO = {
    "medias moveis / cruzamentos":
        "a direcao entra por `tendencia()` + `poder_da_tendencia()`, que "
        "medem a mesma intuicao E testam se ela preve alguma coisa. "
        "Cruzamento de medias afirma sem testar, e o teste e o que decide se "
        "vale deslocar o centro da faixa.",
    "RSI / estocastico":
        "sobrecompra e sobrevenda sao afirmacoes sobre o proximo movimento. "
        "O que muda a faixa e a autocorrelacao, que mede a mesma intuicao sem "
        "o limiar arbitrario de 70/30.",
    "MACD":
        "derivada de medias moveis: mesma objecao, com mais parametros.",
    "fibonacci / ondas":
        "os niveis nao sobrevivem a um teste fora da amostra, e aqui existe "
        "sobrevivencia empirica — que responde a mesma pergunta contando.",
    "volume profile / VWAP":
        "util para execucao de ordem grande, nao para escolher borda. E o "
        "volume da BOLSA nao e o volume da pool, que e o que paga a fee.",
    "suporte e resistencia desenhados":
        "nao sao reproduziveis e, pior, sao da BOLSA. A faixa vive no preco "
        "da POOL, e o basis entre os dois tem desvio de 1,42% — um nivel "
        "traçado la chega aqui deslocado.",
}

__all__ = ["Perfil", "perfilar", "tendencia", "eficiencia_em_janelas",
           "poder_da_tendencia", "centro_sugerido", "sigma_anualizado", "eficiencia", "atr",
           "largura_bollinger", "percentil_do_preco", "autocorrelacao",
           "maior_excursao", "sobrevivencia", "gaps", "retornos",
           "LARGURAS_PADRAO", "POR_QUE_NAO", "BARRAS_POR_ANO_1H"]
