"""A pagina `/heat/` — heatmap dos tokens da chain, no estilo do coin360.

    python -m defime.jobs.heatmap                       # top 10 por TVL
    python -m defime.jobs.heatmap --top 16 --saida ./site/heat --grafico ./site/grafico

**Duas chamadas de RPC por ciclo, e elas compram o "agora".** Area e volume
saem de `pool_hora` (o que `python -m defime.jobs.pools_hora` ja grava); o PRECO sai do estado
do contrato, porque `pool_hora` e horaria e publicava a cor do tile com **52
min de atraso mediano** numa chain que negocia 24/7 — medido em 24/09, com
172 min no pior caso. `precos_do_contrato` le as 45 pools num bloco so
(`aggregate3`), e o custo medido e **0,41 s**.

Se a leitura falhar, o preco volta para `pool_hora` e o tile DIZ de onde veio:
degradar para o dado velho e aceitavel, degradar em silencio nao. Se este job
falhar inteiro, nada de producao para.

---

**O tile e o TOKEN, nao a pool.** Esta chain tem tres pools de SPY, duas de
GOOGL e duas de RBLX; a agregacao mora em `defime/heatmap.agregar`, que soma
TVL e volume e tira preco e variacao da pool DOMINANTE — com o endereco dela
no `title`, para o numero ser rastreavel.

**O clique abre `/grafico/<simbolo>/`, e so quando a pagina EXISTE.** O mapa
de links sai de quem sabe: os simbolos com serie em `acoes_ohlc` cruzados com
os que o `defime.jobs.grafico` publicou. Montar o href por convencao de nome
produziria 404 para SPCX e SGOV, que negociam na chain e nao tem acao na
bolsa — e link quebrado nao levanta nada.

**`--top` corta por TVL e o corte e DITO.** Dez tiles e o padrão
e o que cabe numa tela de celular sem o simbolo sumir; o rodape publica
quantos tokens ficaram de fora, porque um heatmap que omite em silencio faz
o leitor concluir que a chain tem dez tokens.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

RAIZ = Path.cwd()
ESTILO = Path(__file__).resolve().parents[1] / "web" / "estilo.css"
os.environ.setdefault("LOG_JSON", "true")

from defime import heatmap as H                                   # noqa: E402
from defime.db.registro import gravar_batimento                   # noqa: E402
from defime.macro import noticias_banco as NB                     # noqa: E402
from defime.logging import get_logger                             # noqa: E402

from defime.web import heatmap_render as R                       # noqa: E402
from defime.web import noticias_render as NR                     # noqa: E402

log = get_logger(__name__)

BD = str(RAIZ / "defime.db")
SAIDA = Path(os.environ.get("HEAT_SAIDA", RAIZ / "site" / "heat"))

# Quanto da serie de `pool_hora` ler. 30 h e a janela de 24 h mais folga para
# achar o ponto de partida quando ha buraco — a serie e horaria e o
# `pools_hora` as vezes atrasa um ciclo.
JANELA_H = 30

# Idade maxima do ultimo ponto para o token entrar. Acima disso a pool nao
# esta sendo medida, e publicar o preco dela ao lado dos outros poria dado de
# ontem na mesma tela que o de agora — a familia numero um deste projeto.
IDADE_MAXIMA_H = 3.0

# O mesmo, para a BOLSA — e generoso porque ela FECHA: de sexta 16:00 ET a
# segunda 9:30 ET sao 65 h, e um feriado emenda mais um dia. Usar aqui o
# limiar da pool (3 h) apagaria todo tile de acao fora do pregao, que e a
# maior parte do tempo. Mesma constante do `macro/acoes.IDADE_MAXIMA_H`.
IDADE_BOLSA_H = 96.0

# Ate quando a barra de 1 m e "o pregao em curso". Ela NAO decide qual fonte
# usar — isso e do relogio, e a mais nova ganha sempre; decide so o rotulo do
# tile, entre "ha 2 min, pregao em curso" e o fechamento que ja passou.
IDADE_1M_MIN = 15.0


def _serie_por_pool(bd: str, agora: int,
                    contrato: dict | None = None) -> dict[str, dict]:
    """Preco, TVL, volume e variacao de 24 h, por POOL.

    A variacao compara o ultimo preco com o ponto mais proximo de 24 h atras
    **que exista**, e devolve junto quantas horas a serie de fato cobriu —
    quem decide se isso vale como "24 h" e `heatmap.agregar`, com o limiar
    declarado la. Aqui nao se inventa ponto nem se interpola.
    """
    c = sqlite3.connect(bd)
    try:
        # As tres tabelas sao pre-requisito e a falta de uma tem de DIZER
        # qual, com o comando que a preenche: "no such table" sozinho manda
        # quem le procurar em 40 arquivos. O banco de desenvolvimento deste
        # projeto costuma estar atrasado, entao isto acontece de verdade.
        tem = {n for (n,) in c.execute(
            "select name from sqlite_master where type='table'")}
        falta = {"pool_hora", "pool_param", "token_simbolo"} - tem
        if falta:
            raise RuntimeError(
                f"faltam as tabelas {sorted(falta)} neste banco. "
                f"`token_simbolo` vem de `python -m defime.jobs.simbolos`; "
                f"`pool_hora`/`pool_param`, de `python -m defime.jobs.pools_hora`.")
        sim = {a: s for a, s in
               c.execute("select endereco, simbolo from token_simbolo")}
        pools = c.execute(
            "select p.pool, p.token0, p.token1, p.usdg0, p.versao, p.taxa"
            "  from pool_param p").fetchall()
        desde = agora - JANELA_H * 3600
        saida: dict[str, dict] = {}
        for pool, t0, t1, u0, versao, taxa in pools:
            pts = c.execute(
                "select ts, preco, tvl_usd, volume_usd from pool_hora"
                " where pool = ? and ts > ? order by ts",
                (pool, desde)).fetchall()
            com_preco = [p for p in pts if p[1]]
            if not com_preco:
                continue
            ultimo = com_preco[-1]
            if (agora - ultimo[0]) / 3600 > IDADE_MAXIMA_H:
                continue

            # O ponto de partida: o mais proximo de 24 h atras, sem inventar.
            alvo = agora - 86400
            base = min(com_preco, key=lambda p: abs(p[0] - alvo))

            # **O "agora" vem do CONTRATO quando ele foi lido.** `pool_hora`
            # e horaria e publicava o preco com 52 min de atraso mediano numa
            # chain que negocia 24/7; `precos_do_contrato` le o bloco
            # corrente. O ponto de PARTIDA continua vindo do banco — nao ha
            # como ler o preco de 24 h atras do estado de agora.
            agora_preco, agora_ts, fonte = ultimo[1], ultimo[0], "pool_hora"
            do_contrato = (contrato or {}).get(pool)
            if do_contrato and do_contrato.get("preco"):
                agora_preco, agora_ts = do_contrato["preco"], agora
                fonte = "contrato"

            horas = (agora_ts - base[0]) / 3600
            var = motivo = None
            if base[0] >= agora_ts:
                motivo = "só um ponto com preço na janela"
            elif base[1]:
                var = (agora_preco / base[1] - 1) * 100

            vol = sum((p[3] or 0) for p in pts if p[0] > agora - 86400)
            alvo_tok = t1 if u0 else t0
            saida[pool] = {
                "simbolo": sim.get(alvo_tok),
                "tvl_usd": ultimo[2] or 0.0,
                "volume_24h_usd": vol,
                "preco": agora_preco,
                "preco_fonte": fonte,
                "tvl_idade_min": (agora - ultimo[0]) / 60,
                "variacao_24h_pct": var,
                "horas_cobertas": horas,
                "motivo": motivo,
                "versao": versao,
                "taxa_pct": (taxa or 0) * 100,
                "ts": ultimo[0],
            }
        return saida
    finally:
        c.close()


def precos_do_contrato(bd: str) -> dict[str, dict]:
    """Preco de AGORA de cada pool, lido do estado do contrato num bloco so.

    **`pool_hora` nao serve para "agora".** Ela e escrita de hora em hora e,
    medido em 24/09, publicava o preco com **52 min de atraso mediano e 172 no
    pior caso** — enquanto a chain negocia 24/7. Nessa janela o preco da QUBT
    anda 0,13% na mediana, **1,27% no p90 e 3,86% no maximo**: um em cada dez
    ciclos pintava o tile com mais de 1% de erro.

    O estado do contrato responde a mesma pergunta no bloco corrente:

      V3   `slot0()` de cada pool, todas num `aggregate3` do Multicall3
      V4   `extsload` do slot da pool no PoolManager, no mesmo lote

    Medido: **29 pools V3 em 2 chamadas de RPC, 0,41 s.** E o bloco e UM so
    para todas — parear o preco de uma pool de um bloco com o de outra de
    outro seria a familia numero um deste projeto, agora entre tiles do mesmo
    mapa.

    **Devolve so o PRECO.** TVL e volume continuam de `pool_hora`: o primeiro
    exige somar o livro inteiro e o segundo e uma janela, nenhum dos dois se
    le num `slot0`. A area do tile muda devagar; a cor, nao — e e a cor que
    esta errada com 52 min.
    """
    from defime.chain import abi_v4 as PV4
    from defime.chain import multicall as MC
    from defime.chain.rpc import RpcClient

    c = sqlite3.connect(bd)
    try:
        dec = {a: d for a, d in
               c.execute("select endereco, decimais from token_simbolo")}
        linhas = c.execute(
            "select pool, usdg0, token0, token1, spacing, taxa, versao"
            "  from pool_param").fetchall()
    finally:
        c.close()

    rpc = RpcClient()
    bloco = int(rpc.call("eth_blockNumber", []), 16)

    v3 = [r for r in linhas if (r[6] or "v3") == "v3" and len(r[0]) == 42]
    v4 = [r for r in linhas if (r[6] or "v3") == "v4" and len(r[0]) == 66]

    pedidos = [(r[0], SEL_SLOT0) for r in v3]
    pedidos += [(PV4.POOL_MANAGER,
                 PV4.SEL_EXTSLOAD + format(PV4.slot_da_pool(r[0]), "064x"))
                for r in v4]
    tem_v4 = True

    respostas = MC.aggregate3(rpc, pedidos, bloco)
    saida: dict[str, dict] = {}
    for (pool, u0, t0, t1, _sp, _tx, versao), bruto in zip(
            v3 + (v4 if tem_v4 else []), respostas):
        if not bruto or bruto == "0x":
            continue
        try:
            if (versao or "v3") == "v3":
                tick = _tick_de_slot0(bruto)
            else:
                palavra = int(bruto[2:66], 16)
                tick = _i24((palavra >> 160) & 0xFFFFFF)
            alvo = t1 if u0 else t0
            saida[pool] = {"preco": preco_do_tick(
                               tick, bool(u0), dec.get(t0 if u0 else t1) or 6,
                               dec.get(alvo) or 18),
                           "tick": tick, "bloco": bloco}
        except Exception:                                     # noqa: BLE001
            continue
    return saida


def preco_do_tick(tick: int, usdg_token0: bool, dec_stable: int = 6,
                  dec_ativo: int = 18) -> float:
    """Preco do ativo em dolar a partir do tick.

    A razao nativa do Uniswap e `token1/token0` em unidades ajustadas. Com a
    stable em token0 ela e "ativo por dolar" e o preco e o INVERSO; com a
    stable em token1 ela ja e "dolar por ativo". Supor a orientacao faz metade
    das pools da chain valer quase zero.
    """
    expo = dec_ativo - dec_stable
    return (1.0001 ** (-tick if usdg_token0 else tick)) * (10 ** expo)


def _i24(v: int) -> int:
    return v - (1 << 24) if v >= (1 << 23) else v


def _tick_de_slot0(bruto: str) -> int:
    """`slot0()` empacota sqrtPriceX96 e tick na MESMA palavra na V3 canonica,
    mas esta chain devolve os campos em palavras separadas — o tick e a
    palavra 1. Ler da palavra errada ja custou "mediana 100,000%" com p10 de
    −2,3 trilhoes por cento num spike de 09/09."""
    return _i24(int(bruto[66:130], 16) & 0xFFFFFF)


SEL_SLOT0 = "0x3850c7bd"


def ultimas_de_1m(c: sqlite3.Connection, agora: int) -> dict[str, dict]:
    """Ultima barra de 1m de cada simbolo, com o volume financeiro do dia dela.

    **Duas consultas, e nao uma por simbolo.** O coletor de 1 m (`defime.jobs.acoes`) grava ~10 mil
    barras por dia de pregao; varrer dois dias inteiros a cada ciclo de 5 min
    para achar a ponta de cada serie custaria mais do que a propria coleta.
    """
    if not c.execute("select 1 from acoes_ohlc where intervalo = '1m' "
                     "limit 1").fetchone():
        return {}
    pontas = dict(c.execute(
        "select simbolo, max(ts) from acoes_ohlc"
        " where intervalo = '1m' and ts > ? group by simbolo",
        (agora - 2 * 86400,)))
    saida: dict[str, dict] = {}
    for s, ts in pontas.items():
        if ts is None:
            continue
        dia = dt.datetime.fromtimestamp(ts, dt.UTC).date()
        ini = int(dt.datetime.combine(dia, dt.time(), dt.UTC).timestamp())
        linha = c.execute(
            "select fechamento from acoes_ohlc"
            " where simbolo = ? and intervalo = '1m' and ts = ?",
            (s, ts)).fetchone()
        if not linha or linha[0] is None:
            continue
        vol = c.execute(
            "select sum(volume * fechamento) from acoes_ohlc"
            " where simbolo = ? and intervalo = '1m' and ts >= ?",
            (s, ini)).fetchone()[0]
        saida[s] = {"ts": ts, "fechamento": linha[0],
                    "volume_usd": vol or 0.0, "dia": dia.isoformat()}
    return saida


def serie_da_bolsa(bd: str, agora: int) -> dict[str, dict]:
    """Preco, variacao e volume financeiro de cada acao, de `acoes_ohlc`.

    **Esta e a visao que inclui os tokens que este banco nao indexa na
    chain.** UPS, INTC e JNJ tem pool na Robinhood e nenhuma esta em
    `pool_events`; USAR so listou em 03/2025. Pela bolsa eles existem, e foi
    isso padrão.

    **A variacao e de FECHAMENTO a FECHAMENTO, e nao de 24 h de relogio.** A
    bolsa fecha: entre a ultima barra de sexta e a primeira de segunda passam
    65 horas, e uma janela de relogio ou mediria o nada ou atravessaria dois
    pregoes. Fechamento contra fechamento e o numero que todo heatmap de acao
    publica, e o tile diz que a janela e "pregao".

    **A area e o volume FINANCEIRO** (soma de `volume x fechamento` das barras
    do dia), nao a contagem de acoes: comparar 1 milhao de acoes de $17 com 1
    milhao de $270 pelo numero de acoes ordenaria os tiles pelo preco
    unitario, que nao e tamanho de nada.

    **Durante o pregao o "agora" vem da barra de 1 MINUTO**, coletada pelo
    coletor de 1 m (`defime.jobs.acoes`) — a de 1 h tem ate 60 minutos de atraso, e num pregao
    agitado isso e a diferenca entre publicar o que esta acontecendo e o que
    aconteceu. Ela so vale enquanto fresca (`IDADE_1M_MIN`); passado isso o
    tile volta para a serie horaria e DIZ de onde veio o preco, em vez de
    entregar em silencio a barra de sexta como se fosse de agora.

    **E a base da variacao e ancorada no DIA DA BARRA DE AGORA, nao no fim da
    serie horaria.** As 13:35 o coletor do pregao ainda nao rodou: a serie de
    1 h termina ONTEM enquanto a de 1 m ja virou o dia. Comparar o preco de
    hoje com o penultimo dia da serie horaria publicaria uma variacao de 48 h
    com o rotulo "pregao" — a familia numero um deste projeto, comparar dois
    instantes diferentes.
    """
    c = sqlite3.connect(bd)
    try:
        if not c.execute("select name from sqlite_master where type='table'"
                         " and name='acoes_ohlc'").fetchone():
            return {}
        # **Cripto fica de fora, e nao por gosto: a UNIDADE do volume e
        # outra.** O Yahoo reporta o volume de `BTC-USD` ja em dolares e o de
        # uma acao em NUMERO DE ACOES — multiplicar os dois por preco poe o
        # BTC em **$181 trilhoes** de "volume financeiro" contra $28 bi da
        # maior acao, e ele engole o mapa inteiro. Alem disso nao sao acoes
        # tokenizadas: estao em `acoes_ohlc` para converter gwei em dolar e
        # avaliar o colateral do Morpho, motivos declarados no `run_acoes`.
        simbolos = [s for (s,) in c.execute(
            "select distinct simbolo from acoes_ohlc where intervalo = '1h'")
            if not s.endswith("-USD")]
        de_1m = ultimas_de_1m(c, agora)
        saida: dict[str, dict] = {}
        for s in simbolos:
            # 12 dias cobrem feriado prolongado sem trazer historia inutil.
            linhas = c.execute(
                "select ts, fechamento, volume from acoes_ohlc"
                " where simbolo = ? and intervalo = '1h' and ts > ?"
                " order by ts", (s, agora - 12 * 86400)).fetchall()
            if len(linhas) < 2:
                continue
            dias: dict[str, list] = {}
            for ts, fech, vol in linhas:
                if fech is None:
                    continue
                dia = dt.datetime.fromtimestamp(ts, dt.UTC).date().isoformat()
                dias.setdefault(dia, []).append((ts, fech, vol or 0))
            if not dias:
                continue
            ordem = sorted(dias)
            hoje = dias[ordem[-1]]
            ultimo_ts, fech, _ = hoje[-1]
            dia_agora = ordem[-1]
            volume = sum(v * f for _, f, v in hoje)
            fonte_preco = "1h"

            m = de_1m.get(s)
            # **Ganha a MAIS NOVA das duas, e nao a que passa num limiar.** A
            # primeira versao disto exigia 15 min de frescor para aceitar o
            # 1 m e, de madrugada, publicava a barra de 1 h de 31 h atras
            # tendo a de 1 m de 7 h na mao: a guarda escolhia o dado MAIS
            # VELHO. `IDADE_1M_MIN` continua existindo — para dizer se o
            # rotulo e "pregao em curso" —, mas quem escolhe a fonte e o
            # relogio, e a idade resultante vai declarada no tile.
            if m and m["ts"] > ultimo_ts and m["dia"] >= dia_agora:
                ultimo_ts, fech = m["ts"], m["fechamento"]
                dia_agora, fonte_preco = m["dia"], "1m"
                # O volume acompanha o preco: com a barra de 1 m ja num dia
                # que a serie horaria nao tem, somar o volume de 1 h poria o
                # preco de hoje com o giro de ontem ao lado.
                volume = m["volume_usd"]
            idade_h = (agora - ultimo_ts) / 3600

            var = motivo = None
            # A base e o ultimo fechamento de um dia ANTERIOR ao de agora —
            # nunca `ordem[-2]`, que pula um pregao quando o 1 m ja virou o
            # dia e a serie horaria ainda nao.
            base = next((dias[d][-1][1] for d in reversed(ordem)
                         if d < dia_agora), None)
            if base:
                var = (fech / base - 1) * 100
            else:
                motivo = "só um pregão na janela"
            # A serie parada e declarada, nao descartada: o heatmap continua
            # util com o fechamento de sexta num sabado — o que nao pode e
            # deixar o leitor supor que e de agora.
            if idade_h > IDADE_BOLSA_H:
                motivo = f"último fechamento há {idade_h:.0f} h"
                var = None

            saida[s] = {
                "simbolo": s,
                "tvl_usd": 0.0,
                "volume_24h_usd": volume,
                "preco": fech,
                "variacao_24h_pct": var,
                "horas_cobertas": (ultimo_ts - hoje[0][0]) / 3600,
                "motivo": motivo,
                "fonte": "bolsa",
                "janela": "pregão",
                "preco_fonte": fonte_preco,
                "preco_idade_min": (agora - ultimo_ts) / 60,
            }
        return saida
    finally:
        c.close()


def links_de_grafico(bd: str, saida_grafico: Path | None,
                     simbolos) -> dict[str, str]:
    """`{simbolo: href}` só para quem TEM pagina.

    Duas condicoes, e as duas sao necessarias: o simbolo precisa ter serie em
    `acoes_ohlc` (senao nem daria para gerar) **e** o arquivo precisa existir
    no disco. So a primeira produziria link para pagina ainda nao gerada; so a
    segunda nao saberia o nome. O primeiro simbolo do `defime.jobs.grafico` mora
    na raiz, entao o mapa respeita isso.
    """
    c = sqlite3.connect(bd)
    try:
        com_serie = {s for (s,) in
                     c.execute("select distinct simbolo from acoes_ohlc")}
    finally:
        c.close()
    mapa: dict[str, str] = {}
    for s in simbolos:
        if s not in com_serie:
            continue
        if saida_grafico is None:
            mapa[s] = f"/grafico/{s.lower()}/"
            continue
        if (saida_grafico / s.lower() / "index.html").exists():
            mapa[s] = f"/grafico/{s.lower()}/"
        elif (saida_grafico / "index.html").exists() and _raiz_e(
                saida_grafico, s):
            mapa[s] = "/grafico/"
    return mapa


def _raiz_e(saida_grafico: Path, simbolo: str) -> bool:
    """A pagina da raiz e deste simbolo? O `<title>` responde sem adivinhar
    qual foi o primeiro da lista que gerou aquele diretorio.

    **Le o PRIMEIRO token do titulo, nao o separador.** A primeira versao
    procurava `"<title>MU —"` e dependia do travessao sobreviver ao encoding
    do arquivo: lido com `errors="ignore"` sobre bytes de outra codificacao,
    o travessao some e a funcao devolve False para a pagina certa — o tile
    perde o link sem ninguem notar. O simbolo e o que vem antes do primeiro
    espaco, e isso nao depende de pontuacao nenhuma.
    """
    try:
        cab = (saida_grafico / "index.html").read_text(
            encoding="utf-8", errors="ignore")[:800]
    except OSError:
        return False
    m = re.search(r"<title>\s*([^\s<]+)", cab)
    return bool(m) and m.group(1).upper() == simbolo.upper()


def noticias_do_banco(bd: str, agora: int,
                      simbolos) -> tuple[dict, dict, str | None]:
    """Manchetes de 24 h e o estado de leitura de cada simbolo.

    Le pela MESMA `noticias_banco.ler` que a pagina de grafico usa. Banco sem
    as tabelas (coletor nunca rodou) nao derruba o heatmap: devolve vazio e o
    MOTIVO, que a pagina publica — mapa sem selo com a razao escrita, nunca
    selo "0".
    """
    try:
        c = sqlite3.connect(f"file:{bd}?mode=ro", uri=True, timeout=30)
        try:
            leit = NB.ultima_leitura(c)
            por_sim = {s: NB.ler(c, agora=agora, simbolo=s, horas=24,
                                 limite=50) for s in simbolos}
        finally:
            c.close()
    except sqlite3.Error as exc:
        return {}, {}, f"notícias indisponíveis: {exc}"
    estados = {s: NR.estado(s, por_sim.get(s), leit.get(s)) for s in simbolos}
    return por_sim, estados, None


def montar(bd: str, top: int, css: str = "", agora: int | None = None,
           saida_grafico: Path | None = None,
           top_bolsa: int = 40) -> tuple[str, dict]:
    agora = agora or int(time.time())
    # **A falha aqui nao pode derrubar a pagina.** O heatmap inteiro funciona
    # sem o contrato — so com a cor mais velha —, entao a leitura e tentada e
    # o motivo fica registrado para o rodape.
    contrato, erro_contrato = {}, None
    try:
        contrato = precos_do_contrato(bd)
    except Exception as exc:                                  # noqa: BLE001
        erro_contrato = f"{type(exc).__name__}: {exc}"[:120]
        log.warning("heat_contrato_falhou", erro=erro_contrato)
    por_pool = _serie_por_pool(bd, agora, contrato)
    tokens = H.agregar(por_pool)
    tokens.sort(key=lambda t: -(t.tvl_usd or 0))
    escolhidos, fora = tokens[:top], tokens[top:]

    da_bolsa = H.agregar(serie_da_bolsa(bd, agora))
    da_bolsa.sort(key=lambda t: -(t.volume_24h_usd or 0))

    # O link cobre as DUAS fontes: um simbolo pode estar so na visao da bolsa
    # (UPS, INTC, JNJ, USAR) e ainda assim ter pagina de grafico — alias, e
    # exatamente para eles que a visao existe.
    links = links_de_grafico(
        bd, saida_grafico,
        sorted({t.simbolo for t in escolhidos} | {t.simbolo for t in da_bolsa}))

    # **Cada visao recebe a sua PROPRIA lista de objetos**, porque `H.montar`
    # grava o retangulo dentro do `Token`: reusar a mesma lista faria a
    # segunda chamada sobrescrever o layout da primeira e os dois SVG sairiam
    # identicos, com o rotulo dizendo que sao diferentes.
    #
    # E o layout e calculado SO com os escolhidos: filtrar os tiles depois de
    # montar deixaria os retangulos do conjunto inteiro, com buracos onde
    # estavam os cortados e area que nao fecha o container.
    manter = {t.simbolo for t in escolhidos}
    def _so_escolhidos():
        return [t for t in H.agregar(por_pool) if t.simbolo in manter]
    # As dimensoes do layout sao as do viewBox do render, e nao o default
    # de `H.montar`: calcular o treemap num retangulo e desenha-lo em outro
    # distorceria as razoes de aspecto que o squarified existe para otimizar.
    cx = {"largura": R.VB_L, "altura": R.VB_A}
    por_tvl = H.montar(_so_escolhidos(), por="tvl", **cx)
    por_vol = H.montar(_so_escolhidos(), por="volume", **cx)

    # **A terceira visao e a BOLSA, e ela e outra FONTE.** As tres nunca se
    # misturam num mapa so: o basis entre o preco do token e o da acao tem
    # desvio de 1,42% e chega a 3%, entao colorir os dois lado a lado poria
    # na mesma escala dois numeros que nao sao comparaveis. Aqui entram os
    # tokens que este banco nao indexa na chain (UPS, INTC, JNJ) e os que
    # listaram tarde (USAR).
    # **Aqui nao se corta por tamanho.** Na grade uniforme um tile a mais nao
    # encolhe os outros, entao limitar o numero so esconderia acao — e foram
    # justamente as menores (QUBT, USAR, UPS, RBLX) que o primeiro corte de 18
    # tirou, que sao as do padrão. O parametro fica para a tela
    # ficar utilizavel se a lista crescer muito.
    bolsa = H.montar(da_bolsa[:top_bolsa], por="igual", **cx)

    # Manchetes por ativo (PLAN_NOTICIAS N2): selo no tile e "O que moveu".
    todos = sorted({t.simbolo for t in escolhidos} | {t.simbolo for t in da_bolsa})
    noticias, estados, sem_noticias = noticias_do_banco(bd, agora, todos)

    quando = dt.datetime.fromtimestamp(agora, dt.UTC).strftime(
        "%d/%m %H:%M UTC")
    do_contrato = sum(1 for t in escolhidos if t.preco_fonte == "contrato")
    resumo = {"tokens": len(escolhidos), "fora": [t.simbolo for t in fora],
              "preco_do_contrato": f"{do_contrato}/{len(escolhidos)}",
              "contrato_erro": erro_contrato,
              "pools": len(por_pool), "com_link": len(links),
              "bolsa": len(bolsa["tiles"]),
              "com_noticia": sum(1 for e in estados.values() if e["n"]),
              "noticias_erro": sem_noticias}

    nota_fora = ""
    if fora:
        nota_fora = (
            f'<p class="nota">Mostrando os <b>{len(escolhidos)} maiores por '
            f'TVL</b> de {len(tokens)} tokens com série utilizável. Ficaram '
            f'de fora: {R._e(", ".join(t.simbolo for t in fora))}.</p>')

    html = f"""<!doctype html><html lang="pt-BR" data-theme="dark">
