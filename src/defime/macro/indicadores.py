"""Indicadores classicos de grafico — RSI, Stoch RSI, MACD, medias, canais.

**Este modulo e LEITURA, nao decisao**, e a distincao esta registrada no
projeto desde que `macro/tecnica.py` nasceu: RSI, MACD e cruzamento de medias
estao no `POR_QUE_NAO` de la, reprovados para DIMENSIONAR FAIXA. O motivo
continua valendo — sobrecompra e uma afirmacao sobre o proximo movimento, e o
que muda a borda e a autocorrelacao, que mede a mesma intuicao sem o limiar
arbitrario de 70/30.

Reprovado para decidir nao e o mesmo que inutil para olhar. O que este modulo
NAO pode virar e gatilho: nenhum numero daqui entra em `decision/`, e ha teste
garantindo isso. Quem decide faixa continua sendo `tecnica.perfilar`.

---

**Duas convencoes que evitam o erro classico de indicador, e as duas tem
teste:**

1. **Alinhamento por construcao.** Toda funcao devolve uma lista do MESMO
   comprimento da entrada, com `None` no aquecimento. A alternativa — devolver
   so os pontos validos — obriga quem chama a acertar o deslocamento, e errar
   por um indice desenha o indicador de ontem sobre o candle de hoje. E a
   familia numero um deste projeto ("comparar dois instantes diferentes"), que
   aqui apareceria calada: o desenho continua bonito.

2. **Serie curta nao levanta.** Devolve tudo `None`, porque indicador sem
   aquecimento e ausencia de medicao e nao zero — a mesma regra que faz o
   painel publicar "—" em vez de `0,00`.

---

**A descontinuidade da bolsa entra aqui de proposito, e isso CONTRADIZ
`tecnica.retornos()`.** La as barras nao contiguas sao descartadas, senao o
retorno que atravessa a noite entra na mesma amostra do retorno de uma hora e
o sigma sai inflado. Aqui nao se descarta nada: a sequencia de barras de
pregao E a serie, que e o que o TradingView desenha e o que o usuário está
acostumado a ler.

Sao duas perguntas diferentes e cada uma tem a sua convencao. Quem for medir
volatilidade usa `tecnica.retornos()`; quem for desenhar RSI usa isto. Juntar
as duas faria um RSI que pula barras ou um sigma que soma horizontes.

**Consequencia no eixo X:** o grafico indexa por NUMERO DE BARRA e nao por
tempo. Com tempo real, as 17h de mercado fechado abririam um vao maior que o
pregao inteiro e a serie viraria uma fileira de tracinhos. E tambem o que o
TradingView faz.

---

**Escala 0..100 no RSI e no Stoch RSI**, nao 0..1, porque e a escala em que os
limiares de 70/30 e 80/20 sao lidos. O resto do projeto usa fracao; aqui a
convencao e a da ferramenta que se esta imitando, e o nome do campo diz isso.
"""

from __future__ import annotations

import math
import statistics as st

# Parametros classicos. Ficam nomeados porque o grafico os publica na legenda:
# um indicador sem periodo declarado e um numero que ninguem consegue conferir.
RSI_JANELA = 14
STOCH_JANELA = 14
STOCH_K = 3
STOCH_D = 3
MACD_RAPIDA, MACD_LENTA, MACD_SINAL = 12, 26, 9
BOLL_JANELA, BOLL_K = 20, 2.0
DONCHIAN_JANELA = 20
ATR_JANELA = 14

# Gaussian Channel (Ehlers). `polos` e a ordem do filtro; `mult` multiplica a
# banda de True Range. Os defaults sao os do indicador popular no TradingView.
GC_POLOS, GC_PERIODO, GC_MULT = 4, 144, 1.414

Num = float | None


def _vazio(n: int) -> list[Num]:
    return [None] * n


# ------------------------------------------------------------------ medias

def sma(v: list[float], janela: int) -> list[Num]:
    """Media simples. `None` ate haver `janela` pontos."""
    if janela < 1 or len(v) < janela:
        return _vazio(len(v))
    saida: list[Num] = _vazio(janela - 1)
    soma = sum(v[:janela])
    saida.append(soma / janela)
    for i in range(janela, len(v)):
        soma += v[i] - v[i - janela]
        saida.append(soma / janela)
    return saida


