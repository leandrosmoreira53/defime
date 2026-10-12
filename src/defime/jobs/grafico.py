"""A pagina `/grafico/` — candles e indicadores da acao na BOLSA.

    python -m defime.jobs.grafico                       # MU, o default
    python -m defime.jobs.grafico --simbolo RDDT
    python -m defime.jobs.grafico --simbolos MU,USO,RDDT --saida ./site

**Zero chamadas de RPC e zero rede.** Tudo sai de `acoes_ohlc`, que o
`python -m defime.jobs.acoes` ja coleta as 03:20 UTC. Este job so le, calcula os indicadores
em `macro/indicadores.py` e escreve HTML — se ele falhar, nada de producao
para.

**Seis timeframes (1h, 4h, 1d, 3d, 5d, 1sem) e so um deles e coletado.** O
Yahoo nao tem 4h (os aceitos sao 1m..1h, 1d, 1wk, 1mo) e o banco so guarda 1h.
Todo o resto sai de `acoes.agregar`/`agrupar_por`, sempre na cascata
1h -> 1d -> N, e a agregacao **quebra no fim do pregao**: sem isso um bloco
atravessaria o fechamento e a barra rotulada 4h carregaria as ~17h de mercado
fechado no meio, com a maxima e a minima de dois dias diferentes e o gap de
abertura escondido dentro do candle.

O diario usa `por=99`, que nunca e atingido num pregao de 6h30 — entao cada
bloco e exatamente um dia. E como a coleta roda com `includePrePost=false`,
esse diario e o do **pregao regular**, nao o dia de calendario.

---

**O que esta pagina NAO responde.** Ela desenha o preco da BOLSA. A faixa de
LP vive no preco da POOL, e o basis entre os dois tem desvio de 1,42% e chega
a 3% — um nivel tracado aqui chega na pool deslocado. Para decidir largura e
centro continuam valendo `/tecnica/` (sobrevivencia, eficiencia, poder da
tendencia) e `/compor/` (o `dentro %` medido swap a swap na propria pool).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

RAIZ = Path.cwd()
ESTILO = Path(__file__).resolve().parents[1] / "web" / "estilo.css"
os.environ.setdefault("LOG_JSON", "true")

from defime.db.registro import gravar_batimento                  # noqa: E402
from defime.estado import diretorio as estado_dir                # noqa: E402
from defime.estado import gravar_estado                          # noqa: E402
from defime.logging import get_logger                            # noqa: E402
from defime.macro.acoes import (Barra, agregar,                  # noqa: E402
                                agrupar_por)
from defime.macro import resumo_tecnico as RT                    # noqa: E402
from defime.macro.tecnica import POR_QUE_NAO                     # noqa: E402
from defime.macro import noticias_banco as NB                    # noqa: E402

from defime.web import grafico_render as G                                       # noqa: E402
from defime.web import noticias_render as NR                                     # noqa: E402

log = get_logger(__name__)

BD = str(RAIZ / "defime.db")
SAIDA = Path(os.environ.get("GRAFICO_SAIDA", RAIZ / "site" / "grafico"))

# `por=99` nunca e atingido num pregao de 6h30, entao `quebrar_no_dia` fecha
# exatamente um bloco por dia. Escrito assim, e nao `por=7`, porque um dia com
# 8 barras (feriado meia-sessao invertido, mudanca de horario) quebraria em
# dois e a barra "diaria" viraria duas.
POR_DIA = 99

TIMEFRAMES = ("1h", "4h", "1d", "3d", "5d", "1sem")

# Quantas barras de 1h a pagina carrega. **E corte de PESO, nao de dado**, e
# ele vai publicado no rodape: com tres anos inteiros o JSON do 1h sozinho da
# 919 KB (70% da pagina), e o celular paga isso em parse antes de desenhar o
# primeiro candle. 2.000 barras sao ~14 meses de pregao — quem quer 2023 olha
# no 4h ou no diario, que ficam INTEIROS, que e como se faz.
#
# O corte acontece depois do calculo dos indicadores (`_serie_de(ultimas=)`),
# entao a SMA 200 e o canal de 144 ja estao aquecidos no primeiro candle
# visivel.
JANELA_1H = int(os.environ.get("GRAFICO_BARRAS_1H", "2000"))


def barras_do_banco(bd: str, simbolo: str,
                    intervalo: str = "1h") -> list[Barra]:
    """As barras de 1h como `acoes.Barra`, em ordem.

    Barra sem fechamento nao existe no banco: o `run_acoes` ja descarta na
    coleta, porque preencher com o anterior inventaria preco que nao houve.
    """
    c = sqlite3.connect(bd)
    try:
        linhas = c.execute(
            "select ts, abertura, maxima, minima, fechamento, volume"
            "  from acoes_ohlc where simbolo = ? and intervalo = ?"
            " order by ts", (simbolo, intervalo)).fetchall()
    finally:
        c.close()
    return [Barra(ts=int(r[0]), abertura=float(r[1]), maxima=float(r[2]),
                  minima=float(r[3]), fechamento=float(r[4]),
                  volume=int(r[5] or 0)) for r in linhas
            if r[4] is not None]


def series_de(barras: list[Barra], janela_1h: int | None = None) -> dict:
    """Os seis timeframes. So o 1h e coletado; o resto e agregado aqui.

    **3d e 5d contam PREGOES; 1sem conta CALENDARIO, e eles nao sao a mesma
    coisa.** Uma semana cheia tem 5 pregoes e as duas colunas coincidem; na
    semana de feriado o bloco de calendario fecha com 4 e o de contagem
    continua juntando de cinco em cinco, atravessando o feriado. Publicar um
    com o nome do outro seria rotular errado — a familia do `(EIA quarta
    10:30 ET)` literal na f-string.

    A cascata e sempre 1h -> 1d -> N, nunca 1h -> N direto: agregar horas de
    cinco em cinco sem passar pelo dia juntaria o fim de um pregao com o
    comeco do seguinte, escondendo o overnight dentro do candle. E o mesmo
    motivo pelo qual `agregar` quebra no dia.

    So o 1h leva corte de janela: e ele que pesa (70% do JSON com tres anos),
    e e nele que a historia antiga menos serve — ninguem le RSI de 1h de 2023.
    Os longos ficam INTEIROS, e sao eles que respondem "e os tres anos?".
    """
    janela_1h = JANELA_1H if janela_1h is None else janela_1h
    diarias = agregar(barras, por=POR_DIA, quebrar_no_dia=True)
    return {
        "1h": G._serie_de(barras, ultimas=janela_1h),
        "4h": G._serie_de(agregar(barras, por=4, quebrar_no_dia=True)),
        "1d": G._serie_de(diarias),
        "3d": G._serie_de(agregar(diarias, por=3, quebrar_no_dia=False)),
        "5d": G._serie_de(agregar(diarias, por=5, quebrar_no_dia=False)),
        "1sem": G._serie_de(agrupar_por(diarias, _semana)),
    }


def _semana(b: Barra):
    """Chave de semana ISO. Segunda a domingo, e a virada de ano cai certa —
    `isocalendar()` resolve 29/12 a 04/01 sem o bloco quebrar em dois."""
    return b.quando.isocalendar()[:2]


def destino_de(saida: str, simbolos: list[str], simbolo: str) -> Path:
    """**O primeiro simbolo mora na RAIZ da saida**, os outros em subpasta.

    Com `--saida ./site/grafico --simbolos MU`, a pagina responde em
    `/grafico/` e nao em `/grafico/mu/`. Uma pasta so com um arquivo dentro
    obrigaria quem abre `/grafico/` a ver o listing do nginx ou um redirect —
    e o link que o usuario guarda e o curto.
    """
    p = Path(saida)
    return p if simbolo == simbolos[0] else p / simbolo.lower()


def _abas(simbolos: list[str], simbolo: str) -> str:
    """Os links entre as paginas, relativos a QUEM esta desenhando.

    O primeiro esta na raiz e os outros um nivel abaixo, entao o caminho de
    volta muda: escrever `../<sym>/` sempre quebraria a aba da raiz, e
    `<sym>/` sempre quebraria as de dentro.
    """
    if len(simbolos) < 2:
        return ""
    na_raiz = simbolo == simbolos[0]
    partes = []
    for s in simbolos:
        if s == simbolo:
            partes.append(f"<b>{G._e(s)}</b>")
        elif s == simbolos[0]:
            partes.append(f'<a href="{"" if na_raiz else "../"}">{G._e(s)}</a>')
        else:
            alvo = ("" if na_raiz else "../") + s.lower() + "/"
            partes.append(f'<a href="{alvo}">{G._e(s)}</a>')
    return " · ".join(partes)


def noticias_bloco(bd: str, simbolo: str, agora: dt.datetime) -> str:
    """Bloco "Noticias" (PLAN_NOTICIAS N2), pela MESMA `noticias_banco.ler`
    do `/heat/`. Sete dias, porque a pagina e gerada uma vez por dia e 24 h
    deixaria a lista vazia na metade do tempo. Banco sem as tabelas nao
    derruba a pagina: o bloco sai com o motivo."""
    ts = int(agora.timestamp())
    try:
        c = sqlite3.connect(f"file:{bd}?mode=ro", uri=True, timeout=30)
        try:
            ms = NB.ler(c, agora=ts, simbolo=simbolo, horas=24 * 7, limite=60)
            leit = NB.ultima_leitura(c).get(simbolo)
        finally:
            c.close()
    except sqlite3.Error as exc:
        return (f'<div class="sec"><h2>Notícias</h2></div><p class="nota">'
                f'notícias indisponíveis: {G._e(exc)}</p>')
    return (NR.lista(simbolo, ms, NR.estado(simbolo, ms, leit))
            + f'<p class="nota" style="opacity:.7">lista de '
              f'{agora.strftime("%d/%m %H:%M")} UTC — esta página é gerada '
              f'uma vez por dia; as manchetes de agora estão no '
              f'<a href="/heat/">/heat/</a>.</p>')


def montar(simbolo: str, barras: list[Barra], css: str = "",
           agora: dt.datetime | None = None, outros=(),
           series: dict | None = None, noticias: str = "") -> str:
    agora = agora or dt.datetime.now(dt.UTC)
    # `series` vem pronto quando o chamador ja o calculou para o estado do
    # MCP: e o MESMO dicionario nas duas saidas, e calcula-lo duas vezes
    # dobraria o tempo do job sem mudar um numero.
    series = series if series is not None else series_de(barras)
    idade = G._idade(barras, agora)
    ult = barras[-1] if barras else None
    prim = barras[0] if barras else None

    abas = _abas(list(outros), simbolo) if outros else ""

    de_ate = ""
    if prim and ult:
        de_ate = (f'{prim.quando.date().strftime("%d/%m/%Y")} a '
                  f'{ult.quando.date().strftime("%d/%m/%Y")}')

    preco = (f"${ult.fechamento:,.2f}".replace(",", "@").replace(".", ",")
             .replace("@", ".")) if ult else "—"

    return f"""<!doctype html><html lang="pt-BR" data-theme="dark">