<head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Heatmap dos tokens</title>
<style>{css}{R.CSS}{NR.CSS}{_CSS}</style></head><body>
<div class="w">
<p class="eyebrow"><a href="../">defime</a> · <a href="../pools/">as pools da
chain</a> · <a href="../grafico/">gráfico</a> · <a href="pools/"><b>heatmap das pools</b></a></p>
<div class="hd"><div><h1>Heatmap dos tokens</h1>
<p class="sub">cor = variação · clique abre o gráfico · duas fontes em
abas separadas: a <b>pool</b> na chain (24/7) e a <b>bolsa</b> (Yahoo) ·
número no canto = manchetes da ação em 24 h, ● = evento</p>
</div></div>

{R.bloco(por_tvl, por_vol, links, quando, bolsa, selos=estados)}
{nota_fora}
{NR.o_que_moveu(bolsa["tiles"], noticias, estados, links)}
{f'<p class="nota">{R._e(sem_noticias)}</p>' if sem_noticias else ""}

<div class="sec"><h2>Como ler</h2></div>
<p class="nota"><b>Um tile é um TOKEN, não uma pool.</b> Esta chain tem três
pools de SPY, duas de GOOGL e duas de RBLX — uma célula por pool publicaria o
mesmo ativo três vezes e quem olha leria três ativos. TVL e volume
<b>somam</b> entre as pools do token; <b>preço e variação saem da pool
dominante</b> (a de maior TVL), com o endereço dela no <code>title</code> de
cada tile. Média de preço entre pools de liquidez diferente é um número que
ninguém negocia.</p>

