"""Livro de liquidez lido do ESTADO da pool V3, nao dos eventos.

**Por que existe.** O livro da pagina sai de `core.book.build_book`, que soma
`Mint`/`Burn` do indice. Falta um `Mint` e o `Burn` dele fica orfao: a curva
cumulativa vai a negativo e `integridade()` recusa desenhar — a defesa de
01/09, que existe porque a MU publicou dois espetos e um vale vazio com um
terco dos blocos nunca coletados. A recusa esta certa, e deixa a aba vazia em
toda pool nova, cujo backfill ainda nao alcancou o nascimento dela.

O contrato responde a mesma pergunta sem depender de coleta nenhuma:
`tickBitmap` diz quais ticks estao inicializados e `ticks()` da o
`liquidityNet` de cada um. O que ele NAO da e historia — "como o livro estava
ontem" continua saindo dos eventos.

**A ancora e o `liquidity()` e isso vai DECLARADO.** `liquidityNet` e um
delta: a soma so fecha a partir de um ponto conhecido, e o ponto conhecido e a
liquidez ativa do contrato. Por isso o retorno carrega `ancora` e as faixas
dos dois lados do tick corrente saem com ela — ler o resultado como se fosse
uma soma independente seria ler errado.

**O que da para conferir de verdade, e esta conferido:**

    liquidityGross > 0 em todo tick lido        tick nao inicializado lido por engano
    |liquidityNet| <= liquidityGross            sinal ou palavra trocada na decodificacao
    nenhuma faixa com liquidez negativa         a janela nao fecha com a ancora

Um erro de sinal no `int128` passaria pelas duas primeiras em algumas pools e
morreria na terceira: metade das pools desta chain tem tick negativo.

**A janela e um RECORTE e nao o livro inteiro** — liquidez fora dela existe e
nao esta somada. Quem publica o numero tem de dizer isso junto.
"""

from __future__ import annotations

from .multicall import aggregate3

SEL_TICK_BITMAP = "0x5339c296"         # tickBitmap(int16)
SEL_TICKS = "0xf30dba93"               # ticks(int24)


def _sem_sinal(v: int, bits: int = 256) -> str:
    return format(v & ((1 << bits) - 1), "064x")


def _com_sinal(h: str, bits: int) -> int:
    v = int(h, 16) & ((1 << bits) - 1)
    return v - (1 << bits) if v >= 1 << (bits - 1) else v


def palavras_do_bitmap(tick_lo: int, tick_hi: int, spacing: int) -> list[int]:
    """As palavras do `tickBitmap` que cobrem a janela, sem repetir.

    Varrer o bitmap e o que separa "os ticks que EXISTEM" de "todos os
    multiplos do spacing": sem ele seriam centenas de `ticks()` devolvendo
    zero.
    """
    lo, hi = sorted((tick_lo // spacing, tick_hi // spacing))
    return sorted({c >> 8 for c in range(lo, hi + 1)})


def ticks_inicializados(rpc, pool: str, spacing: int, tick_lo: int,
                        tick_hi: int, bloco: int) -> list[int]:
    palavras = palavras_do_bitmap(tick_lo, tick_hi, spacing)
    respostas = aggregate3(
        rpc, [(pool, SEL_TICK_BITMAP + _sem_sinal(w, 256)) for w in palavras],
        bloco)
    comp_lo, comp_hi = sorted((tick_lo // spacing, tick_hi // spacing))
    saida = []
    for w, r in zip(palavras, respostas):
        if not r:
            continue
        bm = int(r, 16)
        for b in range(256):
            if bm >> b & 1:
                c = (w << 8) + b
                if comp_lo <= c <= comp_hi:
                    saida.append(c * spacing)
    return sorted(saida)


def gross_e_net(rpc, pool: str, ticks: list[int], bloco: int) -> dict[int, tuple[int, int]]:
    """`ticks(i)` de cada tick: `liquidityGross` (uint128) e `liquidityNet` (int128)."""
    if not ticks:
        return {}
    respostas = aggregate3(
        rpc, [(pool, SEL_TICKS + _sem_sinal(t, 256)) for t in ticks], bloco)
    saida = {}
    for t, r in zip(ticks, respostas):
        if not r or len(r) < 2 + 128:
            continue
        h = r[2:]
        saida[t] = (int(h[:64], 16), _com_sinal(h[64:128], 128))
    return saida


def caminhar(tick: int, liquidez: int, net: dict[int, int],
             lo: int | None = None, hi: int | None = None) -> list[tuple[int, int, int]]:
    """Faixas `(tick_de, tick_ate, liquidez)` a partir da ancora.

    `liquidityNet[t]` entra ao cruzar `t` para CIMA, entao a faixa abaixo de
    `t` tem `L - net[t]`. **A faixa que contem o tick corrente vale a ancora
    dos DOIS lados dele** — ela so esta partida em duas porque a caminhada
    comeca no tick corrente, e nao numa borda.

    **As pontas fecham em `lo`/`hi` quando eles vem.** Depois do ultimo tick
    inicializado da janela a liquidez continua valendo ate o proximo tick
    inicializado, que esta FORA — sem fechar na borda, a faixa que contem o
    preco sumia do desenho toda vez que nao havia tick inicializado acima
    dele, que e o caso de qualquer pool com o livro todo abaixo do preco.
    """
    segs = []
    acc, anterior = liquidez, tick
    for t in sorted(x for x in net if x > tick):
        segs.append((anterior, t, acc))
        acc += net[t]
        anterior = t
    if hi is not None and hi > anterior:
        segs.append((anterior, hi, acc))
    acc, anterior = liquidez, tick
    for t in sorted((x for x in net if x <= tick), reverse=True):
        segs.append((t, anterior, acc))
        acc -= net[t]
        anterior = t
    if lo is not None and lo < anterior:
        segs.append((lo, anterior, acc))
    return sorted(s for s in segs if s[1] > s[0])


def livro(rpc, pool: str, *, spacing: int, tick: int, liquidez: int,
          alcance_ticks: int, bloco: int) -> dict:
    """O livro da janela, com a conferencia junto. Nao desenha nada."""
    lo = ((tick - alcance_ticks) // spacing) * spacing
    hi = ((tick + alcance_ticks) // spacing + 1) * spacing
    ticks = ticks_inicializados(rpc, pool, spacing, lo, hi, bloco)
    dados = gross_e_net(rpc, pool, ticks, bloco)
    problemas = []
    for t, (gross, net) in sorted(dados.items()):
        if gross <= 0:
            problemas.append("tick %d com liquidityGross zero: nao esta inicializado" % t)
        elif abs(net) > gross:
            problemas.append("tick %d com |net| > gross: decodificacao suspeita" % t)
    segs = caminhar(tick, liquidez, {t: n for t, (_g, n) in dados.items()}, lo, hi)
    negativos = [s for s in segs if s[2] < 0]
    if negativos:
        problemas.append("%d faixa(s) com liquidez negativa: a janela nao fecha "
                         "com a liquidez ativa do contrato" % len(negativos))
    return {"segmentos": [{"tick_de": a, "tick_ate": b, "liquidez": L}
                          for a, b, L in segs],
            "ticks": len(ticks), "ancora": liquidez, "bloco": bloco,
            "janela_ticks": [lo, hi], "problemas": problemas,
            "confere": not problemas,
            "ressalva": "janela e recorte: liquidez fora dela existe e nao esta somada"}


__all__ = ["livro", "caminhar", "ticks_inicializados", "gross_e_net",
           "palavras_do_bitmap"]
