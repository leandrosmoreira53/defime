"""A pagina de grafico — candles, volume e indicadores, no estilo de uma
plataforma de trading.

**O que nasce aqui: nada.** Os candles saem de `acoes_ohlc` e os indicadores
de `macro/indicadores.py`, que e puro. Este arquivo formata e desenha — a
mesma regra do `perfil_render` e do `analise_render`.

---

**Os indicadores sao calculados no PYTHON e mandados prontos**, e essa e a
decisao de projeto que mais importa aqui. A alternativa — mandar os candles e
calcular RSI e MACD no navegador — seria uma SEGUNDA implementacao da mesma
matematica, e este projeto ja sabe o que custa: o `jobs/compor.js` precisou de
um portao de conformidade rodando as duas versoes sobre a mesma grade porque
duas implementacoes divergem no primeiro ajuste, e o sintoma e o pior
possivel (a pagina e a CLI discordando sem ninguem saber qual esta certa).

O preco disso e que trocar o periodo de um indicador exige regerar a pagina.
Em troca, o numero da tela e exatamente o numero que o Python calculou, e os
periodos vao ESCRITOS na legenda — indicador sem periodo declarado e um numero
que ninguem consegue conferir.

**O JS que sobra e desenho e interacao**: escala, pan, zoom, crosshair. Toda
serie que ele desenha vem de `DADOS`, e ha teste procurando no corpo do script
a reintroducao das contas (`_PROIBIDO_NO_JS` em `tests/test_grafico_render`) —
nao e prova formal, e um alarme barato contra a segunda implementacao voltar
por descuido.

---

**O eixo X e o indice da barra, nao o relogio.** A bolsa fecha 17h por dia e
o fim de semana inteiro; num eixo temporal a serie viraria tracinhos separados
por vaos maiores que o proprio pregao. E o que toda plataforma faz, e a
consequencia esta declarada: **a distancia horizontal entre dois candles nao e
tempo.**

**So o 1h e coletado; 4h, 1d, 3d, 5d e 1sem sao derivados.** O `acoes_ohlc`
so guarda 1h, e a cascata e sempre 1h -> 1d -> N. O diario sai de
`acoes.agregar(por=99, quebrar_no_dia=True)`, que nunca atinge 99 e por isso
fecha exatamente um bloco por dia de pregao; como a coleta usa
`includePrePost=false`, ele e o do **pregao regular** e nao o dia de
calendario. O de 4h quebra no fim do pregao pelo motivo que `acoes.agregar`
documenta: um bloco atravessando o fechamento esconderia 17h de mercado
fechado dentro de uma barra rotulada 4h.

**3d e 5d contam PREGOES e 1sem conta CALENDARIO**, e a diferenca aparece na
semana de feriado: o bloco de calendario fecha com quatro barras, o de
contagem atravessa o feriado e junta cinco. `acoes.agrupar_por` existe para
essa segunda pergunta e o campo `barras` de cada bloco diz quantos entraram.

---

**Esta e a bolsa, e a faixa nao vive aqui.** O basis entre o preco da acao e o
preco do token na pool tem desvio de 1,42% e chega a 3%: um nivel tracado
neste grafico chega na pool deslocado. A pagina diz isso em vez de deixar o
leitor supor, e e o mesmo motivo pelo qual "suporte e resistencia desenhados"
esta no `POR_QUE_NAO` de `macro/tecnica.py`.
"""

from __future__ import annotations

import datetime as dt
import html
import json
import math

from defime.macro import indicadores as I

# Quanto tempo uma serie pode ficar sem barra nova antes de a pagina DIZER que
# esta velha. Generoso porque a bolsa fecha: sexta 16:00 ET ate segunda 9:30
# ET sao 65h, e um feriado emenda mais um dia. Mesma constante do
# `macro/acoes.IDADE_MAXIMA_H`, e pelo mesmo motivo — dado velho entregue em
# silencio e pior que dado ausente.
IDADE_MAXIMA_H = 96.0

# Medias publicadas. Sao as classicas e vao na legenda com o periodo.
EMAS = (9, 21, 50)
SMAS = (200,)


def _e(s) -> str:
    return html.escape(str(s if s is not None else ""))


def _r(v, casas=4):
    """`None` vira `null` no JSON — nunca zero, que o desenho leria como
    medicao. E o arredondamento existe porque o tamanho da pagina e o preco de
    tres anos de barras."""
    return None if v is None else round(float(v), casas)