<p class="nota"><b>A cor satura num teto derivado, e ele está na legenda.</b>
É o p90 das magnitudes do dia, com piso de {R._d(H.COR_TETO_MIN, 1)}%. Sem
teto, um token que andou 40% pintaria todo o resto de cinza; sem piso, um dia
calmo sairia saturado sugerindo movimento que não houve. Acima do teto a cor
não continua escurecendo — por isso ele vai escrito.</p>

<p class="nota"><b>Variação ausente é cinza, não verde-fraco.</b> Série que
não cobre {R._d(H.JANELA_MIN_H, 0)} h não vira "variação de 24 h" com outro
nome, e zero afirmaria que o token não andou. O tile fica no lugar, com o
tamanho certo, e o motivo no <code>title</code>.</p>

<p class="nota"><b>As duas primeiras abas são a POOL; a terceira é a
BOLSA — e elas nunca se misturam num mapa só.</b> O basis entre o preço do
token na chain e o da ação tem desvio de <b>1,42%</b> e chega a 3%: colorir
os dois lado a lado poria na mesma escala dois números que não são
comparáveis. Cada aba tem o seu teto de cor e a sua janela, e a legenda muda
junto. O <code>title</code> de todo tile diz de qual fonte ele é.</p>

<p class="nota"><b>Na aba da bolsa os tiles têm o MESMO tamanho</b>, e isso é
medição e não estética: o volume financeiro do pregão varia <b>623×</b> entre
a maior e a menor das ações acompanhadas (MU $28,5 bi contra QUBT $45,7 mi).
Num treemap proporcional a QUBT ficaria com 1/623 da área da MU e sumiria —
uma grade uniforme diz honestamente que ali o tamanho não codifica nada, e o
volume vai no <code>title</code>.</p>