<head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{G._e(simbolo)} — grafico</title>
<style>{css}{G.CSS}{NR.CSS}{_CSS}</style></head><body>
<div class="w">
<p class="eyebrow"><a href="../">defime</a>{(" · " + abas) if abas else ""}</p>
<div class="hd"><div><h1>{G._e(simbolo)} · {preco}</h1>
<p class="sub">bolsa (Nasdaq/NYSE via Yahoo) · barras de 1 h desde
{de_ate} · 4 h, diário, 3 d, 5 d e semanal agregados aqui</p></div></div>

{G.bloco(simbolo, series, idade)}
{noticias}

<div class="sec"><h2>O que este gráfico é, e o que ele não decide</h2></div>
<p class="nota"><b>Este é o preço da BOLSA.</b> A faixa de LP vive no preço da
<b>pool</b>, e o basis entre os dois tem desvio de <b>1,42%</b>, com p5/p95 em
−2,89%/+1,72% — medido em 4.348 swaps da MU pareados com a barra de 1 h da
bolsa. Um nível traçado aqui chega na pool deslocado por até 3%. Para
dimensionar faixa, o número que responde é o <code>dentro %</code> medido swap
a swap na própria pool (página de composição do painel).</p>

<p class="nota"><b>RSI, MACD e cruzamento de médias estão reprovados para
decidir faixa neste projeto</b>, e continuam reprovados — o
<code>POR_QUE_NAO</code> de <code>macro/tecnica.py</code> diz por quê:
sobrecompra é uma afirmação sobre o próximo movimento, e o que muda a borda é
a autocorrelação, que mede a mesma intuição sem o limiar arbitrário de 70/30.
Eles estão aqui como <b>leitura</b>: nenhum número desta página alimenta
decisão automática.</p>