def ema(v: list[float], janela: int) -> list[Num]:
    """Media exponencial, semeada com a SMA das primeiras `janela` barras.

    **A semente importa e por isso e explicita.** Comecar o EMA no primeiro
    ponto (`ema[0] = v[0]`) faz o inicio da serie carregar o peso de um unico
    candle por dezenas de barras — o valor converge, mas o comeco do desenho
    mente. Semear com a SMA e a convencao das plataformas e o que faz o
    MACD daqui bater com o de la.
    """
    if janela < 1 or len(v) < janela:
        return _vazio(len(v))
    k = 2.0 / (janela + 1)
    saida: list[Num] = _vazio(janela - 1)
    atual = sum(v[:janela]) / janela
    saida.append(atual)
    for i in range(janela, len(v)):
        atual = v[i] * k + atual * (1 - k)
        saida.append(atual)
    return saida


def _wilder(v: list[float], janela: int) -> list[Num]:
    """Suavizacao de Wilder: usada pelo RSI e pelo ATR, nunca pelo MACD.

    E um EMA de fator `1/n` em vez de `2/(n+1)`. Trocar um pelo outro muda o
    RSI em varios pontos e e um erro que nao levanta nada.
    """
    if janela < 1 or len(v) < janela:
        return _vazio(len(v))
    saida: list[Num] = _vazio(janela - 1)
    atual = sum(v[:janela]) / janela
    saida.append(atual)
    for i in range(janela, len(v)):
        atual = (atual * (janela - 1) + v[i]) / janela
        saida.append(atual)
    return saida


# --------------------------------------------------------------------- RSI

def rsi(fech: list[float], janela: int = RSI_JANELA) -> list[Num]:
    """RSI de Wilder, em 0..100.

    Perda media zero devolve **100** e nao divisao por zero: a serie so subiu
    na janela, o que e medicao e nao erro.
    """
    n = len(fech)
    if janela < 1 or n < janela + 1:
        return _vazio(n)
    ganhos, perdas = [], []
    for i in range(1, n):
        dd = fech[i] - fech[i - 1]
        ganhos.append(max(dd, 0.0))
        perdas.append(max(-dd, 0.0))
    mg, mp = _wilder(ganhos, janela), _wilder(perdas, janela)
    # `ganhos` comeca no indice 1 da serie original: o deslocamento entra aqui,
    # uma vez, e nao no chamador.
    saida: list[Num] = [None]
    for g, p in zip(mg, mp):
        if g is None or p is None:
            saida.append(None)
        elif p == 0:
            saida.append(100.0)
        else:
            saida.append(100.0 - 100.0 / (1.0 + g / p))
    return saida


def _sma_com_buracos(v: list[Num], janela: int) -> list[Num]:
    """SMA que exige a janela CHEIA de valores nao nulos.

    Suavizar por cima de `None` — pulando-os ou tratando-os como zero —
    produziria um ponto que parece medido e foi montado com menos amostra do
    que o nome diz.
    """
    saida: list[Num] = _vazio(len(v))
    for i in range(janela - 1, len(v)):
        jan = v[i - janela + 1:i + 1]
        if all(x is not None for x in jan):
            saida[i] = sum(jan) / janela            # type: ignore[arg-type]
    return saida


def stoch_rsi(fech: list[float], janela: int = RSI_JANELA,
              estoc: int = STOCH_JANELA, k: int = STOCH_K,
              d: int = STOCH_D) -> dict[str, list[Num]]:
    """Stoch RSI: o estocastico aplicado ao RSI, nao ao preco.

    Devolve `k` e `d` em 0..100. Janela achatada (maximo == minimo) sai
    **`None`**, nao 50: nao houve variacao de RSI para posicionar nada, e
    publicar o meio afirmaria neutralidade medida.
    """
    r = rsi(fech, janela)
    n = len(r)
    cru: list[Num] = _vazio(n)
    for i in range(n):
        if r[i] is None:
            continue
        jan = [x for x in r[max(0, i - estoc + 1):i + 1] if x is not None]
        if len(jan) < estoc:
            continue
        lo, hi = min(jan), max(jan)
        cru[i] = None if hi == lo else 100.0 * (r[i] - lo) / (hi - lo)
    kk = _sma_com_buracos(cru, k)
    dd = _sma_com_buracos(kk, d)
    return {"k": kk, "d": dd}


