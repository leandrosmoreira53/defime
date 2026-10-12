"""Manchetes por ativo, de meia em meia hora. Fase N1 do PLAN_NOTICIAS.md.

    python -m defime.jobs.noticias                       # todos os simbolos
    python -m defime.jobs.noticias --simbolos MU,QUBT
    python -m defime.jobs.noticias --listar MU           # le o banco, sem rede

**Zero chamadas de RPC.** Uma requisicao por simbolo no caso normal (Yahoo
busca), com pausa entre elas; as reservas so rodam para quem a primeira nao
respondeu. ~31 requisicoes por ciclo, ~70s com a pausa de 2s.

**Unico lugar com HTTP de noticia por ativo.** O MCP le o que este job grava
em `noticias.json` (Fase N3, `estado_noticias`) e nao busca nada por conta
propria — senao seria a segunda conta de rate limit nos mesmos sites.

**A escada, por simbolo:** Yahoo busca -> Yahoo RSS -> Google News pelo nome.
Toda tentativa vira linha em `noticia_leitura`, inclusive a que falhou: e ela
que permite a pagina dizer "fonte muda desde" em vez de "0 noticias".

**TradingView NAO entra** (decisao D1, 23/09): o endpoint interno funciona, mas
os Termos vedam coleta automatizada e o proprio payload marca licenca de
provedor. O que fica e o link para a pagina do ativo la, sem requisicao.

Unit (a cada 30 min), `systemctl cat uso-noticias` antes de escrever:

    ExecStart=/opt/defime/.venv/bin/python -m defime.jobs.noticias
    OnCalendar=*-*-* *:12,42:00
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

RAIZ = Path.cwd()
os.environ.setdefault("LOG_JSON", "true")

from defime.db.registro import gravar_batimento                   # noqa: E402
from defime.estado import gravar_estado                           # noqa: E402
from defime.logging import get_logger                             # noqa: E402
from defime.macro import noticias_ativos as N                     # noqa: E402
from defime.macro import noticias_banco as B                      # noqa: E402

from defime.jobs.acoes import SIMBOLOS as _SIMBOLOS_BOLSA                  # noqa: E402

log = get_logger(__name__)

BD = str(RAIZ / "defime.db")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) uso-lp-noticias"
TIMEOUT_S = 15
PAUSA_S = 2.0          # entre simbolos; o Yahoo limita por IP e o coletor de cotações
                       # ja divide o mesmo IP

# Os da bolsa sem cripto (ETH-USD e BTC-USD estao la para outra conta), mais a
# SPCX: nao tem acao listada, mas o Yahoo marca materia da SpaceX com o
# ticker — medido em 23/09, 18 de 18 itens com `SPCX` em `relatedTickers`.
SIMBOLOS = [s for s in _SIMBOLOS_BOLSA if not s.endswith("-USD")] + ["SPCX"]

URL_BUSCA = ("https://query1.finance.yahoo.com/v1/finance/search?q={s}"
             "&newsCount=20&quotesCount=0&enableFuzzyQuery=false")
URL_RSS = ("https://feeds.finance.yahoo.com/rss/2.0/headline?s={s}"
           "&region=US&lang=en-US")
URL_GOOGLE = ("https://news.google.com/rss/search?q={q}"
              "&hl=en-US&gl=US&ceid=US:en")


def _get(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
        return r.read().decode("utf-8", "replace")


def _tentar(fonte: str, simbolo: str, busca, montar) -> N.Lida:
    """Rede -> Lida. Excecao de rede vira `ok=False` com o erro, nunca pula."""
    try:
        texto = busca()
    except Exception as exc:  # noqa: BLE001
        return N.Lida(fonte, simbolo, False,
                      erro=f"{type(exc).__name__}: {str(exc)[:160]}")
    return montar(texto)


def ler_simbolo(simbolo: str, universo: set[str], agora: int) -> list[N.Lida]:
    """A escada inteira para um simbolo. Para no primeiro degrau que responde."""
    s = urllib.parse.quote(simbolo)
    lidas = [_tentar("yahoo_busca", simbolo,
                     lambda: _get(URL_BUSCA.format(s=s)),
                     lambda t: N.ler_yahoo_busca(t, simbolo, universo, agora))]
    if lidas[-1].ok:
        return lidas
    lidas.append(_tentar("yahoo_rss", simbolo,
                         lambda: _get(URL_RSS.format(s=s)),
                         lambda t: N.ler_yahoo_rss(t, simbolo, agora)))
    if lidas[-1].ok or simbolo not in N.NOMES:
        return lidas
    nome = N.NOMES[simbolo][0]     # a mesma lista que decide `no_titulo`
    q = urllib.parse.quote_plus(f'"{nome}"')
    lidas.append(_tentar("google_news", simbolo,
                         lambda: _get(URL_GOOGLE.format(q=q)),
                         lambda t: N.ler_google_news(t, simbolo, nome, agora)))
    return lidas


def coletar(c: sqlite3.Connection, simbolos: list[str], *,
            pausa_s: float = PAUSA_S, ler=ler_simbolo) -> dict:
    """Um ciclo. `ler` e injetavel para o teste rodar sem rede."""
    agora = N.agora_utc()
    universo = set(SIMBOLOS) | set(simbolos)
    novas, sem_fonte, por_fonte = 0, [], {}
    for i, sim in enumerate(simbolos):
        if i and pausa_s:
            time.sleep(pausa_s)
        lidas = ler(sim, universo, agora)
        for ld in lidas:
            novas += B.gravar(c, ld, agora)
            por_fonte[ld.fonte] = por_fonte.get(ld.fonte, 0) + int(ld.ok)
        c.commit()
        if not any(ld.ok for ld in lidas):
            sem_fonte.append(sim)
            log.warning("noticias_sem_fonte", simbolo=sim,
                        erros={ld.fonte: ld.erro for ld in lidas})
    return {"simbolos": len(simbolos), "novas": novas,
            "sem_fonte": sem_fonte, "ok_por_fonte": por_fonte}


# O que vai para o MCP. 72 h e nao 24: quem pergunta "o que saiu da MU" numa
# segunda de manha quer ver a sexta. Acima disso, a pagina do grafico (7 d).
JANELA_ESTADO_H = 72.0
LIMITE_ESTADO = 1500        # teto de seguranca do arquivo; ~500 hoje em 72 h


def estado_noticias(c: sqlite3.Connection, agora: int,
                    simbolos: list[str]) -> dict:
    """O `noticias.json` que o MCP le. Mesma `noticias_banco.ler` das paginas.

    Vai a `leitura` de cada simbolo junto, porque sem ela o MCP nao teria
    como separar "nenhuma manchete" de "a fonte ficou muda" — e responderia
    a segunda como se fosse a primeira.
    """
    itens = B.ler(c, agora=agora, horas=JANELA_ESTADO_H, limite=LIMITE_ESTADO)
    return {
        "versao": 1,
        "atualizado_em": agora,
        "janela_h": JANELA_ESTADO_H,
        "cortado_no_limite": len(itens) >= LIMITE_ESTADO,
        "simbolos": sorted(simbolos),
        "leitura": B.ultima_leitura(c),
        "noticias": [{k: m[k] for k in ("titulo", "publisher", "link",
                                        "publicado_em", "fonte", "eventos",
                                        "simbolos", "nomeia", "n_tickers")}
                     for m in itens],
        "fontes": ["yahoo_busca", "yahoo_rss", "google_news"],
        "entra_na_decisao": False,
        "ressalva": ("manchete da ACAO (bolsa), nao do token na chain; "
                     "`simbolos` e a atribuicao da fonte, `nomeia` os que o "
                     "titulo cita — resumo de mercado entra em `simbolos` "
                     "de muita gente"),
        "tradingview": ("fora da coleta (Termos vedam acesso automatizado; "
                        "o payload marca licenca de provedor)"),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--simbolos", default=",".join(SIMBOLOS))
    ap.add_argument("--pausa", type=float, default=PAUSA_S)
    ap.add_argument("--listar", metavar="SIMBOLO",
                    help="imprime as manchetes de 24 h do banco, sem rede")
    ap.add_argument("--horas", type=float, default=24.0)
    args = ap.parse_args()

    # timeout de 60s: catorze processos escrevem neste SQLite, e trava curta e
    # esperar, nao desistir (a licao da LLY).
    c = sqlite3.connect(BD, timeout=60)
    B.preparar(c)

    if args.listar:
        for n in B.ler(c, agora=N.agora_utc(), simbolo=args.listar,
                       horas=args.horas, limite=50):
            ev = f"  [{', '.join(n['eventos'])}]" if n["eventos"] else ""
            print(f"{n['idade_h']:6.1f}h  {n['publisher'] or n['fonte']:<18.18}"
                  f"  {n['titulo'][:90]}{ev}")
        est = B.ultima_leitura(c).get(args.listar.upper())
        print(f"\nleitura: {est}")
        return 0

    t0 = time.time()
    simbolos = [s.strip().upper() for s in args.simbolos.split(",") if s.strip()]
    r = coletar(c, simbolos, pausa_s=args.pausa)
    # O estado sai do BANCO, nao desta rodada: `--simbolos MU` a mao nao pode
    # apagar os outros 26 do MCP. Falha aqui nao derruba o job (gravar_estado
    # nunca levanta).
    gravar_estado("noticias", estado_noticias(
        c, N.agora_utc(), sorted(set(SIMBOLOS) | set(simbolos))))
    dur = time.time() - t0
    ok = len(r["sem_fonte"]) < len(simbolos)
    gravar_batimento("noticias", ok=ok, itens=r["novas"], duracao_s=dur,
                     erro=None if ok else "nenhuma fonte respondeu",
                     nota=(f"sem fonte: {','.join(r['sem_fonte'])}"
                           if r["sem_fonte"] else None))
    print(f"{r['simbolos']} simbolos · {r['novas']} manchetes novas · "
          f"sem fonte: {', '.join(r['sem_fonte']) or 'nenhum'} · "
          f"ok por fonte {r['ok_por_fonte']} · {dur:.0f}s")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
