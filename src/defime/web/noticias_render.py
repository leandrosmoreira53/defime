"""Noticias por ativo nas paginas — Fase N2 do PLAN_NOTICIAS.md.

Tres pecas de HTML/SVG, todas escritas no servidor e sem JavaScript:

  * `selo_svg`     — o selo no canto do tile do `/heat/` (manchetes em 24 h)
  * `o_que_moveu`  — o bloco abaixo do mapa: quem mais andou e as manchetes
  * `lista`        — o bloco "Noticias" da pagina `/grafico/<sym>/`

**Nada aqui le banco nem rede.** Recebe o que `macro/noticias_banco.ler`
devolveu — a MESMA funcao para as duas paginas, que e o que impede o `/heat/`
e o `/grafico/` de discordarem sobre as manchetes de um ativo.

**A noticia e da ACAO.** Na aba da bolsa isso casa com a cor do tile; nas
abas da pool o tile e o TOKEN, que desvia ate 3% da acao. O `title` do selo
diz isso, pela regra de que um numero sem a pergunta que ele responde nao
informa.

**"Sem leitura" nunca vira "0".** Se a ultima tentativa de todas as fontes
falhou, o selo sai cinza com "?" e o motivo; zero manchetes com fonte viva e
outra afirmacao, e sai como zero.
"""

from __future__ import annotations

import datetime as dt
import html

TRADINGVIEW = "https://www.tradingview.com/symbols/{s}/"

# Onde o link externo nao pode ser montado por convencao: a SPCX do Yahoo e a
# SpaceX, e nada garante que o `SPCX` do TradingView seja a mesma empresa.
SEM_TRADINGVIEW = {"SPCX"}


def _e(s) -> str:
    return html.escape(str(s), quote=True)


def _hora(ts: int | None) -> str:
    if not ts:
        return "—"
    return dt.datetime.fromtimestamp(ts, dt.UTC).strftime("%d/%m %H:%M UTC")


def _idade(h: float) -> str:
    if h < 1:
        return f"{max(1, round(h * 60))} min"
    if h < 48:
        return f"{h:.0f} h"
    return f"{h / 24:.0f} d"


def estado(simbolo: str, manchetes: list[dict] | None,
           leitura: dict | None) -> dict:
    """O que o selo e a lista precisam saber de um simbolo, sem ambiguidade.

    `coletado`: o simbolo esta no universo do coletor? (SGOV nao esta.)
    `sem_leitura`: a ultima rodada nao ouviu fonte nenhuma para ele.
    """
    if leitura is None:
        return {"coletado": False, "sem_leitura": False, "n": None,
                "eventos": 0, "ultima_ok": None}
    ms = manchetes or []
    return {"coletado": True, "sem_leitura": bool(leitura.get("sem_leitura")),
            "n": len(ms),
            "eventos": sum(1 for m in ms
                           if m["eventos"] and simbolo in m.get("nomeia", [])),
            "ultima_ok": leitura.get("ultima_ok")}


# ------------------------------------------------------------------- selo

def selo_svg(t, est: dict | None) -> str:
    """Selo no canto superior direito do tile. Nao muda cor nem area.

    So entra se o tile tiver espaco: selo por cima do simbolo e pior que selo
    nenhum, e o `title` do proprio selo repete o numero.
    """
    if not est or not est["coletado"]:
        return ""
    w, h = t.largura, t.altura
    fs = min(2.1, h / 6.0, w / 7.0)
    if fs < 1.1:
        return ""
    if est["sem_leitura"]:
        txt, cls = "?", "selo mudo"
        tit = (f"{t.simbolo}: nenhuma fonte de notícia respondeu na última "
               f"rodada · última leitura boa {_hora(est['ultima_ok'])}")
    else:
        txt = str(est["n"]) + (" ●" if est["eventos"] else "")
        cls = "selo" + (" ev" if est["eventos"] else "")
        tit = (f"{t.simbolo}: {est['n']} manchete(s) em 24 h"
               + (f", {est['eventos']} com evento que nomeia o ativo"
                  if est["eventos"] else "")
               + " · notícia da AÇÃO (Yahoo), não do token na chain")
    x = t.x + w - 0.6
    y = t.y + 0.6 + fs * 0.55
    return (f'<text x="{x:.2f}" y="{y:.2f}" class="{cls}" '
            f'font-size="{fs:.2f}" text-anchor="end" '
            f'dominant-baseline="central"><title>{_e(tit)}</title>'
            f'{_e(txt)}</text>')