<p class="nota"><b>Cripto fica fora da aba da bolsa, e o motivo é de
unidade.</b> O Yahoo reporta o volume de <code>BTC-USD</code> já em dólares e
o de uma ação em <b>número de ações</b>; multiplicar os dois por preço punha
o BTC em <b>$181 trilhões</b> de "volume financeiro" contra $28 bi da maior
ação, e ele engolia o mapa inteiro. Além disso <code>BTC-USD</code> e
<code>ETH-USD</code> não são ações tokenizadas — estão no banco para converter
gwei em dólar e avaliar o colateral do Morpho.</p>

<p class="nota"><b>UPS, INTC, JNJ e USAR aparecem só na aba da bolsa.</b> Os
três primeiros têm pool na Robinhood Chain e <b>nenhuma está indexada neste
banco</b>; a USAR só listou em 03/2025. Pela bolsa eles existem, e é dali que
sai o número deles — o que o tile declara. Token da chain sem ação na bolsa
(SPCX, SGOV) faz o caminho inverso: aparece nas abas da pool e não é
clicável, porque inventar o link produziria 404.</p>

<p class="nota" style="opacity:.75">{R._e(quando)} · {len(por_pool)} pools
lidas · preço do <b>estado do contrato</b> em {do_contrato} de
{len(escolhidos)} tiles (2 chamadas de RPC, um bloco só); área e volume de
<code>pool_hora</code>{", contrato indisponível: " + R._e(erro_contrato)
 if erro_contrato else ""}</p>
