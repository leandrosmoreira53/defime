"""A pagina `/heat/` — o treemap dos tokens, e o clique que abre o grafico.

**O que nasce aqui: nada.** Os tiles e os retangulos vem de `defime/heatmap`,
que e puro; este arquivo escolhe cor, corta texto que nao cabe e escreve SVG.

---

**SVG do servidor e completo, e o JS so alterna a visao.** As duas visoes
(area por TVL e por volume) sao desenhadas as duas no HTML — sem JavaScript a
de TVL aparece inteira e clicavel, porque o link e um `<a>` de verdade e nao
um `onclick`. Mesma regra do grafico de APR e do `/lab/`.

**A cor e calculada no PYTHON e vai no `fill`.** Ela depende do teto derivado
(p90 das magnitudes), que e um numero medido — calcula-lo no navegador seria a
segunda implementacao que o `compor.js` ja custou caro a este projeto.

---

**Tile pequeno perde texto por ORDEM, nao some.** Primeiro sai o preco, depois
a variacao, e o simbolo fica ate nao caber. Um tile que existe e nao se lê
ainda informa o tamanho; um tile com texto transbordando por cima do vizinho
nao informa nada. O `title` tem sempre tudo, inclusive a pool dominante.
"""

from __future__ import annotations

import html

from defime import heatmap as H

from defime.web import noticias_render as NR   # o selo de manchetes (PLAN_NOTICIAS N2)

# O retangulo em que o treemap e calculado. **Mais quadrado que 16:9 de
# proposito**: no celular uma faixa larga e baixa deixaria os dez tiles com
# 20 px de altura. O CSS aplica a MESMA proporcao ao container, e e isso que
# impede a distorcao do texto.
VB_L, VB_A = 100.0, 72.0

# Paleta do tema escuro do projeto (`painel_estilo.css`). As cores sao
# interpoladas aqui e nao em CSS porque a intensidade e um numero MEDIDO.
_NEUTRO = (0x1C, 0x27, 0x30)      # tile sem variacao publicavel
_ALTA = (0x2E, 0xA3, 0x72)
_BAIXA = (0xC9, 0x4F, 0x43)


def _e(s) -> str:
    return html.escape(str(s if s is not None else ""))


def _mistura(a, b, t: float) -> str:
    t = max(0.0, min(1.0, t))
    return "#%02x%02x%02x" % tuple(
        round(a[i] + (b[i] - a[i]) * t) for i in range(3))


def cor_do_tile(variacao: float | None, teto: float) -> str:
    """Neutro quando a variacao nao e publicavel — **nao verde-claro**.

    Pintar de verde fraco quem nao tem medicao poria "nao sei" na mesma escala
    de "subiu pouco", que e a confusao que este projeto ja pagou com o zero no
    lugar do ausente.
    """
    if variacao is None:
        return _mistura(_NEUTRO, _NEUTRO, 0)
    alvo = _ALTA if variacao >= 0 else _BAIXA
    # piso de 0,12 para o tile nao ficar indistinguivel do neutro quando a
    # variacao e minuscula mas MEDIDA — a diferenca entre 0,0% e "nao sei"
    # tem de aparecer.
    return _mistura(_NEUTRO, alvo, 0.12 + 0.88 * H.intensidade(variacao, teto))


def _d(v: float, casas: int = 2) -> str:
    return f"{v:,.{casas}f}".replace(",", "@").replace(".", ",").replace("@", ".")


def _usd(v: float) -> str:
    if v >= 1e9:
        return f"${_d(v / 1e9, 2)}B"
    if v >= 1e6:
        return f"${_d(v / 1e6, 2)}M"
    if v >= 1e3:
        return f"${_d(v / 1e3, 0)}k"
    return f"${_d(v, 0)}"


def _pct(v: float | None) -> str:
    if v is None:
        return "—"
    return ("+" if v >= 0 else "−") + _d(abs(v), 2) + "%"


def _alvo(t, links: dict[str, str]) -> str | None:
    """Para onde o tile leva. `links` e `{simbolo: href}` e vem de quem SABE
    o que existe — inventar `/grafico/<sym>/` aqui produziria 404 para os
    tokens sem serie de bolsa, e link quebrado nao levanta nada."""
    return links.get(t.simbolo)


def _texto(t, links, teto) -> str:
    """O conteudo de um tile: simbolo, variacao e preco, nesta ordem de
    sacrificio. Quem corta o que nao cabe e `texto_no_tile`, que o
    `/heat/pools/` usa com as linhas dele — uma regra de encaixe so."""
    extra = []
    if t.variacao_24h_pct is not None or t.motivo_sem_variacao:
        extra.append(("var", _pct(t.variacao_24h_pct), 0.60))
    if t.preco is not None:
        extra.append(("pr", "$" + _d(t.preco, 2), 0.48))
    return texto_no_tile(t.x, t.y, t.largura, t.altura, t.simbolo, extra)


