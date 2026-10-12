"""Cotacao da acao na BOLSA, para comparar com o preco na pool.

O projeto tem o preco de dentro da chain desde sempre. O que faltava era a
referencia de fora — e ela e o que separa "o token andou" de "a acao andou".

Fonte: Yahoo Finance, `v8/finance/chart`. Sem chave, sem cadastro, so um
`User-Agent`. Medido em 2026-08-31 para MU: **5.097 barras de 1h cobrindo
2023-10-03 a 2026-08-31**, quase tres anos.

---

**NAO EXISTE intervalo de 4h no Yahoo.** Os aceitos sao 1m, 2m, 5m, 15m, 30m,
60m/1h, 1d, 5d, 1wk, 1mo, 3mo. A barra de 4h sai daqui, agregando as de 1h — e
a agregacao e onde mora a armadilha, por isso ela e explicita:

**A bolsa FECHA e a pool nao.** O pregao regular tem 6h30 (9:30-16:00 ET), o
que da 7 barras de 1h por dia, a ultima com meia hora. Agregar de quatro em
quatro "atravessando" o fechamento juntaria 16h de calendario numa barra
rotulada 4h — o overnight inteiro escondido dentro dela. Entao a agregacao
**quebra no fim do pregao**: cada dia produz blocos completos mais um resto, e
`barras` diz quantas horas de verdade entraram em cada um. Uma barra com
`barras=2` nao e uma barra de 4h, e quem for medir volatilidade precisa saber.

**Serie parada e recusada com o motivo.** Mesma guarda dos outros providers
deste pacote, e pelo mesmo motivo ja pago duas vezes: os futuros da EIA
respondiam 200 com dado congelado em 2024, e o contrato da CFTC foi renomeado
em 2022 sem que a consulta antiga reclamasse. Dado velho entregue em silencio
e pior que dado ausente — o ausente o sistema declara, o velho ele usa.
"""

from __future__ import annotations

import datetime as dt
import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from ..core.sessoes import NY as _NY, PREGAO, sessao_de

URL = ("https://query1.finance.yahoo.com/v8/finance/chart/{simbolo}"
       "?interval={intervalo}&range={janela}&includePrePost=false")
UA = {"User-Agent": "Mozilla/5.0 (compatible; uso-lp/1.0)"}
TIMEOUT_S = 30

# Intervalos que o Yahoo aceita. `4h` NAO esta aqui de proposito: ele nao
# existe na API e sai de `agregar()`.
INTERVALOS = {"1m", "2m", "5m", "15m", "30m", "60m", "1h", "1d", "1wk", "1mo"}

# Quanto tempo uma serie pode ficar sem barra nova antes de ser recusada.
# Generoso porque a bolsa fecha: sexta 16:00 ET ate segunda 9:30 ET sao 65h,
# e um feriado emenda mais um dia.
IDADE_MAXIMA_H = 96.0


@dataclass(frozen=True)
class Barra:
    ts: int                  # inicio da barra, epoch UTC
    abertura: float
    maxima: float
    minima: float
    fechamento: float
    volume: int
    barras: int = 1          # quantas barras de origem entraram nesta

    @property
    def quando(self) -> dt.datetime:
        return dt.datetime.fromtimestamp(self.ts, dt.UTC)

    @property
    def amplitude_pct(self) -> float:
        return 100 * (self.maxima / self.minima - 1) if self.minima else 0.0


@dataclass
class Serie:
    simbolo: str
    intervalo: str
    barras: list[Barra] = field(default_factory=list)
    disponivel: bool = False
    motivo: str | None = None
    moeda: str | None = None
    fuso: str | None = None

    def resumo(self) -> dict:
        return {"simbolo": self.simbolo, "intervalo": self.intervalo,
                "n": len(self.barras), "disponivel": self.disponivel,
                "motivo": self.motivo, "moeda": self.moeda,
                "de": self.barras[0].quando.isoformat() if self.barras else None,
                "ate": self.barras[-1].quando.isoformat() if self.barras else None,
                "ultimo_fechamento": (self.barras[-1].fechamento
                                      if self.barras else None)}


