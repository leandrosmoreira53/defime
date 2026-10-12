"""A pagina `/heat/pools/` — as pools RWA que mais estao pagando, por pool.

**O que nasce aqui: nada.** Metricas, selos, escalas e layout vem de
`defime/heat_pools`, que e puro; este arquivo escolhe cor e escreve SVG.
O encaixe do texto no tile e o do `/heat/` (`heatmap_render.texto_no_tile`),
e nao uma segunda versao dele.

---

**SVG do servidor e completo, e o JS so alterna a visao.** Quatro abas x
{mapa padrao, com os outros pares} = oito SVG no HTML. Sem JavaScript a aba
APR do mapa padrao aparece inteira e clicavel.

**Cor calculada no PYTHON**, pelo mesmo motivo do `/heat/`: o piso e o teto
da escala sao numeros medidos, e calcula-los no navegador seria a segunda
implementacao que o `compor.js` ja custou caro.

**Duas paletas, porque sao duas perguntas.** APR, volume e giro nao tem
negativo: escala SEQUENCIAL (escuro -> ambar), em log. "Aquecendo" tem um
centro natural, 1,0x = o ritmo medio do dia: escala DIVERGENTE (azul esfria,
laranja esquenta). Usar verde/vermelho aqui leria como "subiu/caiu", que e a
pergunta do `/heat/`, nao desta pagina.
"""

from __future__ import annotations

import html

from defime import heat_pools as HP

from defime.web.heatmap_render import _d, _e, _mistura, _usd, texto_no_tile

VB_L, VB_A = 100.0, 72.0          # a mesma proporcao do /heat/, pelo mesmo motivo

_NEUTRO = (0x1C, 0x27, 0x30)      # sem valor publicavel: cinza, nunca "0"
_QUENTE = (0xE8, 0xA3, 0x3D)      # sequencial: escuro -> ambar
_ESQUENTA = (0xE0, 0x7B, 0x39)
_ESFRIA = (0x3E, 0x7C, 0xB1)

ROTULO_ABA = {
    "apr": ("APR 24 h", "área = TVL · cor = APR do LP em 24 h (taxa que CHEGA ao LP)"),
    "volume": ("volume 24 h", "área e cor = volume negociado em 24 h"),
    "giro": ("giro (vol/TVL)", "área = TVL · cor = volume 24 h ÷ TVL: quantas vezes "
             "o capital da pool girou no dia"),
    "aquecendo": ("aquecendo", "área = volume 24 h · cor = ritmo da ÚLTIMA hora ÷ ritmo "
                  "médio do dia (1,0x = normal)"),
}


def cor(intensidade: float | None, tipo: str) -> str:
    """Cinza sem valor; na sequencial, piso de 0,12 para o MENOR valor medido
    nao ficar igual ao "sem valor" — a mesma regra do `/heat/`."""
    if intensidade is None:
        return _mistura(_NEUTRO, _NEUTRO, 0)
    if tipo == "log":
        return _mistura(_NEUTRO, _QUENTE, 0.12 + 0.88 * intensidade)
    alvo = _ESQUENTA if intensidade >= 0 else _ESFRIA
    return _mistura(_NEUTRO, alvo, abs(intensidade))


def fmt_valor(aba: str, v: float | None) -> str:
    if v is None:
        return "—"
    if aba == "apr":
        return f"{_d(v, 0 if v >= 100 else 1)}%"
    if aba == "volume":
        return _usd(v)
    if aba == "giro":
        return f"{_d(v, 2)}x"
    return f"{_d(v, 2)}x"


def _pct(v: float | None, casas: int = 1) -> str:
    return "—" if v is None else f"{_d(v, casas)}%"