# -------------------------------------------------------------------- MACD

def macd(fech: list[float], rapida: int = MACD_RAPIDA,
         lenta: int = MACD_LENTA,
         sinal: int = MACD_SINAL) -> dict[str, list[Num]]:
    """MACD, linha de sinal e histograma.

    O sinal e o EMA do MACD, e o aquecimento dele comeca onde o MACD comeca —
    nao no inicio da serie. Por isso o EMA do sinal roda sobre a parte densa e
    volta para a posicao certa no fim, em vez de o chamador somar indices.
    """
    er, el = ema(fech, rapida), ema(fech, lenta)
    linha: list[Num] = [None if (a is None or b is None) else a - b
                        for a, b in zip(er, el)]
    densa = [x for x in linha if x is not None]
    ini = len(linha) - len(densa)
    s_densa = ema(densa, sinal) if densa else []
    s: list[Num] = _vazio(ini) + list(s_densa)
    s = s + _vazio(len(linha) - len(s))
    hist: list[Num] = [None if (a is None or b is None) else a - b
                       for a, b in zip(linha, s)]
    return {"macd": linha, "sinal": s, "hist": hist}


# ------------------------------------------------------------------ canais

def bollinger(fech: list[float], janela: int = BOLL_JANELA,
              k: float = BOLL_K) -> dict[str, list[Num]]:
    """Bandas de Bollinger com desvio POPULACIONAL (`pstdev`).

    As plataformas usam o populacional; trocar por amostral alarga a banda em
    ~2,6% numa janela de 20 e a banda deixa de bater com a da tela.
    """
    m = sma(fech, janela)
    sup: list[Num] = _vazio(len(fech))
    inf: list[Num] = _vazio(len(fech))
    if janela < 1 or len(fech) < janela:
        return {"meio": m, "sup": sup, "inf": inf}
    for i in range(janela - 1, len(fech)):
        dp = st.pstdev(fech[i - janela + 1:i + 1])
        sup[i] = m[i] + k * dp                      # type: ignore[operator]
        inf[i] = m[i] - k * dp                      # type: ignore[operator]
    return {"meio": m, "sup": sup, "inf": inf}


def donchian(maximas: list[float], minimas: list[float],
             janela: int = DONCHIAN_JANELA) -> dict[str, list[Num]]:
    """Canal de Donchian: maxima e minima da janela, do EXTREMO e nao do
    fechamento — a mesma razao pela qual a sobrevivencia deste projeto conta
    toque de borda e nao fechamento fora dela."""
    n = len(maximas)
    sup: list[Num] = _vazio(n)
    inf: list[Num] = _vazio(n)
    meio: list[Num] = _vazio(n)
    if janela < 1 or n < janela:
        return {"sup": sup, "inf": inf, "meio": meio}
    for i in range(janela - 1, n):
        hi = max(maximas[i - janela + 1:i + 1])
        lo = min(minimas[i - janela + 1:i + 1])
        sup[i], inf[i], meio[i] = hi, lo, (hi + lo) / 2
    return {"sup": sup, "inf": inf, "meio": meio}


def _filtro_gauss(v: list[float], alpha: float, polos: int) -> list[float]:
    """Filtro gaussiano de N polos como CASCATA de N filtros de 1 polo.

    E equivalente a expansao binomial que o indicador original escreve com
    coeficientes ate a sexta ordem, e legivel o bastante para alguem conferir.
    O primeiro ponto semeia com o proprio valor: a alternativa, comecar em
    zero, faria o filtro subir do chao ao longo de centenas de barras.
    """
    saida = list(v)
    for _ in range(polos):
        acc = saida[0]
        nova = []
        for x in saida:
            acc = alpha * x + (1 - alpha) * acc
            nova.append(acc)
        saida = nova
    return saida


