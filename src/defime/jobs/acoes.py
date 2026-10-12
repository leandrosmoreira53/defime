"""Baixa o OHLCV das acoes na BOLSA e grava no banco. Read-only na chain.

    python -m defime.jobs.acoes                    # todos os simbolos, 1h, 730 dias
    python -m defime.jobs.acoes --simbolos MU,RDDT
    python -m defime.jobs.acoes --intervalo 1d --janela 5y

A referencia de fora que faltava. O projeto tem o preco de DENTRO da chain
desde sempre; sem o de fora nao da para separar "o token andou" de "a acao
andou" — e essa e a pergunta que decide se um movimento de preco na pool
merece resposta ou e ruido de basis.

**Uma requisicao por simbolo cobre 730 dias de barras de 1h.** Medido em
2026-08-31: 5.097 barras da MU em ~2s. O custo do job inteiro e de segundos, e
a cadencia noturna e por outro motivo: de madrugada o dia de pregao ja fechou e
a ultima barra nao muda mais debaixo do banco.

**Grava com `insert or replace`, e essa e a excecao a regra do projeto.**
Medicao NOSSA e datada e nunca se sobrescreve — e por isso que `pending_fees`
tem chave `(pool, token_id, bloco)`. Aqui o dado e do PROVEDOR: a barra da hora
corrente ainda esta se formando e muda a cada leitura, e barras antigas sao
revisadas pela fonte. Congelar a primeira leitura guardaria uma barra pela
metade para sempre.

**Barra sem fechamento e descartada, nunca preenchida.** Buraco do provedor
lido como candle de volume zero vira preco que nao houve — a mesma familia dos
futuros da EIA congelados respondendo 200.
"""

import argparse
import os
import sqlite3
import sys
import time
from pathlib import Path

RAIZ = Path.cwd()
os.environ.setdefault("LOG_JSON", "true")

from defime.db.registro import gravar_batimento                # noqa: E402
from defime.macro import acoes                                 # noqa: E402

BD = str(RAIZ / "defime.db")

# Os tickers das pools que este projeto acompanha. `USO` e o ETF de petroleo,
# os outros sao as acoes tokenizadas do ranking. Escrito aqui e nao
# derivado do nome da pool de proposito: "MU 1% v4" vira `MU`, mas "SPCX" e
# "CRCL" nao sao obviamente tickers de bolsa e um deles pode nem existir la —
# melhor a lista explicita, que falha visivelmente, do que uma heuristica que
# baixa o papel errado em silencio.
SIMBOLOS = ["USO", "MU", "RDDT", "COST", "MSFT", "GME", "SLV", "AAPL",
            "TSLA", "AMZN", "NVDA", "TSM", "MSTR", "GOOGL", "SPY", "QQQ",
            "PLTR", "RBLX", "TTWO", "CRCL", "QUBT", "SNDK",
            # Acrescentados em 22/09 para o heatmap e o grafico. UPS,
            # INTC e JNJ tem pool na chain que este banco NAO indexa, e USAR
            # so listou em 03/2025 — entram pela bolsa, que e o que a pagina
            # declara usar neles.
            "UPS", "INTC", "JNJ", "USAR",
            # **`ETH-USD` nao e acao tokenizada e entra por outro motivo:** e
            # o preco que converte gwei em dolar. Sem ele a sentinela de gas
            # publica o gwei e DECLARA que o dolar falta, em vez de supor um
            # preco — a mesma regra que fez a fee pendente publicar os tokens
            # ao lado do dolar em 07/09.
            "ETH-USD",
            # `BTC-USD`: referencia para converter valores em BTC.
            "BTC-USD"]

# **A coleta de 1m nao inclui cripto, e nao por gosto.** O portao do pregao
# fecha fora de 9:30-16:00 ET; `ETH-USD` e `BTC-USD` negociam 24/7 e ficariam
# com a barra do minuto so durante o pregao americano, que e a pior amostra
# possivel de um ativo continuo. Eles entram pelo 1h de sempre, e o heatmap da
# bolsa ja os exclui por causa da unidade do volume.
SIMBOLOS_BOLSA = [s for s in SIMBOLOS if not s.endswith("-USD")]

ESQUEMA = """
create table if not exists acoes_ohlc(
    simbolo   text    not null,
    intervalo text    not null,
    ts        integer not null,          -- inicio da barra, epoch UTC
    abertura  real, maxima real, minima real, fechamento real,
    volume    integer,
    primary key (simbolo, intervalo, ts));
create index if not exists ix_acoes on acoes_ohlc(simbolo, intervalo, ts desc);

create table if not exists acoes_coleta(
    simbolo text not null, intervalo text not null,
    ts integer not null, barras integer, novas integer,
    ok integer, motivo text,
    primary key (simbolo, intervalo, ts));
"""