def _title(p: HP.Pool, aba: str, valor) -> str:
    """Tudo o que o tile nao coube em texto, e sempre a FONTE de cada numero."""
    l = [f"{p.rotulo} · {ROTULO_ABA[aba][0]} {fmt_valor(aba, valor)}"]
    fonte = ("agregador (GeckoTerminal) + taxa do LP lida do contrato"
             if p.fonte == "agregador" else "indexador do USO (pools_hora), amostrado")
    l.append(f"fonte: {fonte}")
    l.append(f"APR 24 h {_pct(p.apr_24h)} · 7 d {_pct(p.apr_7d)} · 30 d {_pct(p.apr_30d)}")
    if p.fonte == "agregador" and p.apr_idx is not None:
        l.append(f"APR pelo indexador {_pct(p.apr_idx)} (conferência, outra janela e outro TVL)")
    l.append(f"volume 24 h {_usd(p.vol_24h) if p.vol_24h is not None else '—'}"
             + (" (estimado)" if p.fonte == "indexador" else "")
             + (f" · última hora {_usd(p.vol_1h)}" if p.vol_1h is not None else ""))
    tvl = f"TVL {_usd(p.tvl)}"
    if p.fonte == "agregador" and p.tvl_idx:
        tvl += f" · indexador {_usd(p.tvl_idx)}"
    l.append(tvl)
    if p.giro is not None:
        l.append(f"giro {_d(p.giro, 2)}x/dia")
    if p.aquecendo is not None:
        l.append(f"última hora a {_d(p.aquecendo, 2)}x o ritmo médio do dia")
    if p.concentracao is not None:
        l.append(f"maior hora do dia = {_d(100 * p.concentracao, 0)}% do volume")
    elif p.fonte == "agregador":
        l.append(f"concentração: medindo ({p.pontos_serie} de {HP.SERIE_MIN_PONTOS} "
                 "horas de série)")
    taxa = (f"taxa {_d(p.fee_pct, 3)}%" if p.fee_pct is not None else "taxa ?")
    taxa += (f" → LP {_d(p.lp_pct, 4)}%" if p.lp_pct is not None else " → LP ilegível")
    l.append(taxa)
    if p.idade_d is not None:
        l.append(f"pool com {_d(p.idade_d, 1)} dias")
    for txt in p.selos.values():
        l.append("⚑ " + txt)
    l.append(f"pool {p.pool_id[:12]}…")
    return " | ".join(l)


def _svg(m: dict, links: dict[str, str], ident: str, oculto: bool) -> str:
    aba, esc = m["aba"], m["escala"]
    tipo = esc["tipo"] if esc else "log"
    corpo = []
    for t in m["tiles"]:
        p = t["pool"]
        ressalva = any(k != "estimado" for k in p.selos) or p.fonte == "indexador"
        rect = (f'<rect x="{t["x"]:.3f}" y="{t["y"]:.3f}" width="{max(0, t["l"]):.3f}" '
                f'height="{max(0, t["a"]):.3f}" fill="{cor(t["intensidade"], tipo)}" rx="0.35"'
                + (' class="ress"' if ressalva else "") + "/>")
        amm = HP.amm(p.dex, p.versao) + (" · idx" if p.fonte == "indexador" else "")
        taxa = f"{HP._num(p.fee_pct)}%" if p.fee_pct is not None else "?"
        txt = texto_no_tile(t["x"], t["y"], t["l"], t["a"],
                            p.simbolo if p.usdg else p.par.replace(" ", ""),
                            [("var", fmt_valor(aba, t["valor"]), 0.60),
                             ("pr", f"{taxa} · {amm}", 0.44)])
        tit = f"<title>{_e(_title(p, aba, t['valor']))}</title>"
        href = links.get(p.pool_id.lower())
        if href:
            corpo.append(f'<a href="{_e(href)}" class="tile">{tit}{rect}{txt}</a>')
        else:
            corpo.append(f'<g class="tile sem">{tit}{rect}{txt}</g>')
    return (f'<svg id="{_e(ident)}" class="mapa{" oculto" if oculto else ""}" '
            f'viewBox="0 0 {VB_L:g} {VB_A:g}" role="img" '
            f'aria-label="heatmap das pools, {_e(ROTULO_ABA[aba][0])}">'
            + "".join(corpo) + "</svg>")


