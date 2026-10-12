"""Manchetes POR ATIVO — parse, dedupe e marcacao de evento. Puro: sem rede.

Fase N1 do [PLAN_NOTICIAS.md](../../../PLAN_NOTICIAS.md). Quem busca e grava e
`jobs/noticias.py`; quem le o banco e `macro/noticias_banco.py`. Aqui so se
transforma texto de fonte em `Item`, e por isso tudo aqui e testavel offline
sobre as fixtures gravadas em `tests/fixtures/noticias/`.

**Leitura, nao decisao.** Nada daqui entra em gate, sigma ou score: o unico
caminho de noticia ate a decisao continua sendo o `choque` de `macro/news.py`,
e ha teste de AST reprovando `decision/` que importe este modulo.

---

**A escada por ativo, medida em 23/09/2026 e nao suposta:**

1. **Yahoo busca JSON** (`/v1/finance/search?q=MU&newsCount=20`) — uma
   chamada por ticker, 20 itens, e cada item traz `relatedTickers`: a
   atribuicao sai da FONTE, nao de casar o nome na manchete. Medido: MU, QUBT,
   USAR, SPY e SPCX com 20/20 (18/18 na SPCX) itens marcados com o proprio
   simbolo.
2. **Yahoo RSS por ticker** — reserva. Sem `relatedTickers`: o item vai so para
   o simbolo pedido.
3. **Google News RSS pelo NOME da empresa** — ultimo degrau, e FILTRADO: a
   busca por "USA Rare Earth" devolveu 100 itens de terras raras em geral. So
   entra manchete que contem o nome.

**O RSS do Yahoo com varios tickers NAO serve**, e o plano dizia que servia:
`s=MU,RBLX,QUBT` devolveu os mesmos 19 itens de `s=MU` — so o primeiro ticker.
Tres chamadas por ciclo viraram trinta.

**Zero itens e "sem leitura", nunca "sem noticia".** O Google News ja
respondeu 200 com RSS valido e nenhum item para "Micron" (03/09). `Lida.ok`
fica falso com o motivo, e quem publica diz "fonte muda", nunca "0".

**Marcacao de evento reusa o vocabulario de `news.py`** — nao ha segunda lista
de termos com peso. Diferenca declarada: aqui o casamento e por PALAVRA
inteira (`\\b`), la e por substring. La isso importa menos porque o `assunto`
filtra antes; aqui cada ativo tem vocabulario e manchete generica, e `ban`
casaria em "bank". A marca so destaca, nao pontua.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .news import TERMOS, TERMOS_JOGOS, TERMOS_SEMICONDUTORES

# Feed cujo item mais novo passa disso nao e feed vivo. Generoso de proposito:
# acao pequena fica dias sem materia (a USAR, a QUBT), e isso e silencio da
# empresa, nao da fonte.
IDADE_MAXIMA_H = 14 * 24.0

# Vocabulario por ativo. Os tres dicionarios de `news.py` para quem tem perfil;
# o GERAL e um recorte deles, so com eventos DE EMPRESA — e cada termo tem de
# existir em algum dos tres (teste), para nao nascer uma quarta lista.
_SEMIS = {"MU", "NVDA", "TSM", "SNDK", "INTC"}
_JOGOS = {"RBLX", "TTWO"}
_GERAL_CHAVES = ("guidance", "downgrade", "analyst upgrade", "beats", "misses",
                 "recall", "price target", "class action", "stock offering",
                 "buyback", "data breach", "short report", "outage")
_TODOS = {**TERMOS, **TERMOS_SEMICONDUTORES, **TERMOS_JOGOS}
TERMOS_GERAL = {k: _TODOS[k] for k in _GERAL_CHAVES}


def perfil_do_simbolo(simbolo: str) -> dict[str, float]:
    s = simbolo.upper()
    if s == "USO":
        return TERMOS
    if s in _SEMIS:
        return TERMOS_SEMICONDUTORES
    if s in _JOGOS:
        return TERMOS_JOGOS
    return TERMOS_GERAL


def termos_do_simbolo(simbolo: str) -> dict[str, float]:
    """Perfil do ativo UNIDO ao GERAL. Medido na primeira coleta ao vivo:
    "Citi Resets Micron Target" nao marcava, porque o perfil de
    semicondutores nao tem `price target` — evento de empresa vale para toda
    empresa. A uniao nao respeita o invariante de substring (`upgrade` e
    `analyst upgrade`), e aqui nao precisa: a marca e uma LISTA, nao soma."""
    return {**TERMOS_GERAL, **perfil_do_simbolo(simbolo)}


# Como a manchete chama cada ativo. Serve a duas coisas: o ultimo degrau
# (Google News busca pelo PRIMEIRO nome) e `no_titulo` — a materia e DA
# empresa ou so a cita? Medido na primeira coleta: "General Mills Reaffirms
# Full-Year Guidance" veio marcada pelo Yahoo com NVDA. A atribuicao da fonte
# e mantida; o que muda e que o destaque de evento so vale para o ativo que
# a manchete NOMEIA. O ticker casa em MAIUSCULA e palavra inteira ("(MU)"),
# o nome em qualquer caixa — por isso UPS nao tem "UPS" nos nomes ("ups and
# downs") e MSTR nao tem "Strategy" ("investment strategy").
NOMES = {
    "USO": ("United States Oil Fund", "oil", "crude"),
    "MU": ("Micron",), "RDDT": ("Reddit",), "COST": ("Costco",),
    "MSFT": ("Microsoft",), "GME": ("GameStop",),
    "SLV": ("iShares Silver", "silver"), "AAPL": ("Apple",),
    "TSLA": ("Tesla",), "AMZN": ("Amazon",), "NVDA": ("Nvidia",),
    "TSM": ("TSMC", "Taiwan Semiconductor"),
    "MSTR": ("MicroStrategy",),
    "GOOGL": ("Alphabet", "Google"), "SPY": ("S&P 500",),
    "QQQ": ("Nasdaq 100", "Nasdaq"), "PLTR": ("Palantir",),
    "RBLX": ("Roblox",), "TTWO": ("Take-Two",), "CRCL": ("Circle",),
    "QUBT": ("Quantum Computing Inc", "Quantum Computing"),
    "SNDK": ("SanDisk",), "UPS": ("United Parcel Service",),
    "INTC": ("Intel",), "JNJ": ("Johnson & Johnson", "J&J"),
    "USAR": ("USA Rare Earth",), "SPCX": ("SpaceX",),
}


def nomeia(titulo: str, simbolo: str) -> bool:
    s = simbolo.upper()
    if re.search(rf"(?<![A-Za-z]){re.escape(s)}(?![A-Za-z])", titulo or ""):
        return True
    norm = normalizar_titulo(titulo)
    return any(re.search(rf"\b{re.escape(normalizar_titulo(n))}\b", norm)
               for n in NOMES.get(s, ()))


@dataclass
class Item:
    fonte: str
    titulo: str
    link: str
    publicado_em: int                   # epoch UTC
    publisher: str | None = None
    simbolos: tuple[str, ...] = ()      # os do NOSSO universo
    n_tickers: int = 0                  # quantos a fonte marcou, no total
    eventos: tuple[str, ...] = ()
    no_titulo: tuple[str, ...] = ()     # dos `simbolos`, os que a manchete NOMEIA

    @property
    def id(self) -> str:
        return id_noticia(self.link)

    @property
    def titulo_norm(self) -> str:
        return normalizar_titulo(self.titulo)


@dataclass
class Lida:
    """O resultado de UMA leitura de uma fonte para um simbolo."""
    fonte: str
    simbolo: str
    ok: bool
    itens: list[Item] = field(default_factory=list)
    erro: str | None = None


# ---------------------------------------------------------------- normalizar

def normalizar_titulo(titulo: str) -> str:
    """Chave de dedupe: a mesma materia em dois feeds vira UMA linha."""
    t = unicodedata.normalize("NFKD", html.unescape(titulo or ""))
    t = "".join(c for c in t if not unicodedata.combining(c)).lower()
    t = re.sub(r"[^a-z0-9]+", " ", t)
    return " ".join(t.split())


_RASTREIO = ("utm_", ".tsrc", "guccounter", "ncid", "soc_")


def link_canonico(url: str) -> str:
    """Tira parametro de rastreio e fragmento; o resto do link fica igual."""
    p = urlsplit((url or "").strip())
    q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
         if not k.lower().startswith(_RASTREIO)]
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path,
                       urlencode(q), ""))


def id_noticia(link: str) -> str:
    return hashlib.sha1(link_canonico(link).encode()).hexdigest()[:16]


def eventos(titulo: str, simbolo: str) -> tuple[str, ...]:
    baixo = normalizar_titulo(titulo)
    return tuple(sorted(t for t in termos_do_simbolo(simbolo)
                        if re.search(rf"\b{re.escape(t)}\b", baixo)))


def _nomeados(titulo: str, simbolos) -> tuple[str, ...]:
    return tuple(s for s in simbolos if nomeia(titulo, s))


def _vivo(fonte: str, simbolo: str, itens: list[Item], agora: int) -> Lida:
    """Zero itens ou feed parado viram `ok=False` COM o motivo."""
    if not itens:
        return Lida(fonte, simbolo, False,
                    erro="fonte respondeu sem itens (sem leitura, nao "
                         "'sem noticia')")
    idade_h = (agora - max(i.publicado_em for i in itens)) / 3600.0
    if idade_h > IDADE_MAXIMA_H:
        return Lida(fonte, simbolo, False, itens=[],
                    erro=f"item mais novo tem {idade_h / 24:.1f} dias")
    return Lida(fonte, simbolo, True, itens=itens)


# ---------------------------------------------------------------- Yahoo busca

def ler_yahoo_busca(texto: str, simbolo: str, universo: set[str],
                    agora: int) -> Lida:
    """JSON do `/v1/finance/search`. Fica so o item que a FONTE marcou com o
    simbolo pedido; e ele vai tambem para os outros simbolos do NOSSO universo
    que a fonte marcou (uma materia de chips cita MU, NVDA e SNDK)."""
    s = simbolo.upper()
    try:
        noticias = json.loads(texto).get("news") or []
    except (ValueError, AttributeError) as exc:
        return Lida("yahoo_busca", s, False, erro=f"JSON ilegivel: {exc}"[:200])
    itens = []
    for n in noticias:
        rel = [str(t).upper() for t in (n.get("relatedTickers") or [])]
        if s not in rel or not n.get("title") or not n.get("link"):
            continue
        quando = n.get("providerPublishTime")
        if not isinstance(quando, (int, float)):
            continue
        nossos = tuple(sorted({s} | (set(rel) & universo)))
        titulo = html.unescape(n["title"]).strip()
        itens.append(Item("yahoo_busca", titulo, link_canonico(n["link"]),
                          int(quando), n.get("publisher"), nossos,
                          len(set(rel)), eventos(titulo, s),
                          _nomeados(titulo, nossos)))
    return _vivo("yahoo_busca", s, itens, agora)


# ---------------------------------------------------------------- RSS

def _itens_rss(xml: str) -> list[dict]:
    import xml.etree.ElementTree as ET
    from email.utils import parsedate_to_datetime

    try:
        raiz = ET.fromstring(xml)
    except ET.ParseError:
        return []
    saida = []
    for no in raiz.iter("item"):
        d = {f.tag.rsplit("}", 1)[-1]: (f.text or "").strip() for f in no}
        try:
            q = parsedate_to_datetime(d.get("pubDate", ""))
        except (TypeError, ValueError):
            continue
        if q.tzinfo is None:
            q = q.replace(tzinfo=timezone.utc)
        d["_ts"] = int(q.timestamp())
        saida.append(d)
    return saida


def ler_yahoo_rss(xml: str, simbolo: str, agora: int) -> Lida:
    s = simbolo.upper()
    itens = []
    for d in _itens_rss(xml):
        if not d.get("title") or not d.get("link"):
            continue
        titulo = html.unescape(d["title"])
        itens.append(Item("yahoo_rss", titulo, link_canonico(d["link"]),
                          d["_ts"], None, (s,), 1, eventos(titulo, s),
                          _nomeados(titulo, (s,))))
    return _vivo("yahoo_rss", s, itens, agora)


def ler_google_news(xml: str, simbolo: str, nome: str, agora: int) -> Lida:
    """Filtrado pelo NOME: a busca do Google devolve o assunto, nao a empresa.

    O titulo vem com " - Veiculo" no fim; o veiculo vira `publisher` e sai do
    titulo, senao a mesma materia do Yahoo nao deduplicaria.
    """
    s, alvo = simbolo.upper(), normalizar_titulo(nome)
    itens = []
    for d in _itens_rss(xml):
        titulo, fonte = html.unescape(d.get("title", "")), d.get("source")
        if fonte and titulo.endswith(" - " + fonte):
            titulo = titulo[: -len(" - " + fonte)]
        if not titulo or not d.get("link"):
            continue
        if not re.search(rf"\b{re.escape(alvo)}\b", normalizar_titulo(titulo)):
            continue
        itens.append(Item("google_news", titulo.strip(), link_canonico(d["link"]),
                          d["_ts"], fonte or None, (s,), 1,
                          eventos(titulo, s), (s,)))
    return _vivo("google_news", s, itens, agora)


def agora_utc() -> int:
    return int(datetime.now(timezone.utc).timestamp())


__all__ = ["Item", "Lida", "IDADE_MAXIMA_H", "TERMOS_GERAL", "NOMES", "termos_do_simbolo",
           "perfil_do_simbolo", "nomeia",
           "normalizar_titulo", "link_canonico", "id_noticia", "eventos",
           "ler_yahoo_busca", "ler_yahoo_rss", "ler_google_news", "agora_utc"]