def texto_no_tile(x: float, y: float, w: float, h: float, titulo: str,
                  extra: list[tuple[str, str, float]]) -> str:
    """O texto de um tile, cortado pelo que cabe DENTRO dele.

    `titulo` e a primeira linha (classe `sym`) e define a fonte; `extra` sao
    `(classe, texto, fracao da fonte)` em ordem de sacrificio.

    **A conta e a altura do bloco de texto contra a altura do tile**, nao um
    limiar de tamanho de tile. A primeira versao usava limiares soltos
    (`h >= 10`) e o preco do USO saiu POR BAIXO da borda: o tile era alto o
    bastante pelo limiar e a terceira linha caia fora mesmo assim. Aqui as
    linhas sao montadas de cima para baixo e so entram enquanto o bloco
    inteiro couber — o texto nunca atravessa o vizinho.

    A ordem de sacrificio e de baixo para cima: um tile que mostra so o
    titulo ainda informa o tamanho e o nome, e o `title` tem tudo.
    """
    if w <= 0 or h <= 0:
        return ""

    # A fonte cabe na largura do titulo e na altura do tile, com teto para o
    # tile gigante nao virar um cartaz.
    fs = max(1.2, min(w / (0.62 * max(3, len(titulo)) + 0.8), h / 3.0, 5.0))
    if fs < 1.35 or w < fs * 0.62 * len(titulo):
        return ""                      # nem o titulo cabe: so a cor informa

    linhas = [("sym", titulo, fs)] + [(c, txt, fs * f) for c, txt, f in extra]

    ESPACO = 0.34                       # entrelinha, em multiplos da fonte
    # Largura media de um glifo em fracao do tamanho da fonte, para a familia
    # sem serifa do projeto. Aproximacao — o servidor nao mede texto —, e por
    # isso a margem abaixo e generosa.
    GLIFO = 0.60

    def _altura(ls):
        return sum(f for _, _, f in ls) + ESPACO * fs * (len(ls) - 1)

    # **Cada linha tambem tem de caber na LARGURA.** Sem isto, um tile
    # estreito e alto passa no teste de altura e o "+1,00%" sai pelos lados
    # por cima do vizinho — o mesmo defeito do preco do USO, no outro eixo.
    #
    # E o corte TRUNCA em vez de filtrar: filtrando, uma variacao larga
    # ("+10,00%") sairia enquanto o preco mais curto ficaria, produzindo um
    # tile com simbolo e preco e sem a variacao — que e justamente o numero
    # que o heatmap existe para mostrar. A ordem de sacrificio declarada no
    # docstring so vale se ela for respeitada aqui.
    cabe_ate = len(linhas)
    for i, (_, txt, f) in enumerate(linhas):
        if len(txt) * GLIFO * f > w * 0.92:
            cabe_ate = i
            break
    linhas = linhas[:cabe_ate]
    if not linhas:
        return ""                      # sem o titulo o resto nao identifica

    # corta de baixo para cima ate o bloco caber com margem de 12%
    while len(linhas) > 1 and _altura(linhas) > h * 0.88:
        linhas.pop()
    if _altura(linhas) > h * 0.95:
        return ""

    cx = x + w / 2
    yy = y + (h - _altura(linhas)) / 2   # bloco centralizado no tile
    partes = []
    for cls, txt, f in linhas:
        yy += f / 2
        partes.append(
            f'<text x="{cx:.2f}" y="{yy:.2f}" class="{cls}" '
            f'font-size="{f:.2f}" dominant-baseline="central">'
            f'{_e(txt)}</text>')
        yy += f / 2 + ESPACO * fs
    return "".join(partes)