def _casas_de(precos) -> int:
    """Casas decimais que preservam ~6 digitos significativos do nivel de
    preco. Fixar em 4 desperdicaria bytes numa acao de $958 e seria pouco num
    token de centavos — e nenhum dos dois e escolha, o nivel decide."""
    vivos = [abs(p) for p in precos if p]
    if not vivos:
        return 4
    nivel = sorted(vivos)[len(vivos) // 2]
    return max(0, min(8, 6 - int(math.floor(math.log10(nivel))) - 1))


def _serie_de(barras, ultimas: int | None = None) -> dict:
    """Candles + todos os indicadores, em arrays paralelos.

    Arrays paralelos e nao lista de objetos: com 5 mil barras a diferenca de
    tamanho da pagina e de fator ~3, e o alinhamento fica garantido por
    construcao — todo array tem o comprimento de `t`, porque
    `macro/indicadores` devolve assim.

    **`ultimas` corta DEPOIS do calculo, e a ordem e o ponto todo.** Cortar as
    barras antes faria a SMA 200 e o canal gaussiano (144) comecarem a aquecer
    dentro da janela visivel: as primeiras ~200 barras do desenho sairiam sem
    media longa, ou pior, com uma media longa montada so com o pedaco — um
    numero que parece medido e foi feito com menos amostra do que o nome diz.
    Calculando na serie inteira e fatiando no fim, o primeiro candle visivel ja
    tem os tres anos atras dele.
    """
    t = [b.ts for b in barras]
    o = [b.abertura for b in barras]
    h = [b.maxima for b in barras]
    ll = [b.minima for b in barras]
    c = [b.fechamento for b in barras]
    v = [b.volume for b in barras]
    ca = _casas_de(c)

    m = I.macd(c)
    s = I.stoch_rsi(c)
    bb = I.bollinger(c)
    dc = I.donchian(h, ll)
    gc = I.gaussiano(h, ll, c)

    d = {
        "t": t,
        "o": [_r(x, ca) for x in o], "h": [_r(x, ca) for x in h],
        "l": [_r(x, ca) for x in ll], "c": [_r(x, ca) for x in c],
        "v": v,
        "rsi": [_r(x, 2) for x in I.rsi(c)],
        "stk": [_r(x, 2) for x in s["k"]], "std": [_r(x, 2) for x in s["d"]],
        "macd": [_r(x, ca) for x in m["macd"]],
        "sig": [_r(x, ca) for x in m["sinal"]],
        "hist": [_r(x, ca) for x in m["hist"]],
        "bbs": [_r(x, ca) for x in bb["sup"]],
        "bbm": [_r(x, ca) for x in bb["meio"]],
        "bbi": [_r(x, ca) for x in bb["inf"]],
        "dcs": [_r(x, ca) for x in dc["sup"]],
        "dci": [_r(x, ca) for x in dc["inf"]],
        "gcs": [_r(x, ca) for x in gc["sup"]],
        "gcm": [_r(x, ca) for x in gc["meio"]],
        "gci": [_r(x, ca) for x in gc["inf"]],
    }
    for n in EMAS:
        d[f"ema{n}"] = [_r(x, ca) for x in I.ema(c, n)]
    for n in SMAS:
        d[f"sma{n}"] = [_r(x, ca) for x in I.sma(c, n)]

    # Invariante de alinhamento, conferido no proprio gerador: um array curto
    # desenharia o indicador deslocado sobre os candles, calado. Conferido
    # ANTES do corte, porque e o calculo que ele vigia.
    n = len(t)
    for k, arr in d.items():
        if len(arr) != n:
            raise ValueError(f"serie {k!r} com {len(arr)} pontos, esperado {n}")

    if ultimas and n > ultimas:
        d = {k: arr[-ultimas:] for k, arr in d.items()}
        d["_cortada_de"] = n

    # **Quais series nao tem um unico ponto neste timeframe.** O `_` no nome
    # separa metadado de serie: sem ele, `_vazias` e `_cortada_de` entrariam
    # em qualquer varredura de "todos os arrays desta serie" e o invariante de
    # alinhamento passaria a comparar metadado com candle. Uma serie
    # semanal tem ~150 barras e a SMA 200 sai inteira nula; sem isto o botao
    # ligaria e a tela nao mudaria, que e indistinguivel de defeito. A pagina
    # desabilita o botao e diz que falta amostra — ausencia declarada em vez
    # de silencio, a mesma regra do "—" no painel.
    d["_vazias"] = sorted(k for k, arr in d.items()
                          if isinstance(arr, list) and arr
                          and all(x is None for x in arr))
    return d


def _idade(barras, agora: dt.datetime) -> dict:
    """A guarda de idade. Serie parada nao e recusada aqui — o grafico de tres
    anos continua valendo —, mas a pagina DECLARA de quando e a ultima barra.
    """
    if not barras:
        return {"horas": None, "velha": False, "ultima": None}
    horas = (agora.timestamp() - barras[-1].ts) / 3600
    return {"horas": horas, "velha": horas > IDADE_MAXIMA_H,
            "ultima": dt.datetime.fromtimestamp(barras[-1].ts,
                                                dt.UTC).isoformat()}


CSS = """
.gwrap{background:var(--card);border:1px solid var(--rule);border-radius:12px;
  box-shadow:var(--sh);overflow:hidden;margin:18px 0}
.gbar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;
  padding:10px 12px;border-bottom:1px solid var(--rule);background:var(--rule2)}
.gbar .sep{width:1px;height:22px;background:var(--rule);margin:0 4px}
.gsym{font-weight:700;font-size:16px;margin-right:6px}
.gb{font:600 12px/1 "IBM Plex Sans",system-ui,sans-serif;color:var(--ink2);
  background:var(--card);border:1px solid var(--rule);border-radius:6px;
  padding:6px 9px;cursor:pointer;white-space:nowrap}
.gb:hover{border-color:var(--accent);color:var(--ink)}
.gb[aria-pressed="true"]{background:var(--accent-bg);border-color:var(--accent);
  color:var(--accent)}
.gleg{display:flex;flex-wrap:wrap;gap:14px;padding:8px 12px;font-size:12px;
  border-bottom:1px solid var(--rule);min-height:34px;align-items:center}
.gleg b{font-weight:600}
.gleg .k{color:var(--ink3);margin-right:4px}
.gcv{position:relative;width:100%;height:640px;touch-action:none;cursor:crosshair}
.gcv canvas{position:absolute;inset:0;width:100%;height:100%}
.gft{padding:9px 12px;font-size:12px;color:var(--ink3);
  border-top:1px solid var(--rule);background:var(--rule2)}
.gdot{display:inline-block;width:9px;height:3px;border-radius:2px;
  vertical-align:middle;margin-right:4px}
@media(max-width:760px){.gcv{height:480px}.gsym{font-size:14px}}
"""


def _js(dados: dict) -> str:
    """**`window.DADOS` e nao `const DADOS`**, e a diferenca ja custou uma
    tela em branco.

    `const` no escopo global cria uma binding no ambiente lexical e **nao** uma
    propriedade de `window`: o `if(!window.DADOS) return` do desenho saia cedo,
    sem erro nenhum no console, e a pagina publicava a moldura vazia que este
    projeto ja aprendeu a ler como "o ativo nao andou". Por isso o guarda
    tambem deixou de ser um `return` mudo.
    """
    return ("<script>\nwindow.DADOS = "
            + json.dumps(dados, separators=(",", ":"))
            + ";\n" + _JS_CORPO + "\n</script>")


# O desenho e a interacao. **Nenhuma conta de dominio aqui** — os indicadores
# chegam prontos do Python. O teste `test_js_nao_recalcula_indicador` procura
# as contas de volta neste corpo.
_JS_CORPO = r"""
(function(){
const el = document.getElementById('gcv');
const leg0 = document.getElementById('gleg');

// **Sair calado aqui desenha uma moldura vazia**, e moldura vazia le-se como
// medicao — foi o que a pagina da UPS fez em 07/09 com dois pontos de serie.
// Se nao da para desenhar, a pagina DIZ o motivo.
function morrer(motivo){
  if(leg0) leg0.innerHTML = '<b class="neg">grafico nao desenhou:</b> '
    + motivo + '. O resto da pagina continua valendo.';
  if(el) el.style.display = 'none';
}
if(!el){ return; }
if(!window.DADOS || !window.DADOS.series){
  morrer('os dados nao chegaram ao navegador'); return; }
const tfs0 = Object.keys(window.DADOS.series);
if(!tfs0.length || !window.DADOS.series[tfs0[0]].t.length){
  morrer('a serie veio sem barras'); return; }

const cv = document.createElement('canvas'); el.appendChild(cv);
const cx = cv.getContext('2d');
const leg = document.getElementById('gleg');

const COR = k => getComputedStyle(document.documentElement)
  .getPropertyValue(k).trim();

// Overlays do painel de preco e paineis proprios. `on` e o estado inicial:
// EMA 9/21 e volume ligados, o resto desligado — uma tela que abre com tudo
// aceso nao deixa ver o preco.
const OVER = [
  {id:'ema9',  rot:'EMA 9',   cor:'#E8A33D', ks:['ema9']},
  {id:'ema21', rot:'EMA 21',  cor:'#6BB3DD', ks:['ema21']},
  {id:'ema50', rot:'EMA 50',  cor:'#B07CD8', ks:['ema50']},
  {id:'sma200',rot:'SMA 200', cor:'#9AA7B2', ks:['sma200']},
  {id:'boll',  rot:'Bollinger 20,2', cor:'#5CC495', ks:['bbs','bbm','bbi']},
  {id:'donch', rot:'Donchian 20',    cor:'#D98A6B', ks:['dcs','dci']},
  {id:'gauss', rot:'Gaussian 4,144', cor:'#7EC8C0', ks:['gcs','gcm','gci']}
];
const PAIN = [
  {id:'vol',   rot:'Volume',    alt:0.16},
  {id:'rsi',   rot:'RSI 14',    alt:0.20},
  {id:'stoch', rot:'Stoch RSI', alt:0.20},
  {id:'macd',  rot:'MACD',      alt:0.20}
];

const S = {
  tf: Object.keys(DADOS.series)[0],
  on: {ema9:1, ema21:1, vol:1, rsi:1},
  i0: 0, i1: 0, cur: null, arrasto: null
};

const D = () => DADOS.series[S.tf];
const N = () => D().t.length;

function inicial(){
  const n = N(), vis = Math.min(n, DADOS.barras_iniciais || 160);
  S.i1 = n; S.i0 = Math.max(0, n - vis);
}

// ---------------------------------------------------------------- geometria
const ESQ = 0, DIR = 66, TOPO = 8, BASE = 22, GAP = 6;

// O preco nunca fica com menos de 45% da altura. Com os quatro paineis
// ligados as fracoes somam 0,76 e sobrava uma tira de ~100px para os candles
// — a tela ficava cheia de indicador e sem o que eles descrevem. Passando do
// teto, os paineis encolhem TODOS na mesma proporcao, entao a ordem de
// tamanho entre eles nao muda.
const PRECO_MIN = 0.45;

function paineis(){
  const ativos = PAIN.filter(p => S.on[p.id]);
  const H = Math.max(120, cv.clientHeight - TOPO - BASE - GAP*ativos.length);
  let soma = ativos.reduce((a,p) => a + p.alt, 0);
  const teto = 1 - PRECO_MIN;
  const escala = soma > teto ? teto / soma : 1;
  const hPreco = H * (1 - soma*escala);
  const out = [{id:'preco', y: TOPO, h: hPreco}];
  let y = TOPO + hPreco + GAP;
  for(const p of ativos){
    const h = p.alt * escala * H;
    out.push({id:p.id, rot:p.rot, y:y, h:h});
    y += h + GAP;
  }
  return out;
}

const W = () => cv.clientWidth - ESQ - DIR;
const passo = () => W() / Math.max(1, S.i1 - S.i0);
const xDe = i => ESQ + (i - S.i0 + 0.5) * passo();
const iDe = x => Math.floor((x - ESQ) / passo()) + S.i0;

function faixa(pn){
  const d = D(); let lo = Infinity, hi = -Infinity;
  const ver = v => { if(v==null||!isFinite(v)) return;
                     if(v<lo) lo=v; if(v>hi) hi=v; };
  for(let i=Math.max(0,S.i0); i<Math.min(N(),S.i1); i++){
    if(pn.id==='preco'){
      ver(d.h[i]); ver(d.l[i]);
      for(const o of OVER) if(S.on[o.id]) for(const k of o.ks) ver(d[k][i]);
    } else if(pn.id==='vol'){ lo=0; ver(d.v[i]); }
    else if(pn.id==='rsi'){ return {lo:0, hi:100}; }
    else if(pn.id==='stoch'){ return {lo:0, hi:100}; }
    else if(pn.id==='macd'){ ver(d.macd[i]); ver(d.sig[i]); ver(d.hist[i]); }
  }
  if(!isFinite(lo)||!isFinite(hi)) return {lo:0, hi:1};
  if(lo===hi){ lo-=1; hi+=1; }
  const folga = (hi-lo)*0.06;
  return {lo: lo-folga, hi: hi+folga};
}

const yDe = (v, pn, f) => pn.y + pn.h - (v - f.lo) / (f.hi - f.lo) * pn.h;

// ----------------------------------------------------------------- desenho
function linha(ks, cor, pn, f, larg){
  const d = D();
  cx.strokeStyle = cor; cx.lineWidth = larg || 1.3;
  for(const k of ks){
    cx.beginPath(); let abriu = false;
    for(let i=Math.max(0,S.i0); i<Math.min(N(),S.i1); i++){
      const v = d[k][i];
      if(v==null){ abriu = false; continue; }
      const x = xDe(i), y = yDe(v, pn, f);
      if(!abriu){ cx.moveTo(x,y); abriu = true; } else cx.lineTo(x,y);
    }
    cx.stroke();
  }
}

// `niveis` fixa os valores da grade em vez de dividir a faixa: num oscilador
// limitado a 0..100 a divisao automatica publica 66,667 e 33,333, que sao os
// tercos do painel e nao os limiares que alguem le. Onde o eixo e livre
// (preco, volume, MACD) a divisao continua automatica.
function grade(pn, f, niveis){
  cx.strokeStyle = COR('--rule2'); cx.lineWidth = 1;
  cx.fillStyle = COR('--ink3');
  cx.font = '11px "IBM Plex Mono",ui-monospace,monospace';
  cx.textAlign = 'left'; cx.textBaseline = 'middle';
  let vs = niveis;
  if(!vs){
    const n = Math.max(2, Math.round(pn.h / 46));
    vs = [];
    for(let g=0; g<=n; g++) vs.push(f.lo + (f.hi - f.lo) * g / n);
  }
  for(const v of vs){
    const yb = yDe(v,pn,f), y = Math.round(yb) + .5;
    if(yb < pn.y - 0.5 || yb > pn.y + pn.h + 0.5) continue;
    cx.beginPath(); cx.moveTo(ESQ, y); cx.lineTo(ESQ + W(), y); cx.stroke();
    // **Rotulo colado na borda do painel colide com o do painel vizinho** —
    // foi o "719,89" por cima do "221,6M" na primeira versao. Perto da borda
    // a linha fica e o numero sai: a grade continua legivel e nada se
    // sobrepoe.
    if(yb > pn.y + 8 && yb < pn.y + pn.h - 8)
      cx.fillText(fmt(v, pn.id), ESQ + W() + 6, y);
  }
}

function fmt(v, painel){
  if(v==null) return '—';
  if(painel==='vol'){
    const a = Math.abs(v);
    if(a>=1e9) return (v/1e9).toFixed(2)+'B';
    if(a>=1e6) return (v/1e6).toFixed(1)+'M';
    if(a>=1e3) return (v/1e3).toFixed(0)+'k';
    return v.toFixed(0);
  }
  // Oscilador limitado tem duas casas e ponto final: 64,200 sugere uma
  // precisao que o indicador nao tem.
  if(painel==='rsi' || painel==='stoch') return v.toFixed(2).replace('.',',');
  const a = Math.abs(v);
  return v.toFixed(a>=100?2 : a>=1?3 : 4).replace('.',',');
}

function candles(pn, f){
  const d = D(), p = passo(), larg = Math.max(1, Math.min(p*0.7, 18));
  const fino = p < 3;
  for(let i=Math.max(0,S.i0); i<Math.min(N(),S.i1); i++){
    const sobe = d.c[i] >= d.o[i];
    const cor = sobe ? COR('--up') : COR('--down');
    const x = xDe(i);
    cx.strokeStyle = cor; cx.fillStyle = cor;
    cx.lineWidth = Math.max(1, Math.min(2, p*0.14));
    cx.beginPath();
    cx.moveTo(Math.round(x)+.5, yDe(d.h[i],pn,f));
    cx.lineTo(Math.round(x)+.5, yDe(d.l[i],pn,f));
    cx.stroke();
    if(fino) continue;
    const ya = yDe(d.o[i],pn,f), yf = yDe(d.c[i],pn,f);
    cx.fillRect(Math.round(x-larg/2), Math.round(Math.min(ya,yf)),
                Math.round(larg), Math.max(1, Math.abs(yf-ya)));
  }
}

function volume(pn){
  const d = D(); let hi = 0;
  for(let i=Math.max(0,S.i0); i<Math.min(N(),S.i1); i++)
    if(d.v[i] > hi) hi = d.v[i];
  if(!hi) hi = 1;
  const f = {lo:0, hi:hi*1.05};
  grade(pn, f);
  const p = passo(), larg = Math.max(1, Math.min(p*0.7, 18));
  for(let i=Math.max(0,S.i0); i<Math.min(N(),S.i1); i++){
    cx.fillStyle = d.c[i] >= d.o[i] ? COR('--up-bg') : COR('--down-bg');
    cx.globalAlpha = .95;
    const y = yDe(d.v[i], pn, f);
    cx.fillRect(Math.round(xDe(i)-larg/2), Math.round(y),
                Math.round(larg), Math.round(pn.y+pn.h-y));
    cx.globalAlpha = 1;
  }
}

function nivel(pn, v, cor, tracejado){
  const f = {lo:0, hi:100}, y = Math.round(yDe(v,pn,f))+.5;
  cx.save(); cx.strokeStyle = cor; cx.lineWidth = 1;
  if(tracejado) cx.setLineDash([3,3]);
  cx.beginPath(); cx.moveTo(ESQ,y); cx.lineTo(ESQ+W(),y); cx.stroke();
  cx.restore();
}

function macdPainel(pn){
  const d = D(), f = faixa(pn);
  grade(pn, f);
  const p = passo(), larg = Math.max(1, Math.min(p*0.7, 14));
  const zero = yDe(0, pn, f);
  for(let i=Math.max(0,S.i0); i<Math.min(N(),S.i1); i++){
    const v = d.hist[i]; if(v==null) continue;
    cx.fillStyle = v >= 0 ? COR('--up') : COR('--down');
    cx.globalAlpha = .55;
    const y = yDe(v, pn, f);
    cx.fillRect(Math.round(xDe(i)-larg/2), Math.round(Math.min(y,zero)),
                Math.round(larg), Math.max(1,Math.abs(zero-y)));
    cx.globalAlpha = 1;
  }
  cx.save(); cx.strokeStyle = COR('--rule'); cx.setLineDash([2,3]);
  cx.beginPath(); cx.moveTo(ESQ, zero+.5); cx.lineTo(ESQ+W(), zero+.5);
  cx.stroke(); cx.restore();
  linha(['macd'], '#6BB3DD', pn, f, 1.4);
  linha(['sig'],  '#E8A33D', pn, f, 1.4);
}

function eixoTempo(){
  const d = D(), y = cv.clientHeight - BASE;
  cx.fillStyle = COR('--ink3'); cx.textAlign = 'center';
  cx.textBaseline = 'top';
  cx.font = '11px "IBM Plex Mono",ui-monospace,monospace';
  cx.strokeStyle = COR('--rule');
  cx.beginPath(); cx.moveTo(ESQ,y+.5); cx.lineTo(ESQ+W(),y+.5); cx.stroke();
  const alvo = Math.max(2, Math.floor(W()/120));
  const salto = Math.max(1, Math.ceil((S.i1-S.i0)/alvo));
  let ant = '';
  for(let i=Math.max(0,S.i0); i<Math.min(N(),S.i1); i+=salto){
    const x = xDe(i); if(x < 30 || x > ESQ+W()-30) continue;
    const r = rot(d.t[i]);
    if(r === ant) continue; ant = r;
    cx.fillText(r, x, y+5);
  }
}

// A data sai em Brasilia, que e onde o usuario le. O banco e UTC, sem
// excecao — a conversao acontece so na saida, como no resto do projeto.
function dataBR(ts){ return new Date(ts*1000)
  .toLocaleString('pt-BR', {timeZone:'America/Sao_Paulo'}); }
function rot(ts){
  const o = new Date(ts*1000);
  const p = o.toLocaleString('pt-BR',{timeZone:'America/Sao_Paulo',
    day:'2-digit', month:'2-digit'});
  if(S.tf === '1d') return p;
  return p + ' ' + o.toLocaleString('pt-BR',{timeZone:'America/Sao_Paulo',
    hour:'2-digit', minute:'2-digit'});
}

function crosshair(pns){
  if(S.cur == null) return;
  const i = S.cur;
  if(i < S.i0 || i >= Math.min(N(), S.i1)) return;
  const x = Math.round(xDe(i)) + .5;
  cx.save();
  cx.strokeStyle = COR('--ink3'); cx.setLineDash([3,3]); cx.lineWidth = 1;
  cx.beginPath(); cx.moveTo(x, TOPO); cx.lineTo(x, cv.clientHeight-BASE);
  cx.stroke();
  cx.restore();
  // etiqueta de tempo
  const txt = rot(D().t[i]);
  cx.font = '11px "IBM Plex Mono",ui-monospace,monospace';
  const w = cx.measureText(txt).width + 12;
  cx.fillStyle = COR('--ink');
  cx.fillRect(Math.min(Math.max(x-w/2, ESQ), ESQ+W()-w),
              cv.clientHeight-BASE, w, 17);
  cx.fillStyle = COR('--bg'); cx.textAlign='center'; cx.textBaseline='top';
  cx.fillText(txt, Math.min(Math.max(x, ESQ+w/2), ESQ+W()-w/2),
              cv.clientHeight-BASE+3);
}

function desenhar(){
  const dpr = window.devicePixelRatio || 1;
  const w = el.clientWidth, h = el.clientHeight;
  if(cv.width !== Math.round(w*dpr) || cv.height !== Math.round(h*dpr)){
    cv.width = Math.round(w*dpr); cv.height = Math.round(h*dpr);
    cv.style.width = w+'px'; cv.style.height = h+'px';
  }
  cx.setTransform(dpr,0,0,dpr,0,0);
  cx.clearRect(0,0,w,h);
  const pns = paineis();
  for(const pn of pns){
    if(pn.id === 'preco'){
      const f = faixa(pn); grade(pn,f); candles(pn,f);
      for(const o of OVER) if(S.on[o.id]) linha(o.ks, o.cor, pn, f);
    } else if(pn.id === 'vol'){ volume(pn); }
    else if(pn.id === 'rsi'){
      const f = {lo:0,hi:100}; grade(pn,f,[0,30,50,70,100]);
      nivel(pn,70,COR('--down'),true); nivel(pn,30,COR('--up'),true);
      linha(['rsi'], '#B07CD8', pn, f, 1.5);
    } else if(pn.id === 'stoch'){
      const f = {lo:0,hi:100}; grade(pn,f,[0,20,50,80,100]);
      nivel(pn,80,COR('--down'),true); nivel(pn,20,COR('--up'),true);
      linha(['stk'], '#6BB3DD', pn, f, 1.4);
      linha(['std'], '#E8A33D', pn, f, 1.4);
    } else if(pn.id === 'macd'){ macdPainel(pn); }
    if(pn.rot){
      cx.fillStyle = COR('--ink3'); cx.textAlign='left'; cx.textBaseline='top';
      cx.font = '600 11px "IBM Plex Sans",system-ui,sans-serif';
      cx.fillText(pn.rot, ESQ+6, pn.y+4);
    }
  }
  eixoTempo();
  crosshair(pns);
  legenda();
}

function legenda(){
  const d = D();
  const i = (S.cur!=null && S.cur>=0 && S.cur<N()) ? S.cur
          : Math.min(N(), S.i1) - 1;
  if(i < 0){ leg.textContent = 'sem barras.'; return; }
  const dif = d.o[i] ? (d.c[i]/d.o[i]-1)*100 : 0;
  const cls = dif >= 0 ? 'pos' : 'neg';
  const p = [];
  p.push('<span><span class="k">'+rot(d.t[i])+'</span></span>');
  for(const [k,r] of [['o','A'],['h','M'],['l','m'],['c','F']])
    p.push('<span class="k">'+r+'</span><b class="'+cls+'">'+
           fmt(d[k][i])+'</b>');
  p.push('<span class="k">Vol</span><b>'+fmt(d.v[i],'vol')+'</b>');
  p.push('<b class="'+cls+'">'+(dif>=0?'+':'−')+
         Math.abs(dif).toFixed(2).replace('.',',')+'%</b>');
  for(const o of OVER) if(S.on[o.id])
    p.push('<span class="gdot" style="background:'+o.cor+'"></span>'+
           '<span class="k">'+o.rot+'</span><b>'+fmt(d[o.ks[0]][i])+'</b>');
  if(S.on.rsi) p.push('<span class="k">RSI 14</span><b>'+
    fmt(d.rsi[i],'rsi')+'</b>');
  if(S.on.stoch) p.push('<span class="k">Stoch K/D</span><b>'+
    fmt(d.stk[i],'stoch')+' / '+fmt(d.std[i],'stoch')+'</b>');
  if(S.on.macd) p.push('<span class="k">MACD</span><b>'+fmt(d.macd[i])+
    ' / '+fmt(d.sig[i])+'</b>');
  leg.innerHTML = p.join(' ');
}

// --------------------------------------------------------------- interacao
function zoom(fator, ancora){
  const n = N(), vis = S.i1 - S.i0;
  const novo = Math.max(12, Math.min(n, Math.round(vis*fator)));
  if(novo === vis) return;
  const frac = Math.min(1, Math.max(0, (ancora - ESQ) / W()));
  let i0 = Math.round(S.i0 + (vis - novo) * frac);
  i0 = Math.max(0, Math.min(n - novo, i0));
  S.i0 = i0; S.i1 = i0 + novo;
  desenhar();
}

el.addEventListener('wheel', ev => {
  ev.preventDefault();
  const r = el.getBoundingClientRect();
  zoom(ev.deltaY > 0 ? 1.18 : 1/1.18, ev.clientX - r.left);
}, {passive:false});

function mover(ev){
  const r = el.getBoundingClientRect(), x = ev.clientX - r.left;
  if(S.arrasto != null){
    const dx = x - S.arrasto.x, n = N(), vis = S.i1 - S.i0;
    let i0 = Math.round(S.arrasto.i0 - dx / passo());
    i0 = Math.max(0, Math.min(n - vis, i0));
    S.i0 = i0; S.i1 = i0 + vis;
  }
  S.cur = iDe(x);
  desenhar();
}
el.addEventListener('pointerdown', ev => {
  const r = el.getBoundingClientRect();
  S.arrasto = {x: ev.clientX - r.left, i0: S.i0};
  el.setPointerCapture(ev.pointerId);
});
el.addEventListener('pointermove', mover);
el.addEventListener('pointerup', () => { S.arrasto = null; });
el.addEventListener('pointercancel', () => { S.arrasto = null; });
el.addEventListener('pointerleave', () => { S.cur = null; desenhar(); });

document.addEventListener('keydown', ev => {
  const n = N(), vis = S.i1 - S.i0;
  let usou = true;
  if(ev.key === 'ArrowRight'){ const i0 = Math.min(n-vis, S.i0+1);
    S.i0=i0; S.i1=i0+vis; }
  else if(ev.key === 'ArrowLeft'){ const i0 = Math.max(0, S.i0-1);
    S.i0=i0; S.i1=i0+vis; }
  else if(ev.key === '+' || ev.key === '=') zoom(1/1.25, ESQ+W()/2);
  else if(ev.key === '-') zoom(1.25, ESQ+W()/2);
  else if(ev.key === 'Escape'){ S.cur = null; }
  else usou = false;
  if(usou){ ev.preventDefault(); desenhar(); }
});

// Botao de indicador sem amostra no timeframe corrente sai DESABILITADO com
// o motivo: a SMA 200 nao existe numa serie semanal de 153 barras, e um botao
// que acende sem mudar a tela le-se como defeito.
function ajustarBotoes(){
  const vazias = D()._vazias || [];
  for(const b of document.querySelectorAll('[data-ind]')){
    const o = OVER.find(x => x.id === b.dataset.ind);
    const ks = o ? o.ks : ({rsi:['rsi'], stoch:['stk'], macd:['macd'],
                            vol:['v']})[b.dataset.ind] || [];
    const falta = ks.length && ks.every(k => vazias.includes(k));
    b.disabled = falta;
    b.style.opacity = falta ? .38 : '';
    b.style.cursor = falta ? 'not-allowed' : '';
    b.title = falta ? 'sem amostra em ' + S.tf
                      + ': a serie e mais curta que o periodo do indicador'
                    : '';
  }
}

// botoes
document.querySelectorAll('[data-tf]').forEach(b => {
  b.addEventListener('click', () => {
    S.tf = b.dataset.tf; inicial();
    document.querySelectorAll('[data-tf]').forEach(
      o => o.setAttribute('aria-pressed', o === b ? 'true' : 'false'));
    ajustarBotoes();
    desenhar();
  });
});
document.querySelectorAll('[data-ind]').forEach(b => {
  b.addEventListener('click', () => {
    const k = b.dataset.ind;
    S.on[k] = !S.on[k];
    b.setAttribute('aria-pressed', S.on[k] ? 'true' : 'false');
    desenhar();
  });
});
const btZoom = document.getElementById('gtudo');
if(btZoom) btZoom.addEventListener('click', () => {
  S.i0 = 0; S.i1 = N(); desenhar(); });
const btFim = document.getElementById('gfim');
if(btFim) btFim.addEventListener('click', () => { inicial(); desenhar(); });

new ResizeObserver(() => desenhar()).observe(el);
inicial(); ajustarBotoes(); desenhar();
})();
"""


def _botoes(series: dict) -> str:
    tfs = "".join(
        f'<button class="gb" data-tf="{_e(k)}" '
        f'aria-pressed="{"true" if i == 0 else "false"}">{_e(k)}</button>'
        for i, k in enumerate(series))
    ligado = {"ema9", "ema21", "vol", "rsi"}
    ind = "".join(
        f'<button class="gb" data-ind="{_e(k)}" '
        f'aria-pressed="{"true" if k in ligado else "false"}">{_e(r)}</button>'
        for k, r in [("ema9", "EMA 9"), ("ema21", "EMA 21"),
                     ("ema50", "EMA 50"), ("sma200", "SMA 200"),
                     ("boll", "Bollinger"), ("donch", "Donchian"),
                     ("gauss", "Gaussian"), ("vol", "Volume"),
                     ("rsi", "RSI"), ("stoch", "Stoch RSI"),
                     ("macd", "MACD")])
    return (f'<div class="gbar"><span class="gsym">{{sym}}</span>{tfs}'
            f'<span class="sep"></span>{ind}<span class="sep"></span>'
            f'<button class="gb" id="gfim">recente</button>'
            f'<button class="gb" id="gtudo">tudo</button></div>')


def bloco(simbolo: str, series: dict, idade: dict,
          barras_iniciais: int = 160) -> str:
    """O grafico inteiro: barra de botoes, legenda, canvas e o rodape.

    `series` e `{rotulo_do_timeframe: serie_de()}`. A ordem manda: o primeiro
    abre selecionado.
    """
    dados = {"simbolo": simbolo, "series": series,
             "barras_iniciais": barras_iniciais}
    aviso = ""
    if idade.get("velha"):
        aviso = (f'<p class="nota warn">A ultima barra tem '
                 f'{idade["horas"]:.0f} h. A serie esta parada — o desenho '
                 f'e verdadeiro ate ali e nao inclui o que veio depois. '
                 f'Rode <code>python run_acoes.py --simbolos {_e(simbolo)}'
                 f'</code>.</p>')
    # O corte de janela vai DITO, com o total de onde ele saiu. Publicar
    # "2.000 barras" sem dizer que ha 5.081 deixaria o leitor concluir que a
    # serie acaba ali — e a serie inteira esta no 4h e no diario, ao lado.
    partes = []
    for k, v in series.items():
        n = len(v["t"])
        txt = f"{k} {n:,} barras".replace(",", ".")
        if v.get("_cortada_de"):
            txt += (" (as mais recentes de "
                    + f"{v['_cortada_de']:,}".replace(",", ".") + ")")
        partes.append(txt)
    conta = " · ".join(partes)
    return (f'{aviso}<div class="gwrap">'
            + _botoes(series).replace("{sym}", _e(simbolo))
            + '<div class="gleg" id="gleg"></div>'
            + '<div class="gcv" id="gcv" tabindex="0"></div>'
            + f'<div class="gft">{conta}. Arraste para andar, '
              f'role para dar zoom, setas movem uma barra, '
              f'Esc solta o cursor. '
              f'<b>O eixo horizontal e o numero da barra, nao o relogio</b> — '
              f'a bolsa fecha 17 h por dia e o fim de semana inteiro.</div>'
            + '</div>' + _js(dados))


__all__ = ["bloco", "CSS", "_serie_de", "_idade", "IDADE_MAXIMA_H",
           "EMAS", "SMAS"]