def gravar(c: sqlite3.Connection, s) -> int:
    antes = c.execute("select count(*) from acoes_ohlc where simbolo=? and "
                      "intervalo=?", (s.simbolo, s.intervalo)).fetchone()[0]
    c.executemany(
        "insert or replace into acoes_ohlc values (?,?,?,?,?,?,?,?)",
        [(s.simbolo, s.intervalo, b.ts, b.abertura, b.maxima, b.minima,
          b.fechamento, b.volume) for b in s.barras])
    c.commit()
    depois = c.execute("select count(*) from acoes_ohlc where simbolo=? and "
                       "intervalo=?", (s.simbolo, s.intervalo)).fetchone()[0]
    return depois - antes


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--simbolos", default=",".join(SIMBOLOS))
    ap.add_argument("--intervalo", default="1h")
    ap.add_argument("--janela", default="auto",
                    help="'auto' tenta da maior para a menor; a RDDT nao tem "
                         "730 dias e o provedor responde 422, nao lista vazia")
    ap.add_argument("--pausa", type=float, default=1.5,
                    help="segundos entre simbolos; o provedor limita por IP")
    ap.add_argument("--sem-cripto", action="store_true",
                    help="tira ETH-USD e BTC-USD; eles negociam 24/7 e nao "
                         "cabem numa coleta restrita ao pregao")
    ap.add_argument("--podar-dias", type=float, default=0.0,
                    help="apaga barras DESTE intervalo mais velhas que N "
                         "dias; para o 1m, que so serve como 'agora'")
    ap.add_argument("--so-no-pregao", action="store_true",
                    help="sai sem tocar a rede fora de 9:30-16:00 ET, e para "
                         "na primeira sondagem se for feriado (ver "
                         "acoes.portao_do_pregao)")
    args = ap.parse_args()
    if args.sem_cripto and args.simbolos == ",".join(SIMBOLOS):
        args.simbolos = ",".join(SIMBOLOS_BOLSA)

    t0 = time.time()
    agora = int(t0)
    # **Portao de sessao: primeiro sem rede, depois com uma sondagem so.**
    # Fora do pregao nao ha barra de 1m nova e o job nao existe; num feriado o
    # provedor responde 200 com a serie de ontem, e esse e o modo de falhar
    # que este projeto ja pagou varias vezes (a cota do Alpha Vantage, os
    # futuros da EIA congelados). A sondagem usa o PRIMEIRO simbolo da lista,
    # que seria baixado de qualquer jeito — custo zero quando o portao abre.
    if args.so_no_pregao:
        p = acoes.portao_do_pregao(agora)
        if not p.coletar:
            print(f"portão fechado: {p.motivo}")
            gravar_batimento("acoes", itens=0, duracao_s=0.0, ok=True,
                             nota=f"intervalo={args.intervalo} "
                                  f"portão fechado: {p.motivo}")
            return 0

    # timeout de 60s e nao o padrao de 5s: com a coleta HORARIA do pregao
    # (uso-acoes-pregao) este job passa a escrever no meio do dia, junto com
    # os catorze escritores do banco. Trava curta e esperar, nao desistir — a
    # mesma licao da LLY que perdeu 20 minutos num `database is locked`.
    c = sqlite3.connect(BD, timeout=60)
    c.executescript(ESQUEMA)
    c.commit()

    ok = falhas = novas_total = 0
    for i, sim in enumerate(s.strip().upper()
                            for s in args.simbolos.split(",") if s.strip()):
        serie = (acoes.baixar_maior_janela(sim, args.intervalo)
                 if args.janela == "auto"
                 else acoes.baixar(sim, args.intervalo, args.janela))
        if args.so_no_pregao and i == 0 and serie.barras:
            p = acoes.portao_do_pregao(agora, serie.barras[-1].ts)
            if not p.coletar:
                c.close()
                print(f"portão fechado na sondagem ({sim}): {p.motivo}")
                gravar_batimento("acoes", itens=0,
                                 duracao_s=time.time() - t0, ok=True,
                                 nota=f"intervalo={args.intervalo} "
                                      f"portão fechado: {p.motivo}")
                return 0
        novas = gravar(c, serie) if serie.barras else 0
        novas_total += novas
        c.execute("insert or replace into acoes_coleta values (?,?,?,?,?,?,?)",
                  (sim, args.intervalo, int(t0), len(serie.barras), novas,
                   int(serie.disponivel), serie.motivo))
        c.commit()
        if serie.disponivel:
            ok += 1
            print(f"  {sim:6} {len(serie.barras):5d} barras  "
                  f"+{novas:4d} novas  "
                  f"{serie.barras[0].quando:%Y-%m-%d} .. "
                  f"{serie.barras[-1].quando:%Y-%m-%d}  "
                  f"${serie.barras[-1].fechamento:,.2f}")
        else:
            falhas += 1
            print(f"  {sim:6} INDISPONIVEL: {serie.motivo}")
        if args.pausa:
            time.sleep(args.pausa)
    # **A poda so existe para o 1m, e o motivo e tamanho.** Sao ~10 mil
    # linhas por dia de pregao nos 26 simbolos; em um ano seriam 3,6 milhoes
    # para responder uma pergunta que olha os ultimos minutos. As barras de
    # 1h continuam intocadas — e delas que saem os indicadores, a
    # sobrevivencia e o basis, e ali historia e o produto.
    podadas = 0
    if args.podar_dias > 0:
        corte = int(t0 - args.podar_dias * 86400)
        podadas = c.execute("delete from acoes_ohlc where intervalo = ? "
                            "and ts < ?", (args.intervalo, corte)).rowcount
        c.commit()
        if podadas:
            print(f"  podadas {podadas} barras de {args.intervalo} "
                  f"anteriores a {args.podar_dias:g} dias")
    c.close()

    gravar_batimento("acoes", itens=ok, duracao_s=time.time() - t0,
                     ok=falhas == 0,
                     erro=f"{falhas} simbolo(s) indisponivel(is)" if falhas
                     else None,
                     nota=f"intervalo={args.intervalo} novas={novas_total}"
                          + (f" podadas={podadas}" if podadas else ""))
    print(f"\n{ok} ok · {falhas} falha(s) · {novas_total} barras novas · "
          f"{time.time() - t0:.0f}s")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