<p class="nota"><b>Os períodos vão escritos na legenda de propósito.</b>
Indicador sem período declarado é um número que ninguém consegue conferir. O
RSI é o de Wilder (suavização 1/n, não 2/(n+1)); as bandas de Bollinger usam
desvio <b>populacional</b>, como as plataformas; o Donchian lê a máxima e a
mínima, não o fechamento, porque a posição sai de range no <b>toque</b>; e o
Gaussian Channel é o filtro de Ehlers de 4 polos com banda de True Range
filtrado — por isso ele alarga em barra de amplitude grande mesmo sem o
fechamento andar, que é o que o distingue do Bollinger.</p>

<p class="nota"><b>O eixo horizontal é o número da barra.</b> Com tempo real,
as 17 h de mercado fechado abririam um vão maior que o pregão inteiro e a
série viraria uma fileira de tracinhos. A consequência: <b>a distância
horizontal entre dois candles não é tempo</b> — o vão entre a última barra de
uma sexta e a primeira de uma segunda desenha igual ao de uma hora.</p>

<p class="nota">Os indicadores são calculados no <b>Python</b>
(<code>macro/indicadores.py</code>, puro e testado) e chegam prontos ao
navegador, que só desenha. Calcular no JS seria uma segunda implementação da
mesma matemática, e este projeto já pagou por isso: o <code>compor.js</code>
precisou de um portão rodando as duas versões sobre a mesma grade, porque duas
implementações divergem no primeiro ajuste e o sintoma é a página e a CLI
discordando sem ninguém saber qual está certa.</p>

