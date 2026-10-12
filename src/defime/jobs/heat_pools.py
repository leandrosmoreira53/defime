"""A pagina `/heat/pools/` — heatmap das pools RWA por APR, volume, giro e aquecimento.

    python -m defime.jobs.heat_pools --estado ./estado --saida ./site/heat/pools --grafico ./site/grafico

**Zero RPC e zero rede.** Le o `rwa.json` que `python -m defime.jobs.rwa` grava (o universo RWA com a taxa
do LP lida do contrato e a serie de `vol_1h`) e, se existir, o `pools_ativos.json` de um indexador proprio.
Do banco, so a lista de simbolos com serie de bolsa, para saber quais tiles tem `/grafico/`. Toda conta mora
em `defime.heat_pools` (puro).

**Estado velho e DITO na pagina, nunca usado calado.** Cada JSON leva o seu `gerado_em`, e a pagina publica a
idade de cada um; o `rwa.json` acima de `IDADE_MAX_RWA_H` sai com aviso em destaque. Sem `rwa.json` nao ha mapa
e o job falha; sem `pools_ativos.json` o mapa sai so com o agregador e diz isso.
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

from defime import heat_pools as HP                              # noqa: E402
from defime.db.registro import gravar_batimento                  # noqa: E402
from defime.logging import get_logger                            # noqa: E402

from defime.web import heat_pools_render as R                                    # noqa: E402
from defime.web.heatmap_render import CSS as CSS_HEAT, _e                   # noqa: E402

log = get_logger(__name__)

BD = str(RAIZ / "defime.db")
ESTADO_DIR = os.environ.get("DEFIME_ESTADO_DIR", "estado")

# O `/rwa/` roda de hora em hora; duas horas sem ele e o mapa descreve o
# passado com cara de agora.
IDADE_MAX_RWA_H = 2.5


def ler_estado(diretorio: Path, nome: str) -> tuple[dict | None, int | None, str | None]:
    """`(dados, gerado_em, motivo)`. Ausente ou ilegivel devolve o MOTIVO, nao `{}`:
    um dicionario vazio seria lido adiante como "nenhuma pool", que e outra
    afirmacao."""
    p = diretorio / f"{nome}.json"
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d["dados"], d.get("gerado_em"), None
    except FileNotFoundError:
        return None, None, f"{p} não existe"
    except (OSError, ValueError, KeyError) as exc:
        return None, None, f"{p}: {type(exc).__name__}"


def _raiz_e(saida_grafico: Path, simbolo: str) -> bool:
    """O primeiro simbolo do `defime.jobs.grafico` mora na RAIZ da saida: confere pelo <title>."""
    try:
        cab = (saida_grafico / "index.html").read_text(encoding="utf-8")[:2000]
    except OSError:
        return False
    return f"<title>{simbolo} " in cab


def links_de_grafico(bd: str, saida_grafico: Path | None, simbolos) -> dict[str, str]:
    """`{simbolo: href}` so para quem TEM pagina: serie em `acoes_ohlc` **e** arquivo no disco."""
    try:
        c = sqlite3.connect(f"file:{bd}?mode=ro", uri=True)   # so leitura: nunca cria banco vazio
        try:
            com_serie = {s for (s,) in c.execute("select distinct simbolo from acoes_ohlc")}
        finally:
            c.close()
    except sqlite3.Error:
        return {}
    mapa: dict[str, str] = {}
    for s in simbolos:
        if s not in com_serie:
            continue
        if saida_grafico is None:
            mapa[s] = f"/grafico/{s.lower()}/"
        elif (saida_grafico / s.lower() / "index.html").exists():
            mapa[s] = f"/grafico/{s.lower()}/"
        elif (saida_grafico / "index.html").exists() and _raiz_e(saida_grafico, s):
            mapa[s] = "/grafico/"
    return mapa


def links(pools: list[HP.Pool], grafico: Path | None, bd: str) -> dict[str, str]:
    """`{pool_id: href}` para o `/grafico/` do ativo, so quando a pagina EXISTE (href por convencao daria 404)."""
    g = links_de_grafico(bd, grafico, {p.simbolo for p in pools})
    return {p.pool_id.lower(): g[p.simbolo] for p in pools if p.simbolo in g}


def montar(estado_dir: Path, bd: str, css: str, agora: int | None = None,
           grafico: Path | None = None) -> tuple[str, dict]:
    agora = int(agora or time.time())
    rwa, ts_rwa, erro_rwa = ler_estado(estado_dir, "rwa")
    if rwa is None:
        raise RuntimeError(f"sem rwa.json: {erro_rwa}")
    ativos, ts_ativos, erro_ativos = ler_estado(estado_dir, "pools_ativos")

    pools = HP.normalizar(rwa, agora)
    n_agregador = len(pools)
    if ativos:
        pools = HP.complementar(pools, ativos.get("ativos") or {}, agora)

    mapas, fora = {}, {}
    for todos in (False, True):
        dentro, fora[todos] = HP.filtrar(pools, incluir_outros=todos)
        for aba in HP.ABAS:
            mapas[(aba, todos)] = HP.montar(dentro, aba, R.VB_L, R.VB_A)

    lk = links(pools, grafico, bd)

    def idade(ts):
        return None if ts is None else (agora - ts) / 3600

    i_rwa = idade(ts_rwa)
    aviso_velho = (f'<p class="nota"><b class="down">O universo do agregador tem '
                   f'{R._d(i_rwa, 1)} h</b> — o job do /rwa/ não rodou; o mapa descreve '
                   f'aquele instante.</p>' if i_rwa is not None and i_rwa > IDADE_MAX_RWA_H
                   else "")

    def _fora(f):
        return " · ".join(f"<b>{_e(k)}</b> ({len(v)}): {_e(', '.join(v))}"
                          for k, v in f.items()) or "nenhuma"

    n_idx = len(pools) - n_agregador
    quando = dt.datetime.fromtimestamp(agora, dt.timezone.utc)
    medindo = sum(1 for p in pools if p.fonte == "agregador" and p.concentracao is None)

    def _idade_txt(nome, ts, erro):
        return f"{nome} {R._d(idade(ts), 1)} h" if ts else f"{nome} indisponível ({erro})"

    html = f"""<!doctype html>