def _title(t, por: str) -> str:
    """Tudo o que o tile nao coube em texto. **A fonte vai sempre**, porque
    ler "+2,6%" sem saber se e da pool (24/7) ou da bolsa (pregao) e ler um
    numero sem a pergunta que ele responde."""
    da_bolsa = t.fonte == "bolsa"
    l = [f"{t.simbolo} · {_pct(t.variacao_24h_pct)} em {t.janela}"]
    if t.preco is not None:
        onde = "bolsa" if da_bolsa else "pool"
        l.append(f"preço da {onde} ${_d(t.preco, 4)}")
    if da_bolsa:
        l.append(f"volume financeiro do pregão {_usd(t.volume_24h_usd)}")
        if t.preco_idade_min is not None:
            fresco = t.preco_idade_min <= 15
            qual = ("barra de 1 minuto" if t.preco_fonte == "1m"
                    else "último fechamento horário")
            quando = (f"há {t.preco_idade_min:.0f} min" if t.preco_idade_min < 90
                      else f"há {t.preco_idade_min / 60:.0f} h")
            l.append(f"{qual} — {quando}"
                     + (", pregão em curso" if fresco else ""))
        l.append("fonte: bolsa (Yahoo) — não é o preço do token na chain")
    else:
        l.append("preço do estado do contrato (bloco corrente)"
                 if t.preco_fonte == "contrato"
                 else "preço da série horária — o contrato não respondeu")
        idade = "" if t.tvl_idade_min is None else             f" · medidos há {t.tvl_idade_min:.0f} min"
        l.append(f"TVL {_usd(t.tvl_usd)} · volume 24 h "
                 f"{_usd(t.volume_24h_usd)}{idade}")
        l.append(f"área = {'TVL' if por == 'tvl' else 'volume 24 h'}")
        if t.pools > 1:
            l.append(f"{t.pools} pools somadas; preço e variação da dominante "
                     f"{t.pool_dominante[:10]}…")
        else:
            l.append(f"pool {t.pool_dominante[:10]}…")
    if t.motivo_sem_variacao:
        l.append(f"sem variação: {t.motivo_sem_variacao}")
    return " | ".join(l)


def _svg(m: dict, links: dict[str, str], ident: str, oculto: bool,
         selos: dict | None = None) -> str:
    teto = m["teto_cor_pct"]
    corpo = []
    for t in m["tiles"]:
        cor = cor_do_tile(t.variacao_24h_pct, teto)
        dentro = (
            f'<rect x="{t.x:.3f}" y="{t.y:.3f}" width="{max(0, t.largura):.3f}"'
            f' height="{max(0, t.altura):.3f}" fill="{cor}" rx="0.35"/>'
            + _texto(t, links, teto)
            + NR.selo_svg(t, (selos or {}).get(t.simbolo)))
        href = _alvo(t, links)
        tit = f"<title>{_e(_title(t, m['por']))}</title>"
        if href:
            corpo.append(f'<a href="{_e(href)}" class="tile">{tit}{dentro}</a>')
        else:
            # Sem gráfico não vira link morto: o tile fica e o `title` diz.
            corpo.append(f'<g class="tile sem">{tit}{dentro}</g>')
    cls = "mapa" + (" oculto" if oculto else "")
    # **`preserveAspectRatio` volta ao padrao e o CSS fixa a proporcao.**
    # Com `none` o SVG estica para preencher o container e o TEXTO estica
    # junto: o "SPY" saiu alongado na vertical porque o container era quase
    # quadrado e o viewBox 100x62. Agora o container tem a mesma proporcao do
    # viewBox (`aspect-ratio` no CSS), entao nada distorce.
    return (f'<svg id="{_e(ident)}" class="{cls}" '
            f'viewBox="0 0 {VB_L:g} {VB_A:g}" role="img" '
            f'aria-label="heatmap de tokens">' + "".join(corpo) + "</svg>")


CSS = """
.hwrap{background:var(--card);border:1px solid var(--rule);border-radius:12px;
  box-shadow:var(--sh);overflow:hidden;margin:16px 0}
.hbar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;padding:10px 12px;
  border-bottom:1px solid var(--rule);background:var(--rule2);font-size:12px}
.hbar .cresce{flex:1}
.hb{font:600 12px/1 "IBM Plex Sans",system-ui,sans-serif;color:var(--ink2);
  background:var(--card);border:1px solid var(--rule);border-radius:6px;
  padding:6px 10px;cursor:pointer}
.hb[aria-pressed="true"]{background:var(--accent-bg);border-color:var(--accent);
  color:var(--accent)}
.mapa{display:block;width:100%;aspect-ratio:100/72;
  max-height:78vh;margin:0 auto}
.mapa.oculto{display:none}
.tile rect{stroke:var(--bg);stroke-width:.18;transition:opacity .12s}
.tile:hover rect{opacity:.82}
.tile text{fill:#EAF2F7;text-anchor:middle;pointer-events:none;
  font-family:"IBM Plex Sans",system-ui,sans-serif}
.tile .sym{font-weight:700;letter-spacing:.02em}
.tile .var{font-weight:600;opacity:.95}
.tile .pr{opacity:.72;font-family:"IBM Plex Mono",ui-monospace,monospace}
a.tile{cursor:pointer}
g.tile.sem{cursor:default}
g.tile.sem text{opacity:.62}
.hleg{display:flex;flex-wrap:wrap;gap:12px;align-items:center;
  padding:9px 12px;border-top:1px solid var(--rule);background:var(--rule2);
  font-size:12px;color:var(--ink3)}
.esc{display:inline-flex;align-items:center;gap:6px}
.esc i{display:inline-block;width:86px;height:9px;border-radius:2px;
  background:linear-gradient(90deg,#C94F43,#1C2730,#2EA372)}
"""