def baixar(simbolo: str, intervalo: str = "1h", janela: str = "730d",
           agora: dt.datetime | None = None) -> Serie:
    """Uma serie OHLCV da bolsa. Nunca levanta: devolve `disponivel=False`.

    `agora` existe para o teste congelar o relogio — sem ele, a guarda de
    idade compara fixture antiga com o dia em que a suite roda, e o teste
    troca de veredito com o calendario. Foi exatamente o que aconteceu com o
    `test_macro_eia.py` na virada de 29 para 30/08.
    """
    agora = agora or dt.datetime.now(dt.UTC)
    if intervalo not in INTERVALOS:
        return Serie(simbolo, intervalo, disponivel=False,
                     motivo=f"intervalo {intervalo!r} nao existe na API; "
                            f"para 4h use agregar() sobre 1h")
    try:
        req = urllib.request.Request(
            URL.format(simbolo=simbolo, intervalo=intervalo, janela=janela),
            headers=UA)
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            bruto = json.load(r)
    except Exception as exc:                                  # noqa: BLE001
        return Serie(simbolo, intervalo, disponivel=False,
                     motivo=f"{type(exc).__name__}: {exc}"[:140])

    erro = (bruto.get("chart") or {}).get("error")
    if erro:
        return Serie(simbolo, intervalo, disponivel=False,
                     motivo=f"yahoo: {erro}"[:140])
    res = ((bruto.get("chart") or {}).get("result") or [None])[0]
    if not res or not res.get("timestamp"):
        return Serie(simbolo, intervalo, disponivel=False,
                     motivo="resposta sem timestamp")

    meta = res.get("meta") or {}
    q = res["indicators"]["quote"][0]
    barras = []
    for i, ts in enumerate(res["timestamp"]):
        # Barra sem fechamento e buraco do provedor, nao candle de volume
        # zero: descartar e o certo, preencher com o anterior inventaria
        # preco que nao houve.
        if q["close"][i] is None:
            continue
        barras.append(Barra(
            ts=int(ts), abertura=float(q["open"][i] or q["close"][i]),
            maxima=float(q["high"][i] or q["close"][i]),
            minima=float(q["low"][i] or q["close"][i]),
            fechamento=float(q["close"][i]),
            volume=int(q["volume"][i] or 0)))
    if not barras:
        return Serie(simbolo, intervalo, disponivel=False,
                     motivo="todas as barras vieram sem fechamento")

    idade_h = (agora.timestamp() - barras[-1].ts) / 3600
    if idade_h > IDADE_MAXIMA_H:
        return Serie(simbolo, intervalo, barras=barras, disponivel=False,
                     moeda=meta.get("currency"),
                     fuso=meta.get("exchangeTimezoneName"),
                     motivo=f"serie parada: ultima barra ha {idade_h:.0f}h "
                            f"(limite {IDADE_MAXIMA_H:.0f}h)")
    return Serie(simbolo, intervalo, barras=barras, disponivel=True,
                 moeda=meta.get("currency"),
                 fuso=meta.get("exchangeTimezoneName"))


def agregar(barras: list[Barra], por: int = 4,
            quebrar_no_dia: bool = True) -> list[Barra]:
    """Junta `por` barras de 1h numa so. E daqui que sai a barra de 4h.

    `quebrar_no_dia=True` impede que um bloco atravesse o fechamento do
    pregao. Sem isso, a ultima barra de um dia e as primeiras do seguinte
    cairiam no mesmo bloco, e a barra rotulada "4h" carregaria as ~17h de
    mercado fechado no meio — com a maxima e a minima de dois dias diferentes
    e o gap de abertura escondido dentro do candle.

    O campo `barras` do resultado diz quantas horas entraram de verdade. O
    ultimo bloco de cada dia costuma vir com menos, porque o pregao regular
    tem 6h30 e nao 8h.
    """
    if not barras:
        return []
    saida: list[Barra] = []
    lote: list[Barra] = []

    def fechar() -> None:
        if not lote:
            return
        saida.append(Barra(
            ts=lote[0].ts, abertura=lote[0].abertura,
            maxima=max(b.maxima for b in lote),
            minima=min(b.minima for b in lote),
            fechamento=lote[-1].fechamento,
            volume=sum(b.volume for b in lote), barras=len(lote)))
        lote.clear()

    dia_atual = None
    for b in barras:
        dia = b.quando.date()
        if quebrar_no_dia and dia_atual is not None and dia != dia_atual:
            fechar()
        dia_atual = dia
        lote.append(b)
        if len(lote) >= por:
            fechar()
    fechar()
    return saida