def _nota(m: dict) -> str:
    """O que a AREA quer dizer nesta aba — e na grade uniforme ela nao quer
    dizer nada, e isso tem de estar escrito: "area = TVL" sobre tiles do
    mesmo tamanho seria legenda afirmando o que o desenho nao faz."""
    rot, nota = ROTULO_ABA[m["aba"]]
    if not m["grade_uniforme"]:
        return nota
    cor_txt = nota.split("cor = ", 1)[-1] if "cor = " in nota else nota
    return (f"tiles do MESMO tamanho, em ordem da cor — a razão entre a maior e a "
            f"menor pool passa de {HP.GRADE_RAZAO_MAX:.0f}x e as pequenas sumiriam · "
            f"cor = {cor_txt}")


def _legenda(m: dict) -> str:
    """O que a cor quer dizer NESTA aba, com os limites da escala escritos."""
    esc, aba = m["escala"], m["aba"]
    if esc is None:
        return "sem valores para colorir"
    if esc["tipo"] == "log":
        return (f"escala log de {fmt_valor(aba, esc['piso'])} (p10) a "
                f"{fmt_valor(aba, esc['teto'])} (p90); fora disso satura")
    t = 2 ** esc["teto_log2"]
    return f"1,0x ao centro · satura em {_d(1 / t, 2)}x (esfria) e {_d(t, 1)}x (esquenta)"


CSS = """
.mapa .ress{stroke:#E8A33D;stroke-width:.32;stroke-dasharray:.9 .6}
.esc.seq i{background:linear-gradient(90deg,#1C2730,#E8A33D)}
.esc.div i{background:linear-gradient(90deg,#3E7CB1,#1C2730,#E07B39)}
.top{width:100%;border-collapse:collapse;font-size:13px}
.top th,.top td{padding:7px 8px;border-bottom:1px solid var(--rule);text-align:right;
  white-space:nowrap}
.top th:first-child,.top td:first-child{text-align:left}
.top td.fraco{color:var(--ink3)}
.twrap{overflow-x:auto}
"""

JS = """<script>
(function(){
  // So ALTERNA qual SVG aparece e reescreve a legenda. Os oito mapas ja
  // vieram desenhados do servidor.
  var aba = 'apr', todos = false;
  var abas = [].slice.call(document.querySelectorAll('[data-aba]'));
  var tg = document.getElementById('hp-outros');
  function mostra(){
    [].slice.call(document.querySelectorAll('svg.mapa')).forEach(function(s){
      s.classList.toggle('oculto', s.id !== 'hp-' + aba + (todos ? '-todos' : ''));
    });
    abas.forEach(function(b){ b.setAttribute('aria-pressed', b.dataset.aba === aba); });
    var b = abas.filter(function(x){ return x.dataset.aba === aba; })[0];
    document.getElementById('hp-nota').textContent = b.dataset['nota' + (todos ? 'Todos' : 'Padrao')];
    document.getElementById('hp-esc').textContent = b.dataset['esc' + (todos ? 'Todos' : 'Padrao')];
    var e = document.getElementById('hp-escbar');
    e.className = 'esc ' + (aba === 'aquecendo' ? 'div' : 'seq');
    document.getElementById('hp-n').textContent = b.dataset['n' + (todos ? 'Todos' : 'Padrao')];
  }
  abas.forEach(function(b){ b.addEventListener('click', function(){ aba = b.dataset.aba; mostra(); }); });
  if(tg) tg.addEventListener('click', function(){
    todos = !todos; tg.setAttribute('aria-pressed', todos); mostra();
  });
})();
</script>"""