JS = """<script>
(function(){
  // O JS so TROCA qual SVG esta visivel e reescreve a legenda. Todos os
  // mapas ja vieram desenhados do servidor; sem script a primeira visao
  // continua no ar e clicavel.
  var bts = [].slice.call(document.querySelectorAll('[data-vis]'));
  if(!bts.length) return;
  bts.forEach(function(b){
    b.addEventListener('click', function(){
      bts.forEach(function(o){
        o.setAttribute('aria-pressed', o === b ? 'true' : 'false');
        var el = document.getElementById('mapa-' + o.dataset.vis);
        if(el) el.classList.toggle('oculto', o !== b);
      });
      // **A legenda inteira muda com a aba, nao so a nota.** O teto da cor e
      // a janela sao de CADA visao: deixar o teto da pool desenhado sobre o
      // mapa da bolsa diria que a escala e uma quando e outra.
      var n = document.getElementById('nota-vis');
      if(n) n.textContent = b.dataset.nota || '';
      var e = document.getElementById('esc-txt');
      if(e) e.textContent = '−' + b.dataset.teto + '% a +' + b.dataset.teto
                            + '% em ' + b.dataset.janela;
      var a = document.getElementById('avisos-vis');
      if(a) a.innerHTML = b.dataset.avisos || '';
    });
  });
})();
</script>"""


def bloco(por_tvl: dict, por_vol: dict, links: dict[str, str],
          quando: str = "", bolsa: dict | None = None,
          selos: dict | None = None) -> str:
    """O heatmap inteiro: botoes, os SVG e a legenda.

    **Tres visoes, DUAS fontes, e elas nao se misturam.** `tvl` e `volume`
    descrevem a pool na chain; `bolsa` descreve a acao no pregao. O basis
    entre as duas tem desvio de 1,42% e chega a 3% — um mapa unico colocaria
    na mesma escala de cor dois numeros que nao sao comparaveis. Por isso a
    legenda inteira (teto da cor, janela e fonte) muda junto com a aba.
    """
    n = len(por_tvl["tiles"])

    def _avisos(m: dict, janela: str) -> list[str]:
        av = []
        if m["sem_variacao"]:
            av.append("sem variação publicável: <b>"
                      + _e(", ".join(m["sem_variacao"])) + "</b>")
        sem = sorted(t.simbolo for t in m["tiles"] if t.simbolo not in links)
        if sem:
            av.append("sem gráfico: <b>" + _e(", ".join(sem))
                      + "</b> — o tile não é clicável")
        return av

    abas = [
        ("tvl", "área: TVL", por_tvl,
         "área = TVL somado das pools do token · preço e variação da POOL",
         "24 h"),
        ("volume", "área: volume 24 h", por_vol,
         "área = volume negociado na pool nas últimas 24 h", "24 h"),
    ]
    if bolsa is not None:
        abas.append((
            "bolsa", "bolsa (Yahoo)", bolsa,
            "tiles do MESMO tamanho — o volume do pregão varia 600x e os "
            "menores sumiriam · cor = variação da BOLSA, fechamento contra "
            "fechamento", "pregão"))

    botoes, svgs, legendas = [], [], []
    for i, (ident, rot, m, nota, janela) in enumerate(abas):
        botoes.append(
            f'<button class="hb" data-vis="{_e(ident)}" '
            f'aria-pressed="{"true" if i == 0 else "false"}" '
            f'data-nota="{_e(nota)}" '
            f'data-teto="{_e(_d(m["teto_cor_pct"], 1))}" '
            f'data-janela="{_e(janela)}" '
            f'data-avisos="{_e(" · ".join(_avisos(m, janela)))}">'
            f'{_e(rot)}</button>')
        svgs.append(_svg(m, links, f"mapa-{ident}", oculto=(i != 0),
                         selos=selos))
        legendas.append(nota)

    t0 = abas[0]
    return (
        '<div class="hwrap"><div class="hbar">' + "".join(botoes)
        + f'<span class="cresce"></span><span>{n} tokens na chain · '
        f'{_e(_usd(por_tvl["total_usd"]))} de TVL</span>'
        '</div>' + "".join(svgs)
        + '<div class="hleg">'
        f'<span class="esc"><i></i> <span id="esc-txt">'
        f'−{_e(_d(t0[2]["teto_cor_pct"], 1))}% a '
        f'+{_e(_d(t0[2]["teto_cor_pct"], 1))}% em {_e(t0[4])}</span></span>'
        f'<span id="nota-vis">{_e(legendas[0])}</span>'
        f'<span id="avisos-vis">'
        + " · ".join(_avisos(t0[2], t0[4])) + '</span>'
        + (f"<span>{_e(quando)}</span>" if quando else "")
        + '</div></div>' + JS)


__all__ = ["bloco", "CSS", "cor_do_tile", "texto_no_tile"]