def agrupar_por(barras: list[Barra], chave) -> list[Barra]:
    """Agrega barras em blocos definidos por uma funcao de CHAVE de calendario.

    `agregar()` acima conta de N em N; isto fecha o bloco quando a chave muda.
    Sao coisas diferentes e as duas existem porque a pergunta e diferente:

      * **5 pregoes** e uma contagem — cinco barras diarias seguidas, seja qual
        for o calendario. `agregar(diarias, por=5, quebrar_no_dia=False)`.
      * **1 semana** e um calendario — de segunda a sexta, e a semana que teve
        feriado tem QUATRO barras e continua sendo uma semana.
        `agrupar_por(diarias, lambda b: b.quando.isocalendar()[:2])`.

    Os dois coincidem na maior parte do ano e divergem exatamente onde
    interessa: em volta de feriado e de semana curta. Publicar um e chamar de
    outro seria rotular errado, que e uma familia de bug que este projeto ja
    pagou varias vezes.

    O campo `barras` conta as barras de ORIGEM IMEDIATA, igual ao `agregar` —
    agrupando diarias por semana ele vem **4** na semana de feriado e 5 na
    cheia. Nao e o total de horas: em cascata (1h -> 1d -> 1w) essa contagem
    se perderia de qualquer jeito, e duas semanticas para o mesmo nome seria
    pior que uma so declarada.
    """
    if not barras:
        return []
    saida: list[Barra] = []
    lote: list[Barra] = []

    def fechar() -> None:
        if not lote:
            return
        saida.append(Barra(
            ts=lote[0].ts, abertura=lote[0].abertura,
            maxima=max(b.maxima for b in lote),
            minima=min(b.minima for b in lote),
            fechamento=lote[-1].fechamento,
            volume=sum(b.volume for b in lote),
            barras=len(lote)))
        lote.clear()

    atual = None
    for b in barras:
        k = chave(b)
        if atual is not None and k != atual:
            fechar()
        atual = k
        lote.append(b)
    fechar()
    return saida


def basis(preco_pool: float, serie: Serie) -> dict:
    """Quanto o token na pool esta acima ou abaixo da acao na bolsa.

    **Os dois instantes nao sao o mesmo**, e isso vai declarado em vez de
    escondido: a pool negocia 24/7 e a bolsa fecha. Com o mercado fechado, o
    basis compara agora com o ultimo fechamento — e a diferenca pode ser so o
    tempo, nao o preco. E a sexta aparicao da familia neste projeto, e aqui a
    guarda e publicar `idade_da_referencia_h` junto com o numero.
    """
    if not serie.barras:
        return {"disponivel": False, "motivo": serie.motivo or "sem barras"}
    ref = serie.barras[-1]
    idade_h = (dt.datetime.now(dt.UTC).timestamp() - ref.ts) / 3600
    return {"disponivel": True,
            "preco_pool": preco_pool, "preco_bolsa": ref.fechamento,
            "basis_pct": 100 * (preco_pool / ref.fechamento - 1),
            "referencia_ts": ref.ts,
            "referencia_utc": ref.quando.isoformat(),
            "idade_da_referencia_h": round(idade_h, 2),
            "mercado_provavelmente_fechado": idade_h > 1.5}


# Da maior para a menor. Uma acao que estreou ha pouco nao TEM 730 dias, e o
# provedor responde `422 Unprocessable Entity` — nao lista vazia. Medido em
# 2026-08-31: a RDDT falhou em 730d e respondeu em 1y, porque o IPO dela e
# posterior a setembro de 2023. Ler o 422 como "sem dado" descartaria a acao
# inteira por causa do pedido, nao do papel.
JANELAS = ("730d", "1y", "6mo", "3mo", "60d", "1mo")