</div></body></html>"""
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
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--top-bolsa", type=int, default=40,
                    help="quantas acoes na visao da bolsa; o default cobre "
                         "todas as acompanhadas, porque na grade uniforme um "
                         "tile a mais nao encolhe os outros")
    ap.add_argument("--bd", default=BD)
    ap.add_argument("--saida", default=str(SAIDA))
    ap.add_argument("--grafico", default=None,
                    help="diretorio das paginas de grafico, para saber quais "
                         "tiles sao clicaveis")
    args = ap.parse_args()

    t0 = time.time()
    css_p = ESTILO
    css = css_p.read_text(encoding="utf-8") if css_p.exists() else ""
    g = Path(args.grafico) if args.grafico else None
    try:
        html, resumo = montar(args.bd, args.top, css, saida_grafico=g,
                              top_bolsa=args.top_bolsa)
    except Exception as exc:                                  # noqa: BLE001
        log.warning("heatmap_falhou", erro=str(exc))
        try:
            gravar_batimento("heatmap", ok=False, erro=str(exc)[:200])
        except Exception:                                     # noqa: BLE001
            pass
        print(f"falhou: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    destino = Path(args.saida)
    destino.mkdir(parents=True, exist_ok=True)
    (destino / "index.html").write_text(html, encoding="utf-8")
    print(destino / "index.html")
    log.info("heatmap", **resumo, segundos=round(time.time() - t0, 1))
    try:
        gravar_batimento("heatmap", ok=True)
    except Exception:                                         # noqa: BLE001
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