def bloco(mapas: dict, links: dict[str, str], fora_padrao: dict, fora_todos: dict) -> str:
    """`mapas[(aba, todos)]` -> saida de `HP.montar`. Botoes, SVGs e legenda."""
    botoes, svgs = [], []
    for i, aba in enumerate(HP.ABAS):
        mp, mt = mapas[(aba, False)], mapas[(aba, True)]
        rot = ROTULO_ABA[aba][0]
        botoes.append(
            f'<button class="hb" data-aba="{aba}" aria-pressed="{"true" if i == 0 else "false"}"'
            f' data-nota-padrao="{_e(_nota(mp))}" data-nota-todos="{_e(_nota(mt))}"'
            f' data-esc-padrao="{_e(_legenda(mp))}"'
            f' data-esc-todos="{_e(_legenda(mt))}"'
            f' data-n-padrao="{len(mp["tiles"])} pools"'
            f' data-n-todos="{len(mt["tiles"])} pools">{_e(rot)}</button>')
        svgs.append(_svg(mp, links, f"hp-{aba}", oculto=(i != 0)))
        svgs.append(_svg(mt, links, f"hp-{aba}-todos", oculto=True))
    m0 = mapas[("apr", False)]
    return (
        '<div class="hwrap"><div class="hbar">' + "".join(botoes)
        + '<span class="cresce"></span>'
        '<button class="hb" id="hp-outros" aria-pressed="false" title="pares sem USDG e '
        'pools cuja taxa do LP não se lê no contrato">+ outros pares e AMMs</button>'
        f'<span id="hp-n">{len(m0["tiles"])} pools</span></div>'
        + "".join(svgs)
        + '<div class="hleg">'
        f'<span class="esc seq" id="hp-escbar"><i></i></span>'
        f'<span id="hp-esc">{_e(_legenda(m0))}</span>'
        f'<span id="hp-nota">{_e(_nota(m0))}</span>'
        '<span>borda tracejada = tem ressalva (toque no tile)</span>'
        '</div></div>' + JS)


def tabela_top(top: dict, atual: dict | None) -> str:
    """As pools que mais pagariam ao capital de hoje, com o custo junto."""
    cap = top["capital"]
    linhas = []
    for ln in top["linhas"]:
        p = ln["pool"]
        paga = ("—" if ln["paga_em_h"] is None else
                f"{_d(ln['paga_em_h'], 0)} h" if ln["paga_em_h"] < 1000 else "> 1.000 h")
        linhas.append(
            f"<tr><td>{_e(p.rotulo)}"
            + (' <span class="fraco">(estimado)</span>' if p.fonte == "indexador" else "")
            + f"</td><td>{_pct(p.apr_24h, 0)}</td><td>{_pct(p.apr_7d, 0)}</td>"
            f"<td>{_e(ln.get('janela') or '—')}</td><td>{_usd(ln['book'])}</td>"
            f"<td>{_d(100 * ln['fatia'], 1)}%</td><td>${_d(ln['fee_h'], 2)}</td>"
            f"<td>${_d(ln['fee_dia'], 0)}</td>"
            f"<td>{'—' if ln['custo'] is None else '$' + _d(ln['custo'], 0)}</td>"
            f"<td>{paga}</td></tr>")
    if atual and atual.get("fee_h") is not None:
        cab_atual = (f"Hoje o capital está em <b>{_e(atual['rotulo'])}</b>, que pela mesma "
                     f"conta pagaria <b>${_d(atual['fee_h'], 2)}/h</b>.")
    elif atual:
        cab_atual = (f"Hoje o capital está em <b>{_e(atual['rotulo'])}</b>, sem a mesma "
                     f"conta para ela ({_e(atual.get('motivo') or 'sem book ou volume')}): "
                     "“paga em” fica vazio.")
    else:
        cab_atual = "Sem posição aberta lida: “paga em” fica vazio."
    fora = " · ".join(f"{_e(k)}: {len(v)}" for k, v in top["fora"].items())
    return (
        f'<p class="nota">Capital de referência <b>{_usd(cap)}</b> (o capital informado '
        f'agora), numa faixa de ±1%. {cab_atual}</p>'
        '<div class="twrap"><table class="top"><thead><tr><th>pool</th><th>APR 24 h</th>'
        '<th>APR 7 d</th><th title="o MENOR entre o volume de 24 h e a média diária de 7 d">'
        'volume usado</th><th>book ±1%</th><th>sua fatia</th><th>fee/h</th><th>fee/dia</th>'
        '<th>custo da troca</th><th>paga em</th></tr></thead><tbody>'
        + ("".join(linhas) or '<tr><td colspan="10">nenhuma pool com book medido</td></tr>')
        + "</tbody></table></div>"
        + (f'<p class="nota" style="opacity:.8">Fora do top: {fora}.</p>' if fora else ""))


__all__ = ["bloco", "tabela_top", "CSS", "cor", "fmt_valor"]