<html lang="pt-BR" data-theme="dark"><head>
<meta name="color-scheme" content="dark">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>Pools RWA · heatmap</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500;600;700&display=swap">
<style>{css}{CSS_HEAT}{R.CSS}{_CSS}</style></head><body>
<div class="w largo">
<header class="hd">
  <p class="eyebrow"><a href="../../">defime</a> · <a href="../../grafico/">gráfico</a></p>
  <h1>Pools RWA que mais estão pagando</h1>
  <p class="sub">Robinhood Chain · um tile por POOL · {quando:%d/%m %H:%M} UTC</p>
</header>
{aviso_velho}
{R.bloco(mapas, lk, fora[False], fora[True])}

<p class="nota"><b>As quatro abas elegem campeões diferentes, e isso é o ponto.</b>
Volume é onde está o fluxo; giro é quantas vezes o capital da pool girou no dia;
<b>APR não é uma terceira evidência: APR = giro × taxa do LP × 365.</b> Por isso uma
pool de 1% gira menos e paga mais que uma de 0,05%. "Aquecendo" compara a última
hora com o ritmo médio do dia: um APR de 24 h alto com aquecimento perto de zero é
um dia que já passou.</p>

<div class="sec"><h2>O que ficou fora do mapa padrão</h2></div>
<p class="nota">{_fora(fora[False])}. O botão <b>+ outros pares e AMMs</b> mostra
tudo, com selo — nada some.</p>

<p class="nota"><b>Duas fontes, e cada tile diz a sua.</b> {n_agregador} pools vêm
do agregador (GeckoTerminal), com a taxa do LP lida do contrato. {n_idx} vêm de um
indexador próprio (opcional) porque o agregador não as lista, e o volume de 24 h delas é
<b>estimado</b> das horas que o indexador amostra (marcadas "idx"; sem
volume da última hora, logo sem aquecimento). A concentração do volume ("surto")
precisa de {HP.SERIE_MIN_PONTOS} horas de série: {medindo} pools ainda estão
medindo, e medindo não é "ok".</p>

<p class="nota" style="opacity:.75">estados: {_e(_idade_txt("rwa", ts_rwa, erro_rwa))} ·
{_e(_idade_txt("pools_ativos", ts_ativos, erro_ativos))} · zero chamadas de RPC</p>
</div></body></html>"""
    resumo = {"pools": len(pools), "agregador": n_agregador, "indexador": n_idx,
              "no_mapa": len(mapas[("apr", False)]["tiles"]),
              "links": len(lk)}
    return html, resumo


_CSS = """
h1{font-size:26px;margin:0 0 4px} .sub{color:var(--ink2);margin:0;font-size:13px}
.eyebrow{font-size:12px;color:var(--ink3);margin:0 0 14px}
.eyebrow a{color:var(--accent);text-decoration:none}
.hd{margin-bottom:6px}
.sec{margin:26px 0 10px;border-bottom:1px solid var(--rule)}
.sec h2{font-size:15px;margin:0 0 8px;font-weight:600}
.nota{font-size:13px;color:var(--ink2);margin:10px 0;max-width:80ch}
.nota code{background:var(--rule2);padding:1px 4px;border-radius:3px;
  font-family:"IBM Plex Mono",ui-monospace,monospace;font-size:12px}
.down{color:#C94F43}
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--estado", default=ESTADO_DIR)
    ap.add_argument("--bd", default=BD)
    ap.add_argument("--saida", default=str(RAIZ / "site" / "heat" / "pools"))
    ap.add_argument("--grafico", default=None)
    args = ap.parse_args()
    t0 = time.time()
    css_p = ESTILO
    css = css_p.read_text(encoding="utf-8") if css_p.exists() else ""
    try:
        html, resumo = montar(Path(args.estado), args.bd, css,
                              grafico=Path(args.grafico) if args.grafico else None)
    except Exception as exc:                                  # noqa: BLE001
        log.warning("heat_pools_falhou", erro=str(exc))
        try:
            gravar_batimento("heat_pools", ok=False, erro=str(exc)[:200])
        except Exception:                                     # noqa: BLE001
            pass
        print(f"falhou: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    destino = Path(args.saida)
    destino.mkdir(parents=True, exist_ok=True)
    tmp = destino / "index.html.tmp"
    tmp.write_text(html, encoding="utf-8")
    tmp.replace(destino / "index.html")
    print(destino / "index.html", resumo)
    log.info("heat_pools", **resumo, segundos=round(time.time() - t0, 1))
    try:
        gravar_batimento("heat_pools", ok=True)
    except Exception:                                         # noqa: BLE001
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