<p class="nota" style="opacity:.75">gerado em
{agora.strftime("%d/%m/%Y %H:%M")} UTC · última barra
{G._e((idade["ultima"] or "—")[:16].replace("T", " "))} UTC</p>
</div></body></html>"""


def estado_tecnico(estados: dict[str, dict], agora: int,
                   anterior: dict | None = None) -> dict:
    """O `tecnica.json` que o MCP le: o fim de cada serie, por simbolo.

    **Mescla com o anterior.** Rodar `--simbolos MU` a mao nao pode apagar os
    outros 29 do MCP. Cada simbolo carrega o proprio `calculado_em`, entao o
    que ficou do anterior continua dizendo de quando e.
    """
    simbolos = dict((anterior or {}).get("simbolos") or {})
    simbolos.update(estados)
    return {
        "versao": 1,
        "atualizado_em": agora,
        "simbolos": simbolos,
        "parametros": RT.PARAMETROS,
        "derivados": RT.DERIVADOS,
        "entra_na_decisao": False,
        "por_que_nao": POR_QUE_NAO,
        "ressalva": ("preco da BOLSA; a faixa de LP vive no preco da POOL e o basis "
                     "entre os dois tem desvio de 1,42% (p5/p95 -2,89%/+1,72%, MU)"),
    }


def _estado_anterior() -> dict | None:
    d = estado_dir()
    p = d / "tecnica.json" if d else None
    if not p or not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8")).get("dados")
    except Exception as exc:                                  # noqa: BLE001
        # Anterior ilegivel nao impede gravar o novo: perde-se so a mescla,
        # e isso vai para o log.
        log.warning("tecnica_anterior_ilegivel", erro=str(exc))
        return None


_CSS = """
h1{font-size:26px;margin:0 0 4px} .sub{color:var(--ink2);margin:0;font-size:13px}
.eyebrow{font-size:12px;color:var(--ink3);margin:0 0 14px}
.eyebrow a{color:var(--accent);text-decoration:none}
.sec{margin:26px 0 10px;border-bottom:1px solid var(--rule)}
.sec h2{font-size:15px;margin:0 0 8px;font-weight:600}
.nota{font-size:13px;color:var(--ink2);margin:10px 0;max-width:80ch}
.nota code{background:var(--rule2);padding:1px 4px;border-radius:3px;
  font-family:"IBM Plex Mono",ui-monospace,monospace;font-size:12px}