# ------------------------------------------------------------------- listas

def _item(m: dict, simbolo: str) -> str:
    ev = (f' <span class="nev">{_e(", ".join(m["eventos"]))}</span>'
          if m["eventos"] and simbolo in m.get("nomeia", []) else "")
    cita = ("" if simbolo in m.get("nomeia", [])
            else ' <span class="ncita" title="a fonte marcou o ativo, mas a '
                 'manchete não o nomeia — costuma ser resumo de mercado">'
                 'cita</span>')
    return (f'<li><a href="{_e(m["link"])}" target="_blank" '
            f'rel="noopener nofollow">{_e(m["titulo"])}</a>{ev}{cita}'
            f'<span class="nmeta">{_e(m.get("publisher") or m["fonte"])} · '
            f'{_e(_idade(m["idade_h"]))}</span></li>')


def _ordenar(ms: list[dict], simbolo: str) -> list[dict]:
    """As que NOMEIAM o ativo primeiro, depois as que so o citam; dentro de
    cada grupo, a mais nova primeiro (a ordem que `ler` ja devolve)."""
    return ([m for m in ms if simbolo in m.get("nomeia", [])]
            + [m for m in ms if simbolo not in m.get("nomeia", [])])


def _vazio(simbolo: str, est: dict) -> str:
    if not est["coletado"]:
        return "notícia não coletada para este ativo"
    if est["sem_leitura"]:
        return (f"fonte muda desde {_hora(est['ultima_ok'])} — "
                "sem leitura, não é ausência de notícia")
    return "nenhuma manchete em 24 h (a fonte respondeu)"


def link_tradingview(simbolo: str) -> str | None:
    if simbolo in SEM_TRADINGVIEW:
        return None
    return TRADINGVIEW.format(s=simbolo)


def o_que_moveu(tiles, noticias: dict[str, list[dict]],
                estados: dict[str, dict], links: dict[str, str],
                n: int = 8, por_ativo: int = 3) -> str:
    """Os `n` tiles de maior |variacao| com as manchetes deles.

    `tiles` sao os da aba da BOLSA: a variacao ali e da acao, do mesmo
    mercado da noticia. Pregao e janela de 24 h nao sao o mesmo intervalo, e
    o rodape diz isso — a manchete explica o movimento, nao o prova.
    """
    com_var = [t for t in tiles if t.variacao_24h_pct is not None]
    com_var.sort(key=lambda t: -abs(t.variacao_24h_pct))
    blocos = []
    for t in com_var[:n]:
        s = t.simbolo
        est = estados.get(s) or estado(s, None, None)
        ms = _ordenar(noticias.get(s) or [], s)[:por_ativo]
        var = t.variacao_24h_pct
        cls = "alta" if var > 0 else ("baixa" if var < 0 else "")
        # o mesmo "−" (U+2212) que o heatmap usa no tile
        var_txt = (("+" if var > 0 else "") + f"{var:.2f}%").replace(
            ".", ",").replace("-", "−")
        corpo = ("<ul>" + "".join(_item(m, s) for m in ms) + "</ul>" if ms
                 else f'<p class="nvazio">{_e(_vazio(s, est))}</p>')
        extra = []
        if links.get(s):
            extra.append(f'<a href="{_e(links[s])}">gráfico</a>')
        tv = link_tradingview(s)
        if tv:
            extra.append(f'<a href="{_e(tv)}" target="_blank" '
                         f'rel="noopener nofollow">TradingView ↗</a>')
        cont = ("" if est["n"] is None
                else f' · {est["n"]} em 24 h' + (f', {est["eventos"]} ●'
                                                  if est["eventos"] else ""))
        blocos.append(
            f'<details class="nmov"{" open" if len(blocos) < 3 else ""}>'
            f'<summary><b>{_e(s)}</b> <span class="{cls}">{_e(var_txt)}'
            f'</span>{_e(cont)}</summary>'
            + corpo
            + (f'<p class="nlinks">{" · ".join(extra)}</p>' if extra else "")
            + "</details>")
    if not blocos:
        return ""
    return ('<div class="sec"><h2>O que moveu</h2></div>'
            '<p class="nota">As ações que mais andaram no último pregão, com '
            'as manchetes das últimas 24 h. <b>Primeiro as que nomeiam o '
            'ativo</b>; "cita" marca resumo de mercado em que a fonte o '
            'marcou. ● é evento (guidance, price target, recall…) na manchete '
            'que o nomeia. A manchete ajuda a explicar o movimento; não prova '
            'que foi ela.</p>' + "".join(blocos))