def _true_range(maximas: list[float], minimas: list[float],
                fech: list[float]) -> list[float]:
    tr = [maximas[0] - minimas[0]]
    for i in range(1, len(fech)):
        tr.append(max(maximas[i] - minimas[i],
                      abs(maximas[i] - fech[i - 1]),
                      abs(minimas[i] - fech[i - 1])))
    return tr


def gaussiano(maximas: list[float], minimas: list[float],
              fech: list[float], polos: int = GC_POLOS,
              periodo: int = GC_PERIODO,
              mult: float = GC_MULT) -> dict[str, list[Num]]:
    """Gaussian Channel de Ehlers: linha filtrada e banda de True Range.

    A banda usa o MESMO filtro aplicado ao True Range — nao um desvio padrao.
    E por isso que ele se alarga em barra de amplitude grande mesmo sem o
    fechamento andar, que e o comportamento que o distingue do Bollinger.

    **Aquecimento declarado:** um filtro recursivo nao tem ponto em que fica
    "pronto", ele converge. As primeiras `periodo` barras saem `None` porque
    ali o resultado ainda carrega a semente, e desenha-las afirmaria um canal
    que e artefato do inicio da serie.
    """
    n = len(fech)
    if n < 2 or periodo < 2 or polos < 1 or n <= periodo:
        return {"meio": _vazio(n), "sup": _vazio(n), "inf": _vazio(n)}
    beta = ((1 - math.cos(2 * math.pi / periodo))
            / (math.pow(1.414, 2.0 / polos) - 1))
    alpha = -beta + math.sqrt(beta * beta + 2 * beta)

    f = _filtro_gauss(fech, alpha, polos)
    ftr = _filtro_gauss(_true_range(maximas, minimas, fech), alpha, polos)

    meio: list[Num] = _vazio(periodo) + [f[i] for i in range(periodo, n)]
    sup: list[Num] = _vazio(periodo) + [f[i] + ftr[i] * mult
                                        for i in range(periodo, n)]
    inf: list[Num] = _vazio(periodo) + [f[i] - ftr[i] * mult
                                        for i in range(periodo, n)]
    return {"meio": meio, "sup": sup, "inf": inf}


def atr(maximas: list[float], minimas: list[float], fech: list[float],
        janela: int = ATR_JANELA, modo: str = "wilder") -> list[Num]:
    """ATR, serie inteira, nas DUAS convencoes — e elas dao numeros
    diferentes.

    `tecnica.atr` devolve um ESCALAR e usa a media simples dos ultimos `janela`
    True Ranges; as plataformas de grafico usam a suavizacao de Wilder, que
    carrega a serie inteira com peso decrescente. Nao ha uma certa: ha a que a
    decisao de largura deste projeto ja usa e a que o usuário lê na tela.

    **Entao as duas moram aqui, nomeadas**, em vez de eu escolher uma e deixar
    a outra divergir em silencio — que e a familia da `_horas_fora`
    duplicada. `modo="media"` bate EXATAMENTE com `tecnica.atr` no ultimo
    ponto, e ha teste exigindo isso; `modo="wilder"` e o default porque esta
    curva existe para o grafico.
    """
    n = len(fech)
    if n < 2:
        return _vazio(n)
    tr = _true_range(maximas, minimas, fech)[1:]
    if modo == "media":
        return [None] + list(sma(tr, janela))
    if modo != "wilder":
        raise ValueError(f"modo {modo!r}: use 'wilder' ou 'media'")
    return [None] + list(_wilder(tr, janela))


__all__ = ["sma", "ema", "rsi", "stoch_rsi", "macd", "bollinger", "donchian",
           "gaussiano", "atr", "RSI_JANELA", "STOCH_JANELA", "STOCH_K",
           "STOCH_D", "MACD_RAPIDA", "MACD_LENTA", "MACD_SINAL",
           "BOLL_JANELA", "BOLL_K", "DONCHIAN_JANELA", "ATR_JANELA",
           "GC_POLOS", "GC_PERIODO", "GC_MULT"]