.nota.warn{color:var(--warn);background:var(--warn-bg);padding:10px 12px;
  border-radius:8px;border:1px solid var(--warn);max-width:none}
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--simbolos", default="MU",
                    help="lista separada por virgula; cada um vira uma pagina")
    ap.add_argument("--bd", default=BD)
    ap.add_argument("--saida", default=str(SAIDA),
                    help="diretorio; cada simbolo vira <saida>/<simbolo>/index.html")
    args = ap.parse_args()

    t0 = time.time()
    simbolos = [s.strip().upper() for s in args.simbolos.split(",") if s.strip()]
    css_p = ESTILO
    css = css_p.read_text(encoding="utf-8") if css_p.exists() else ""
    agora = dt.datetime.now(dt.UTC)

    feitos, falhas = [], []
    estados: dict[str, dict] = {}
    for s in simbolos:
        try:
            barras = barras_do_banco(args.bd, s)
            if len(barras) < 60:
                # Menos que isso nem aquece o MACD. Pagina com moldura vazia
                # le-se como "o ativo nao andou", que e a licao de 07/09.
                falhas.append(f"{s}: {len(barras)} barras, minimo 60")
                continue
            destino = destino_de(args.saida, simbolos, s)
            destino.mkdir(parents=True, exist_ok=True)
            series = series_de(barras)
            (destino / "index.html").write_text(
                montar(s, barras, css, agora, outros=simbolos, series=series,
                       noticias=noticias_bloco(args.bd, s, agora)),
                encoding="utf-8")
            try:
                estados[s] = RT.estado_do_simbolo(
                    s, series, barras[-1].ts, int(agora.timestamp()))
            except Exception as exc:                          # noqa: BLE001
                # O estado do MCP e acessorio: a pagina ja saiu. A falha e
                # dita no log e o simbolo fica com o estado anterior.
                log.warning("tecnica_falhou", simbolo=s, erro=str(exc))
            feitos.append(f"{s} ({len(barras)} barras de 1h)")
            print(destino / "index.html")
        except Exception as exc:                              # noqa: BLE001
            # Um simbolo que falha nao leva os outros — a mesma regra do
            # `atualizar_pools`. E a falha e DITA, nunca so registrada.
            falhas.append(f"{s}: {type(exc).__name__}: {exc}")
            log.warning("grafico_falhou", simbolo=s, erro=str(exc))

    if estados:
        gravar_estado("tecnica", estado_tecnico(
            estados, int(agora.timestamp()), _estado_anterior()))

    dur = time.time() - t0
    log.info("grafico", feitos=len(feitos), falhas=len(falhas),
             segundos=round(dur, 1))
    try:
        gravar_batimento("grafico", ok=not falhas,
                         erro="; ".join(falhas)[:200] or None)
    except Exception:                                         # noqa: BLE001
        pass
    if falhas:
        print("falhas: " + "; ".join(falhas), file=sys.stderr)
    return 1 if falhas and not feitos else 0


if __name__ == "__main__":
    raise SystemExit(main())