def lista(simbolo: str, manchetes: list[dict], est: dict,
          limite: int = 30) -> str:
    """Bloco "Noticias" da pagina de grafico: as ultimas, nomeadas primeiro."""
    ms = _ordenar(manchetes, simbolo)[:limite]
    tv = link_tradingview(simbolo)
    cab = (f'<p class="nota">Manchetes da <b>ação</b> nos últimos 7 dias '
           f'(Yahoo; Google News como reserva). Leitura, não sinal — '
           f'nenhuma entra em decisão.'
           + (f' <a href="{_e(tv)}" target="_blank" rel="noopener nofollow">'
              f'Ver no TradingView ↗</a>' if tv else "") + '</p>')
    corpo = ('<ul class="nlista">' + "".join(_item(m, simbolo) for m in ms)
             + "</ul>" if ms
             else f'<p class="nvazio">{_e(_vazio(simbolo, est))}</p>')
    aviso = ""
    if est["coletado"] and est["sem_leitura"] and ms:
        aviso = (f'<p class="nvazio">a última rodada não ouviu fonte '
                 f'nenhuma; lista até {_e(_hora(est["ultima_ok"]))}</p>')
    return ('<div class="sec"><h2>Notícias</h2></div>' + cab + aviso + corpo)


CSS = """
/* `text-anchor:end` no CSS e nao so no atributo: o `.tile text` do heatmap
   poe `middle` por CSS, que vence o atributo, e o selo saia centrado na borda
   do tile, cortado pela metade pelo vizinho. */
.tile .selo{text-anchor:end;fill:#EAF2F7;opacity:.78;font-weight:600;
  pointer-events:auto;
  font-family:"IBM Plex Mono",ui-monospace,monospace}
.tile .selo.ev{fill:#F2C14E;opacity:1}
.tile .selo.mudo{fill:#9AA7B1;opacity:.7}
.nmov{background:var(--card);border:1px solid var(--rule);border-radius:8px;
  padding:8px 12px;margin:8px 0;max-width:90ch}
.nmov summary{cursor:pointer;font-size:14px}
.nmov .alta{color:#2EA372;font-weight:600} .nmov .baixa{color:#C94F43;
  font-weight:600}
.nmov ul,.nlista{margin:8px 0 4px;padding-left:18px;font-size:13px}
.nmov li,.nlista li{margin:5px 0;line-height:1.35}
.nmov li a,.nlista li a{color:var(--ink);text-decoration:none}
.nmov li a:hover,.nlista li a:hover{text-decoration:underline}
.nmeta{display:block;font-size:11px;color:var(--ink3)}
.nev{font-size:11px;color:#F2C14E;margin-left:4px}
.ncita{font-size:10px;color:var(--ink3);border:1px solid var(--rule);
  border-radius:3px;padding:0 3px;margin-left:4px}
.nvazio{font-size:12px;color:var(--ink3);margin:6px 0}
.nlinks{font-size:12px;margin:4px 0 0} .nlinks a{color:var(--accent)}
"""

__all__ = ["estado", "selo_svg", "o_que_moveu", "lista", "link_tradingview",
           "CSS"]
