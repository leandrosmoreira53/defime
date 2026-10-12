"""Heatmap das POOLS RWA que mais estao pagando — APR, volume, giro e aquecimento.

Plano e medicao: o plano do heatmap. **Puro:** recebe o `rwa.json` que o job
do `/rwa/` (agregador) grava e devolve tiles com metricas, selos e layout.
Sem banco, sem rede, sem I/O. Quem le arquivo e banco e o job do painel.

---

**O tile e a POOL, o contrario do `/heat/`.** La a pergunta e "qual ativo
andou"; aqui e "qual pool paga", e as tres pools de SPY pagam 26%, 30% e "—".
Somar as tres esconderia a escolha que a pagina existe para mostrar.

**Toda conta derivada mora AQUI, e so aqui.** O agregador entrega os numeros
crus (volume, TVL, taxa do LP, a serie de `vol_1h`); giro, aquecimento,
concentracao e selo saem deste modulo. Duas implementacoes divergiriam no
primeiro ajuste — a familia da `_horas_fora`.

---

**APR nao e uma terceira informacao independente.** `APR = giro x taxa do
LP x 365`: a GME de 1% gira 14x menos que a de 0,05% e paga mais. Ha teste
exigindo a identidade, e a pagina diz isso na legenda.

**"Surto" precisa de serie, e sem serie o selo e "medindo", nunca "ok".** A
concentracao e a maior hora do dia sobre o dia, e so vale com
`SERIE_MIN_PONTOS` pontos em 24 h — abaixo disso ela mede um pedaco do dia.
Medido em 24/09: mediana 0,24, p90 0,47, e a PLTR 1% V4 com 0,71 (977% de APR
em 24 h contra 231% em 7 dias).

**O porteiro e "taxa do LP lida do contrato", nao "e Uniswap".** O topo do
APR em 24/09 eram NVDA (1.055%) e AAPL (375%) no `up-v3`, com a taxa NOMINAL,
porque a fatia do protocolo daquele AMM nao se le. Fora do mapa padrao, atras
de botao e com selo; nunca somem.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field
from typing import Callable

from defime.heatmap import squarify

# Porteiros. Os numeros e o caso que justifica cada um estao na Fase 0 do plano.
TVL_MIN_USD = 10_000.0          # guarda: a menor pool USDG em 24/09 tinha $97.882
IDADE_MIN_D = 3.0               # guarda: a mais nova em 24/09 tinha 5,2 dias
CONC_MAX = 0.50                 # PLTR 1% V4: 0,71; a seguinte, RDDT, 0,49
DIV_TVL_MAX_PCT = 25.0          # mesmo limiar do /rwa/
SERIE_MIN_PONTOS = 18           # 18 das 24 horas; a regra de `heatmap.JANELA_MIN_H`
JANELA_S = 24 * 3600

# Area proporcional so enquanto o menor tile ainda se ve: acima desta razao a
# grade vira uniforme (a licao dos $181 trilhoes do BTC na aba da bolsa).
GRADE_RAZAO_MAX = 200.0
DIV_TETO_MAX_LOG2 = 2.0         # a escala do "aquecendo" satura em 4x e em 1/4x

ABAS = {
    # aba: (campo da cor, campo da area, tipo de escala)
    "apr": ("apr_24h", "tvl", "log"),
    "volume": ("vol_24h", "vol_24h", "log"),
    "giro": ("giro", "tvl", "log"),
    "aquecendo": ("aquecendo", "vol_24h", "divergente"),
}

SELOS = {
    "nova": "pool com menos de {:.0f} dias: o numero descreve a infancia dela",
    "surto": "a maior hora teve {:.0%} do volume do dia: o dia foi uma hora so",
    "tvl_diverge": "TVL do agregador e do indexador diferem {:+.0f}%",
    "taxa_nominal": "taxa do LP nao legivel neste AMM: APR com a taxa NOMINAL",
    "sem_taxa": "taxa definida a cada swap pelo hook: sem APR",
}


@dataclass
class Pool:
    pool_id: str
    par: str
    simbolo: str
    usdg: bool
    versao: str | None
    dex: str | None
    fee_pct: float | None           # nominal, a que a interface mostra
    lp_pct: float | None            # a que chega ao LP, lida do contrato
    tvl: float
    tvl_idx: float | None
    vol_1h: float | None
    vol_24h: float | None
    apr_24h: float | None
    apr_7d: float | None
    apr_30d: float | None
    idade_d: float | None
    giro: float | None = None
    aquecendo: float | None = None
    concentracao: float | None = None
    pontos_serie: int = 0
    div_tvl_pct: float | None = None
    selos: dict[str, str] = field(default_factory=dict)
    fora_do_padrao: str | None = None     # motivo, ou None se entra no mapa padrao
    # `agregador` (rwa.json, GeckoTerminal + taxa do contrato) ou `indexador`
    # (pools_ativos.json, medido pelo pools_hora). Vai no tile e no balao.
    fonte: str = "agregador"
    apr_idx: float | None = None          # o APR da MESMA pool pelo indexador, para conferir
    book_1pct: float | None = None        # book +-1% do indexador (o denominador da fatia)
    horas_amostradas: float | None = None  # so na fonte indexador: de quantas horas sai o volume

    @property
    def rotulo(self) -> str:
        """`SPY · 0,05% · V4`. O par so aparece quando NAO e USDG, e o AMM so
        quando NAO e Uniswap: em 24/09 a "META 0,3% V3" que pagava 352% era da
        Ramses, onde o LP fica com 0,044% da taxa — "V3" escondia isso."""
        taxa = f"{_num(self.fee_pct)}%" if self.fee_pct is not None else "?"
        nome = self.simbolo if self.usdg else self.par.replace(" ", "")
        return f"{nome} · {taxa} · {amm(self.dex, self.versao)}"


def amm(dex: str | None, versao: str | None) -> str:
    """`uniswap-v4-robinhood` -> `V4`; `ramses-v3-robinhood` -> `RAMSES V3`."""
    d = (dex or "").lower().replace("-robinhood", "")
    if not d or d.startswith("uniswap"):
        return (versao or d.rsplit("-", 1)[-1] or "?").upper()
    return d.replace("-", " ").upper()


def _num(v: float) -> str:
    s = f"{v:.3f}".rstrip("0").rstrip(".")
    return s.replace(".", ",")


def _idade(criada: str | None, agora: int) -> float | None:
    if not criada:
        return None
    try:
        t = dt.datetime.fromisoformat(criada.replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    return (agora - t.timestamp()) / 86400


def concentracao(serie: list[list[float]], vol_24h: float | None,
                 agora: int) -> tuple[float | None, int]:
    """(maior `vol_1h` das ultimas 24 h / `vol_24h`, pontos usados).

    None abaixo de `SERIE_MIN_PONTOS`: com a serie curta a maior hora vista e
    a maior de um PEDACO do dia, e o selo sairia "ok" por falta de olhar.
    """
    pts = [v for t, v in serie if agora - JANELA_S < t <= agora and v is not None]
    if len(pts) < SERIE_MIN_PONTOS or not vol_24h or vol_24h <= 0:
        return None, len(pts)
    return max(pts) / vol_24h, len(pts)


def normalizar(dados: dict, agora: int) -> list[Pool]:
    """`rwa.json["dados"]` -> uma `Pool` por linha, com metricas e selos."""
    saida = []
    for r in dados.get("rwa", []):
        par = r.get("nome") or r["pool_id"]
        lados = [s.strip() for s in par.split("/")]
        usdg = any(s.upper() == "USDG" for s in lados)
        simbolo = next((s for s in lados if s.upper() != "USDG"), lados[0]).upper()
        tvl = r.get("tvl") or 0.0
        v24, v1 = r.get("vol_24h"), r.get("vol_1h")
        p = Pool(pool_id=r["pool_id"], par=par, simbolo=simbolo, usdg=usdg,
                 versao=r.get("versao"), dex=r.get("dex"), fee_pct=r.get("fee"),
                 lp_pct=r.get("lp"), tvl=tvl, tvl_idx=r.get("tvl_idx"),
                 vol_1h=v1, vol_24h=v24, apr_24h=r.get("apr_24h"),
                 apr_7d=r.get("apr_7d"), apr_30d=r.get("apr_30d"),
                 idade_d=_idade(r.get("criada"), agora))
        p.giro = v24 / tvl if v24 is not None and tvl > 0 else None
        p.aquecendo = v1 * 24 / v24 if v1 is not None and v24 else None
        p.concentracao, p.pontos_serie = concentracao(r.get("serie_vol_1h") or [], v24, agora)
        if p.tvl_idx and tvl:
            p.div_tvl_pct = (p.tvl_idx - tvl) / tvl * 100
        _porteiros(p, r)
        saida.append(p)
    return saida


def _porteiros(p: Pool, r: dict) -> None:
    if p.idade_d is not None and p.idade_d < IDADE_MIN_D:
        p.selos["nova"] = SELOS["nova"].format(IDADE_MIN_D)
    if p.concentracao is not None and p.concentracao > CONC_MAX:
        p.selos["surto"] = SELOS["surto"].format(p.concentracao)
    if p.div_tvl_pct is not None and abs(p.div_tvl_pct) > DIV_TVL_MAX_PCT:
        p.selos["tvl_diverge"] = SELOS["tvl_diverge"].format(p.div_tvl_pct)
    if p.lp_pct is None:
        if r.get("marca") == "hook":
            p.selos["sem_taxa"] = SELOS["sem_taxa"]
            p.apr_24h = p.apr_7d = p.apr_30d = None     # o /rwa/ ja manda None; garante
        else:
            p.selos["taxa_nominal"] = SELOS["taxa_nominal"]

    if not p.usdg:
        p.fora_do_padrao = "par sem USDG"
    elif p.lp_pct is None:
        p.fora_do_padrao = ("taxa dinamica (hook)" if "sem_taxa" in p.selos
                            else f"taxa do LP nao legivel ({r.get('motivo') or p.dex})")


def complementar(pools: list[Pool], ativos: dict, agora: int) -> list[Pool]:
    """Junta o que o INDEXADOR mede (`pools_ativos.json["dados"]["ativos"]`).

    Duas coisas, e nenhuma e conta nova — o APR, a fee e o book ja sairam de
    `defime/pools_ativos`:

    1. **Pool que o agregador nao lista entra, marcada `indexador`.** Em 24/09
       o universo do `/rwa/` nao tinha a QUBT/USDG, que e a pool da posicao
       ABERTA, nem LULU, RBLX e PLTR 0,3%. Um heatmap de "onde por o dinheiro"
       sem a pool onde o dinheiro esta nao compara nada.
    2. **Pool que as duas fontes tem ganha `apr_idx` e `book_1pct`**: o APR
       do indexador vai no balao ao lado do do agregador (conferencia, nao
       media), e o book e o denominador da fatia no top.

    O volume de 24 h da fonte indexador e ESTIMADO: `volume da janela /
    horas amostradas x 24`, porque cada leitura do `pools_hora` varre ~1 h e
    elas saem a cada ~2 h. Vai marcado, e `vol_1h` fica None — sem ele nao
    ha aquecimento nem concentracao, e isso nao vira "ritmo normal".
    """
    por_id = {p.pool_id.lower(): p for p in pools}
    novas = []
    for sim, a in (ativos or {}).items():
        for q in a.get("pools") or []:
            pid = (q.get("pool") or "").lower()
            if not pid:
                continue
            ja = por_id.get(pid)
            if ja is not None:
                ja.apr_idx = q.get("apr_lp_pct")
                ja.book_1pct = q.get("book_1pct_usd")
                continue
            h = q.get("horas_amostradas") or 0.0
            vj, fj = q.get("volume_janela_usd"), q.get("fee_lp_janela_usd")
            v24 = vj / h * 24 if vj is not None and h > 0 else None
            lp = fj / vj * 100 if fj is not None and vj else None
            tvl = q.get("tvl_usd") or 0.0
            p = Pool(pool_id=pid, par=f"{sim} / USDG", simbolo=sim.upper(), usdg=True,
                     versao=q.get("versao"), dex=None, fee_pct=q.get("taxa_medida_pct"),
                     lp_pct=lp, tvl=tvl, tvl_idx=tvl, vol_1h=None, vol_24h=v24,
                     apr_24h=q.get("apr_lp_pct"), apr_7d=None, apr_30d=None,
                     idade_d=None, fonte="indexador", apr_idx=q.get("apr_lp_pct"),
                     book_1pct=q.get("book_1pct_usd"), horas_amostradas=h or None)
            p.giro = v24 / tvl if v24 is not None and tvl > 0 else None
            p.pontos_serie = 0
            if p.apr_24h is None:
                p.selos["sem_taxa"] = ("indexador sem APR: "
                                       + (q.get("motivo_apr") or "fee da janela ilegivel"))
                p.fora_do_padrao = "sem APR no indexador"
            p.selos["estimado"] = (f"fonte indexador: volume 24 h ESTIMADO de {h:.1f} h "
                                   "amostradas; sem volume da ultima hora")
            por_id[pid] = p
            novas.append(p)
    return pools + novas


def filtrar(pools: list[Pool], incluir_outros: bool = False,
            tvl_min: float = TVL_MIN_USD) -> tuple[list[Pool], dict[str, list[str]]]:
    """(no mapa, {motivo: [rotulos]}). Quem sai e CONTADO, nunca sumido."""
    dentro, fora = [], {}
    for p in pools:
        motivo = None
        if p.tvl < tvl_min:
            motivo = f"TVL abaixo de ${tvl_min:,.0f}".replace(",", ".")
        elif p.fora_do_padrao and not incluir_outros:
            motivo = p.fora_do_padrao
        if motivo:
            fora.setdefault(motivo, []).append(p.rotulo)
        else:
            dentro.append(p)
    return dentro, {k: sorted(v) for k, v in sorted(fora.items())}


# --------------------------------------------------------------- escalas

def _quantil(xs: list[float], q: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * (len(xs) - 1)))]


def escala_log(valores: list[float | None]) -> dict | None:
    """Piso p10 e teto p90 em espaco log. APR vai de 11% a 978% e volume de
    $100k a $14M: linear pintaria tudo de uma cor com duas pools saturadas.
    Os dois limites vao escritos na legenda — o corte nao pode ser calado."""
    pos = [v for v in valores if v is not None and v > 0]
    if not pos:
        return None
    lo, hi = _quantil(pos, 0.10), _quantil(pos, 0.90)
    if hi <= lo:
        hi = lo * 10
    return {"tipo": "log", "piso": lo, "teto": hi}


def escala_divergente(valores: list[float | None]) -> dict | None:
    """Aquecimento em torno de 1,0x (o ritmo medio do dia), simetrico em log2.

    Teto = p90 de |log2| preso entre 1 (2x) e `DIV_TETO_MAX_LOG2` (4x). O piso
    impede que 1,3x num dia calmo saia saturado. O TETO existe por medicao: em
    24/09 as pools sem negocio na ultima hora (0,00x, normal de madrugada)
    puxaram o p90 para 36x, e a META esquentando a 3,8x ganharia um terco da
    cor. Razao tem escala natural, e acima de 4x "muito mais que o normal" ja
    e toda a informacao que a cor precisa dar — o numero vai no balao."""
    xs = [abs(math.log2(v)) for v in valores if v is not None and v > 0]
    teto = min(DIV_TETO_MAX_LOG2, max(1.0, _quantil(xs, 0.90))) if xs else 1.0
    return {"tipo": "divergente", "centro": 1.0, "teto_log2": teto}


def intensidade(v: float | None, escala: dict | None) -> float | None:
    """0..1 na log, -1..1 na divergente. None e "sem valor" (tile cinza)."""
    if v is None or escala is None:
        return None
    if escala["tipo"] == "log":
        if v <= 0:
            return 0.0
        a, b = math.log(escala["piso"]), math.log(escala["teto"])
        return min(1.0, max(0.0, (math.log(v) - a) / (b - a)))
    if v <= 0:
        return -1.0
    return min(1.0, max(-1.0, math.log2(v) / escala["teto_log2"]))


# ---------------------------------------------------------------- layout

def montar(pools: list[Pool], aba: str, largura: float = 100.0,
           altura: float = 62.0) -> dict:
    """Layout e cor de UMA aba. As abas nao compartilham coordenadas: a area
    muda de TVL para volume, e a pool grande e parada troca de lugar com a
    pequena e agitada — essa troca e informacao."""
    if aba not in ABAS:
        raise ValueError(f"aba={aba!r}: use {', '.join(ABAS)}")
    campo_cor, campo_area, tipo = ABAS[aba]
    vivas = [p for p in pools if (getattr(p, campo_area) or 0) > 0]
    sem_area = sorted(p.rotulo for p in pools if (getattr(p, campo_area) or 0) <= 0)
    vivas.sort(key=lambda p: -(getattr(p, campo_area) or 0))

    areas = [getattr(p, campo_area) for p in vivas]
    uniforme = bool(areas) and max(areas) / min(areas) > GRADE_RAZAO_MAX
    if uniforme:
        # Na grade o tamanho nao codifica nada, entao a ORDEM passa a ser a
        # da cor: a mais quente no canto de cima, onde o olho comeca. Sem
        # valor vai para o fim, nunca como "o menor".
        vivas.sort(key=lambda p: (getattr(p, campo_cor) is None,
                                  -(getattr(p, campo_cor) or 0)))
    caixas = squarify([1.0] * len(vivas) if uniforme else areas, 0, 0, largura, altura)

    valores = [getattr(p, campo_cor) for p in vivas]
    escala = escala_log(valores) if tipo == "log" else escala_divergente(valores)
    tiles = [{"pool": p, "x": x, "y": y, "l": l, "a": a,
              "valor": getattr(p, campo_cor),
              "intensidade": intensidade(getattr(p, campo_cor), escala)}
             for p, (x, y, l, a) in zip(vivas, caixas)]
    return {"aba": aba, "campo_cor": campo_cor, "campo_area": campo_area,
            "escala": escala, "grade_uniforme": uniforme, "tiles": tiles,
            "sem_area": sem_area,
            "sem_valor": sorted(t["pool"].rotulo for t in tiles if t["valor"] is None)}


# ------------------------------------------------------- top para o capital

def volume_dia_7d(p: Pool) -> float | None:
    """Volume MEDIO por dia nos ultimos 7 d, invertendo o `apr_7d` do /rwa/.

    `apr_7d = v7d/dias x taxa_LP / TVL x 365`, com a mesma taxa e o mesmo
    TVL; entao `v7d/dias = apr_7d x TVL / (taxa_LP x 365)` e EXATO, nao
    estimativa — so desfaz a conta que o agregador fez. None fora do agregador
    ou sem taxa legivel."""
    if p.fonte != "agregador" or p.apr_7d is None or not p.lp_pct or not p.tvl:
        return None
    return p.apr_7d * p.tvl / (p.lp_pct * 365)


def volume_para_decidir(p: Pool) -> tuple[float | None, str | None]:
    """O MENOR entre o volume de 24 h e a media diaria de 7 d, e qual foi.

    **Por medicao de 24/09:** o top saiu com a PLTR 1% V4 em primeiro, 395% e
    "paga em 3 h" — a pool que a Fase 0 mediu com 71% do dia numa hora so, e
    cujo selo de surto ainda nao existe porque a serie de `vol_1h` tem um
    ponto. O menor dos dois nao deixa um dia de surto inflar a pool, nem uma
    pool que esvaziou se apresentar pela semana boa. E conservador de
    proposito: o top e onde o numero vira convite a trocar, e este projeto ja
    mediu o volume oscilando 7x dentro do dia."""
    cand = [(v, j) for v, j in ((p.vol_24h, "24 h"), (volume_dia_7d(p), "7 d"))
            if v is not None and v > 0]
    if not cand:
        return None, None
    return min(cand)


def fee_hora(p: Pool, book: float | None, capital: float) -> float | None:
    """`volume/h x taxa do LP x C/(book+C)`, com o volume de `volume_para_decidir`.

    Uma conta so, usada no top E na pool atual: comparar a pool de la pela
    formula e a daqui pela fee realizada da posicao (outra largura, outra
    janela) poria na mesma coluna duas perguntas diferentes."""
    vol, _ = volume_para_decidir(p)
    if p.lp_pct is None or not book or book <= 0 or not vol:
        return None
    return vol / 24 * (p.lp_pct / 100) * capital / (book + capital)


def top_fee_hora(pools: list[Pool], books: dict[str, float], capital: float,
                 n: int = 5, custo: Callable[[float, float, float], float | None] | None = None,
                 fee_h_atual: float | None = None) -> dict:
    """As `n` pools que mais pagariam por hora a `capital` na banda de +-1%.

    `fatia = C / (book + C)` e `fee/h = volume/h x taxa do LP x fatia` — a
    forma fechada do `/score/`, que vale porque o book +-1% E o capital que a
    liquidez ativa representa naquela banda. Usa o volume MEDIO das 24 h, nao
    o da hora, e o MENOR entre 24 h e a media de 7 d (`volume_para_decidir`):
    o ranking nao pode ser decidido pelo acaso do minuto nem do dia.

    `custo(capital, book, taxa_frac)` e injetado pelo job, que ja inclui a
    perna de SAIDA da pool atual; a conta de cada perna e a do `/score/`, para
    existir num lugar so. Com `fee_h_atual`, "paga em" e
    `custo / (fee/h de la - fee/h atual)` em horas DENTRO do range.

    Fora do top, e contadas: pool sem book medido, sem taxa legivel, com selo
    `nova` ou `surto` — o top e onde o numero vira convite a trocar, e ali
    nenhuma das duas ressalvas cabe num `title`.
    """
    linhas, fora = [], {}
    for p in pools:
        motivo = None
        book = books.get(p.pool_id.lower()) or p.book_1pct
        if p.lp_pct is None:
            motivo = "sem taxa do LP legivel"
        elif not book or book <= 0:
            motivo = "sem book +-1% medido pelo indexador"
        elif not p.vol_24h:
            motivo = "sem volume"
        elif "nova" in p.selos or "surto" in p.selos:
            motivo = "selo " + " + ".join(s for s in ("nova", "surto") if s in p.selos)
        if motivo:
            fora.setdefault(motivo, []).append(p.rotulo)
            continue
        fatia = capital / (book + capital)
        fee_h = fee_hora(p, book, capital)
        # O swap paga a taxa CHEIA da pool (nominal), nao a fatia do LP.
        taxa_swap = (p.fee_pct if p.fee_pct is not None else p.lp_pct) / 100
        c = custo(capital, book, taxa_swap) if custo else None
        ganho = fee_h - fee_h_atual if fee_h_atual is not None else None
        paga_h = c / ganho if c is not None and ganho and ganho > 0 else None
        linhas.append({"pool": p, "book": book, "fatia": fatia, "fee_h": fee_h,
                       "janela": volume_para_decidir(p)[1],
                       "fee_dia": fee_h * 24, "custo": c, "ganho_h": ganho,
                       "paga_em_h": paga_h})
    linhas.sort(key=lambda d: -d["fee_h"])
    return {"capital": capital, "linhas": linhas[:n], "avaliadas": len(linhas),
            "fora": {k: sorted(v) for k, v in sorted(fora.items())}}


__all__ = ["Pool", "normalizar", "complementar", "filtrar", "montar", "concentracao",
           "fee_hora", "volume_para_decidir", "volume_dia_7d",
           "escala_log", "escala_divergente", "intensidade", "top_fee_hora",
           "ABAS", "TVL_MIN_USD", "CONC_MAX", "SERIE_MIN_PONTOS"]