def baixar_maior_janela(simbolo: str, intervalo: str = "1h",
                        janelas: tuple[str, ...] = JANELAS,
                        agora: dt.datetime | None = None) -> Serie:
    """A serie mais longa que este papel tem, tentando da maior janela para a
    menor. O `motivo` da ultima tentativa sobrevive quando nenhuma funciona."""
    ultima = None
    for j in janelas:
        s = baixar(simbolo, intervalo, j, agora=agora)
        if s.disponivel:
            return s
        ultima = s
        # Serie parada e resposta VALIDA com dado velho: encurtar a janela nao
        # resolve e so gastaria requisicao. Erro de janela, sim.
        if s.motivo and "parada" in s.motivo:
            break
    return ultima or Serie(simbolo, intervalo, disponivel=False,
                           motivo="nenhuma janela tentada")


# ---------------------------------------------------------------- pregao ----
#
# **O portao existe porque coletar 1m fora do pregao e pagar por nada.** A
# barra de 1 minuto so muda enquanto a bolsa negocia; fora disso o provedor
# devolve a mesma serie do ultimo fechamento, e 30 simbolos a cada 5 min por
# 24 h sao ~8.600 requisicoes/dia sem um dado novo.

@dataclass(frozen=True)
class Portao:
    """Resposta do portao: coletar ou nao, e o motivo (que vai no log)."""
    coletar: bool
    motivo: str


def abertura_de_hoje(agora: int) -> int:
    """09:30 do dia de `agora` em horario de NOVA YORK, em epoch UTC.

    Em ET e nao em UTC fixo: o pregao e 13:30 UTC no verao e 14:30 no inverno,
    e cravar um dos dois erraria metade do ano — a mesma razao que fez
    `core.sessoes` trabalhar em `America/New_York`.
    """
    d = dt.datetime.fromtimestamp(agora, dt.UTC).astimezone(_NY)
    return int(d.replace(hour=9, minute=30, second=0,
                         microsecond=0).timestamp())


# Depois da abertura, quanto tempo a ausencia de barra nova ainda e ambigua
# entre "o provedor nao publicou ainda" e "nao ha pregao hoje".
CARENCIA_ABERTURA_S = 5 * 60


def portao_do_pregao(agora: int, ultima_barra_ts: int | None = None,
                     feriados: frozenset[str] = frozenset()) -> Portao:
    """Decide se vale coletar 1m agora. Puro — sem relogio proprio e sem rede.

    Duas recusas, e elas cobrem coisas diferentes:

    **Fora do pregao** sai pela fronteira de `core.sessoes`, que ja resolve
    horario de verao e fim de semana. Custa zero requisicoes.

    **Feriado da NYSE** `core.sessoes` declara que nao trata, e o calendario
    nao esta neste projeto. Mas o PROVEDOR sabe: num feriado a serie mais nova
    que ele devolve e de antes da abertura de hoje. Uma sondagem responde, e o
    job para nas 30 restantes em vez de repetir a coleta de ontem 78 vezes.

    **Nos primeiros `CARENCIA_ABERTURA_S` nao ha veredito de feriado**, e a
    primeira versao disto errava ai: as 9:31 a barra mais nova do provedor
    ainda e a de ontem as 16:00 — que E anterior a abertura de hoje — e o
    portao publicava "feriado" no minuto em que o pregao abriu. O docstring
    prometia o contrario do que o codigo fazia. Na duvida o portao COLETA: o
    erro custa uma rodada de 30 requisicoes e o ciclo seguinte ja recusa,
    enquanto recusar cedo publicaria um motivo falso no log.
    """
    if sessao_de(agora, feriados) != PREGAO:
        return Portao(False, "fora do pregão")
    abertura = abertura_de_hoje(agora)
    if agora - abertura < CARENCIA_ABERTURA_S:
        return Portao(True, "pregão abrindo — ainda sem veredito de feriado")
    if ultima_barra_ts is not None and ultima_barra_ts < abertura:
        idade_h = (agora - ultima_barra_ts) / 3600
        return Portao(False, f"a bolsa não negociou hoje (última barra há "
                             f"{idade_h:.0f} h) — feriado ou meia sessão")
    return Portao(True, "pregão aberto")


__all__ = ["Barra", "Serie", "baixar", "baixar_maior_janela", "agregar",
           "agrupar_por",
           "basis", "INTERVALOS", "IDADE_MAXIMA_H", "JANELAS",
           "Portao", "portao_do_pregao", "abertura_de_hoje",
           "CARENCIA_ABERTURA_S"]
