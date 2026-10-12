"""Todas as pools RWA/USDG, de hora em hora, e quais ganharam relevancia.

Duas perguntas, uma pagina:

  1. **como esta cada pool agora** — TVL, book, volume, fee, APR, taxa, idade,
     no formato de tela de mercado, com filtro no cliente;
  2. **o que MUDOU na ultima hora** — que e a pergunta que uma foto nao
     responde e a razao de este job existir.

**Relevancia se mede contra a propria pool, nao contra as outras.** Uma pool
que gira $3M por dia fazendo $150k numa hora e rotina; uma que gira $50k
fazendo $150k e um evento. O sinal e `volume_1h / mediana das ultimas leituras
da MESMA pool`, e ele so sai com **>= 6 leituras** de base — antes disso a
pagina diz "sem base" em vez de chamar a primeira medicao de recorde.

---

**Duas colunas de APR, e a segunda e a que decide.** `APR sobre TVL` e o numero
que os agregadores publicam e serve para comparar com eles. Mas a fee que voce
recebe e `L_sua / (L_ativa + L_sua)`, e `L_ativa` e so o que esta NO TICK
CORRENTE: uma pool de $5M com a liquidez longe do preco dilui menos que uma de
$400k concentrada nele. `APR sobre book +-1%` e o que a sua posicao encontra.

**O TVL de V3 e exato e o de V4 e reconstruido**, e a pagina diz qual e qual.
Na V3 o proprio contrato guarda os dois tokens e `balanceOf` responde. Na V4 o
`PoolManager` e singleton com flash accounting: `balanceOf` nele devolve a soma
da chain inteira, um numero grande e sem significado.

**O TVL de V4 e INCREMENTAL**, e sem isso o job nao existiria: a reconstrucao
completa varre 40M de blocos em ~40 chamadas por pool, o que daria centenas de
chamadas por ciclo. Aqui as posicoes acumuladas ficam em `pool_v4_estado` com o
bloco ate onde foram somadas, e cada hora varre so o trecho novo.

**Preco, liquidez e TVL saem do MESMO bloco.** E a sexta armadilha desta
familia neste projeto — a anterior produziu um book de $222.823 dentro de um
TVL de $148.330 — e aqui a guarda e estrutural: as fotos devolvem os numeros
juntos ou nenhum.

**O volume vem da CHAIN, nao do banco.** O indexador pode cobrir so parte das
pools (ou so algumas janelas), e ler dali produziria zero em toda pool fora da
cobertura. Zero por falta de coleta e indistinguivel de zero por falta de
negocio.

Pre-requisitos: `pool_events` (o indexador, `defime.ingest`) para descobrir as
pools e derivar orientacao e spacing, e `python -m defime.jobs.simbolos --v4`
para os nomes e a taxa declarada das V4.

    python -m defime.jobs.pools_hora                 # todas as pools
    python -m defime.jobs.pools_hora --relevantes    # ciclo curto: as do topo
"""

import datetime as dt
import html
import json
import os
import sqlite3
import statistics as st
import sys
import time
from math import gcd
from pathlib import Path

RAIZ = Path.cwd()
ESTILO = Path(__file__).resolve().parents[1] / "web" / "estilo.css"
os.environ.setdefault("LOG_JSON", "true")

from defime.chain.abi import TOPIC0 as TOPIC_V3               # noqa: E402
from defime.chain.abi import decode_log as decode_v3          # noqa: E402
from defime.chain.abi_v4 import POOL_MANAGER, TOPIC0          # noqa: E402
from defime.chain.abi_v4 import decode_log as decode_v4       # noqa: E402
from defime.chain.rpc import RpcClient                        # noqa: E402
from defime.core.v3math import (                              # noqa: E402
    amounts_for_liquidity, tick_to_sqrt_price_x96)
from defime.core.taxa_lp import (                              # noqa: E402
    fee_protocol_do_slot0, fracao_lp_v3)
from defime.db.registro import gravar_batimento               # noqa: E402

BD = str(RAIZ / "defime.db")
SAIDA = Path(os.environ.get("POOLS_SAIDA", RAIZ / "site" / "pools" / "index.html"))
NPM = "0x73991a25c818bf1f1128deaab1492d45638de0d3"

# 1h a ~0,1s por bloco. Nao sao "36.000 exatos": o job le o bloco de agora e
# recua esta quantidade, entao um no lento encurta a janela em vez de mentir
# sobre ela — a janela real vai gravada em `bloco_de`/`bloco_ate`.
BLOCOS_POR_HORA = 36_000
BASE_MINIMA = 6            # leituras necessarias para declarar relevancia
JANELA_BASE = 24           # quantas leituras entram na mediana
RELEVANTE = 1.8            # razao a partir da qual a pool entra no destaque

SEL_SLOT0, SEL_LIQ = "0x3850c7bd", "0x1a686502"
SEL_T0, SEL_T1, SEL_BAL = "0x0dfe1681", "0xd21220a7", "0x70a08231"


# `{pool: "MU/USDG 0,30%"}`, preenchido por `coletar` a partir do cache de
# simbolos (`defime.jobs.simbolos`). Pool sem nome sai pelo endereco curto.
NOMES: dict[str, str] = {}


def _nomes_do_banco(c: sqlite3.Connection) -> dict[str, str]:
    from defime.jobs import simbolos as S
    antigo = c.row_factory
    c.row_factory = sqlite3.Row
    try:
        return S.nomes(c)
    except sqlite3.OperationalError:            # banco sem `token_simbolo` ainda
        return {}
    finally:
        c.row_factory = antigo


def nome(p: str) -> str:
    return NOMES.get(p) or NOMES.get(p.lower()) or (
        ("V4 " if len(p) == 66 else "V3 ") + p[:10] + "…")


def i256(h: str) -> int:
    v = int(h, 16)
    return v - (1 << 256) if v >= (1 << 255) else v


def preco_do(tick: int, usdg0: bool) -> float:
    return (1.0001 ** (-tick if usdg0 else tick)) * 1e12


# Quantas pools o ciclo curto varre. **Nao e limiar de dolar, e ranking**: um
# piso absoluto ("volume > $50k/h") envelhece — a chain cresce, o piso nao, e
# um dia ele deixa passar tudo. Um numero fixo de pools se ajusta sozinho.
#
# Medido em 2026-09-01, volume medio por hora das 33 pools: as 12 primeiras vao
# de $2,2M a $46k; a cauda cai para $3.141, $1.685, $75 e **$0**. Varrer as 33
# levava ~20 min e o ciclo ocupava um terco do relogio.
TOPO_RELEVANTE = 12

def _relevantes(c: sqlite3.Connection, topo: int = TOPO_RELEVANTE) -> set[str]:
    """As `topo` maiores por volume medio.

    Sem historico ainda (primeira execucao), devolve vazio — e o chamador
    varre todas. Cortar por falta de dado seria decidir no escuro.
    """
    linhas = c.execute(
        "select pool, avg(volume_usd) v from pool_hora "
        "where volume_usd is not null group by pool "
        "order by v desc limit ?", (topo,)).fetchall()
    return {p for p, _ in linhas}


def _esquema(c: sqlite3.Connection) -> None:
    c.executescript("""
    create table if not exists pool_hora(
        pool text not null, ts integer not null,
        bloco_de integer, bloco_ate integer,
        tick integer, preco real, liquidez_ativa text,
        tvl_usd real, tvl_fonte text, book_usd real,
        ratio_stable real,
        swaps integer, volume_usd real, fee_usd real,
        primary key (pool, ts));
    create index if not exists ix_pool_hora on pool_hora(pool, ts desc);

    create table if not exists pool_v4_estado(
        pool text primary key, ate_bloco integer not null,
        posicoes text not null, atualizado integer not null);

    create table if not exists pool_param(
        pool text primary key, usdg0 integer, token0 text, token1 text,
        spacing integer, taxa real, versao text, primeiro_ts integer);

    -- O `feeProtocol` do slot0 de cada V3, lido a cada rodada (o slot0 ja e
    -- lido para o tick). Tabela propria e nao coluna do `pool_param`, que e
    -- gravado por posicao: coluna nova ali embaralharia o banco antigo.
    create table if not exists pool_v3_protocolo(
        pool text primary key, fee_protocol integer not null,
        lido_em integer not null);
    """)
    c.commit()


# -------------------------------------------------------------- parametros
def _parametros(c: sqlite3.Connection, pool: str, rpc) -> dict | None:
    """Le do cache; se nao houver, deriva dos eventos e do contrato e grava.

    Nenhum destes e suposto, e cada suposicao ja custou caro neste projeto:
    fixar a taxa em 0,30% fez a GOOGL render +$1.691 numa pool que ganhou $457;
    fixar o spacing em 60 pos a RDDT em primeiro lugar com uma faixa que nao
    existe naquela pool; supor a stable em token0 fez GME e SLV valerem ZERO.
    """
    linha = c.execute(
        "select usdg0, token0, token1, spacing, taxa, versao, primeiro_ts "
        "from pool_param where pool=?", (pool,)).fetchone()
    if linha and linha[4] is not None:
        return {"usdg0": bool(linha[0]), "token0": linha[1], "token1": linha[2],
                "spacing": linha[3], "taxa": linha[4], "versao": linha[5],
                "primeiro_ts": linha[6]}

    v4 = len(pool) == 66
    # Orientacao pelo TAMANHO dos amounts: o lado de 18 casas sai ~1e12 vezes
    # maior que o de 6 para o mesmo valor em dolar.
    usdg0 = None
    for (a,) in c.execute("select args from pool_events where pool_address=? "
                          "and event='Swap' limit 400", (pool,)):
        j = json.loads(a)
        a0, a1 = abs(int(j["amount0"])), abs(int(j["amount1"]))
        if a0 and a1:
            usdg0 = a0 < a1
            break
    if usdg0 is None:
        return None

    ticks: list[int] = []
    taxas: list[float] = []
    for (a,) in c.execute(
            "select args from pool_events where pool_address=? and event in "
            "('Mint','Burn','ModifyLiquidity','Swap') limit 4000", (pool,)):
        j = json.loads(a)
        if "tickLower" in j:
            ticks += [abs(int(j["tickLower"])), abs(int(j["tickUpper"]))]
        if j.get("fee") is not None:
            taxas.append(float(j["fee"]))
    g = 0
    for t in ticks:
        g = gcd(g, t)
    spacing = next((x for x in (200, 100, 60, 20, 10, 1)
                    if g and g % x == 0), None)

    taxa = st.median(taxas) / 1e6 if taxas else None
    t0 = t1 = None
    if not v4:
        try:
            t0 = "0x" + rpc.eth_call(pool, SEL_T0)[-40:]
            t1 = "0x" + rpc.eth_call(pool, SEL_T1)[-40:]
            if taxa is None:
                taxa = int(rpc.eth_call(pool, "0xddca3f43"), 16) / 1e6
        except Exception:                                     # noqa: BLE001
            pass
    (primeiro,) = c.execute("select min(ts) from pool_events where "
                            "pool_address=?", (pool,)).fetchone()
    c.execute("insert or replace into pool_param values (?,?,?,?,?,?,?,?)",
              (pool, int(usdg0), t0, t1, spacing, taxa,
               "v4" if v4 else "v3", primeiro))
    c.commit()
    return {"usdg0": usdg0, "token0": t0, "token1": t1, "spacing": spacing,
            "taxa": taxa, "versao": "v4" if v4 else "v3",
            "primeiro_ts": primeiro}


# ------------------------------------------------------------------- fotos
def _book(sq: int, tk: int, L: int, usdg0: bool) -> float | None:
    """Capital dentro de +-1% do preco. E ele, e nao o TVL, que dilui a fee."""
    if L <= 0:
        return None
    a = amounts_for_liquidity(sq, tick_to_sqrt_price_x96(tk - 100),
                              tick_to_sqrt_price_x96(tk + 100), L)
    p = preco_do(tk, usdg0)
    return (a[0] / 1e6 + a[1] / 1e18 * p if usdg0
            else a[0] / 1e18 * p + a[1] / 1e6)


def _logs(rpc, corpo: dict, de: int, ate: int, minimo: int = 30_000) -> list:
    """`eth_getLogs` que ENCOLHE a janela em vez de aceitar a falha.

    Medido em 2026-08-31: pedir 1M de blocos ao no da uma faixa e falha em
    outra — `log query timed out` depende de quantos logs ha dentro, nao do
    tamanho do intervalo. A primeira versao engolia a excecao e devolvia lista
    vazia, e treze pools V4 sairam como **"nenhum Swap nos ultimos 5M
    blocos"** quando o que tinham era uma consulta que nao voltou.

    Falha de query lida como ausencia de dado e a familia de erro que este
    projeto mais pagou. Aqui a janela se divide pela metade ate `minimo`; se
    falhar no minimo, a excecao **sobe** — vira erro declarado na pagina, e
    nao zero.
    """
    try:
        return rpc.call("eth_getLogs", [{**corpo, "fromBlock": hex(de),
                                         "toBlock": hex(ate)}])
    except Exception:                                         # noqa: BLE001
        if ate - de <= minimo:
            raise
        meio = (de + ate) // 2
        return (_logs(rpc, corpo, de, meio, minimo)
                + _logs(rpc, corpo, meio + 1, ate, minimo))


def _foto_v3(pool: str, par: dict, rpc) -> dict:
    """slot0, liquidez e TVL do MESMO instante. Uma funcao, quatro numeros."""
    s0 = rpc.eth_call(pool, SEL_SLOT0)
    sq, tk = int(s0[2:66], 16), i256("0x" + s0[66:130])
    # O feeProtocol vem na MESMA leitura e ate 24/09 era jogado fora, enquanto
    # a fee usava 5/6 suposto — errado nas V3 de 0,05% (0x44, LP fica com 3/4).
    fee_protocol = fee_protocol_do_slot0(s0)
    L = int(rpc.eth_call(pool, SEL_LIQ), 16)
    p = preco_do(tk, par["usdg0"])
    tvl = None
    v_stable = v_ativo = None
    if par["token0"] and par["token1"]:
        arg = SEL_BAL + pool[2:].rjust(64, "0")
        b0 = int(rpc.eth_call(par["token0"], arg), 16)
        b1 = int(rpc.eth_call(par["token1"], arg), 16)
        # **A COMPOSICAO das reservas, que ate 03/09 era lida e jogada
        # fora.** `tvl_usd` e um numero so; o `Ratio` dos agregadores (76/24,
        # 100/0) e a divisao entre os dois lados, e ela nao se reconstroi de
        # um total. Um `0/100` significa que o preco atravessou toda a
        # liquidez concentrada — o que a coluna de TVL nao consegue dizer.
        v_stable = b0 / 1e6 if par["usdg0"] else b1 / 1e6
        v_ativo = (b1 / 1e18 * p if par["usdg0"] else b0 / 1e18 * p)
        tvl = v_stable + v_ativo
    book = _book(sq, tk, L, par["usdg0"])
    # O book e PARTE do TVL: nao pode passar dele. Se passar, alguma leitura
    # esta errada e omitir e melhor que publicar — foi assim que o bug de
    # instante apareceu na AMZN.
    if tvl and book and book > tvl * 1.01:
        book = None
    return {"sq": sq, "tick": tk, "preco": p, "L": L, "tvl": tvl,
            "tvl_fonte": "balanceOf" if tvl is not None else None,
            # Fracao em STABLE. `None` quando nao houve leitura de saldo —
            # zero aqui afirmaria uma pool 0/100, que e um estado real e
            # extremo, e nao "nao medi".
            "ratio_stable": (100 * v_stable / tvl
                             if tvl and tvl > 0 else None),
            "book": book, "corte": None, "fee_protocol": fee_protocol}


def _foto_v4(pool: str, par: dict, rpc, topo: int, piso: int = 0) -> dict | None:
    """O ultimo Swap da a foto; o TVL vem do acumulado incremental.

    A busca vai do topo para tras em passos de 1M ate `piso`, que e o bloco do
    ultimo Swap que o BANCO conhece: procurar abaixo dele seria gastar chamada
    onde ja sabemos que nao ha nada mais novo. Sem esse piso a SPY 0,30% v4
    saia como "nenhum Swap nos ultimos 5M blocos" — ela simplesmente nao
    negocia ha mais que isso, e o limite era meu, nao dela.
    """
    sq = tk = L = corte = None
    lo = topo
    while lo > max(piso - 1, topo - 40_000_000) and corte is None:
        de = max(0, lo - 999_999)
        sw = _logs(rpc, {"address": POOL_MANAGER,
                         "topics": [TOPIC0["Swap"], pool]}, de, lo)
        if sw:
            d = decode_v4(sw[-1])
            if d:
                corte = int(sw[-1]["blockNumber"], 16)
                sq, tk = int(d[1]["sqrtPriceX96"]), int(d[1]["tick"])
                L = int(d[1]["liquidity"])
        lo = de - 1
    if corte is None:
        return None
    return {"sq": sq, "tick": tk, "preco": preco_do(tk, par["usdg0"]),
            "L": L, "corte": corte, "tvl": None, "tvl_fonte": None,
            "book": _book(sq, tk, L, par["usdg0"])}


def _tvl_v4(c: sqlite3.Connection, pool: str, par: dict, foto: dict, rpc,
            desde: int) -> float | None:
    """Soma das posicoes ate `foto['corte']`, varrendo so o trecho novo.

    **Lacuna vira `None`, nunca TVL a menos.** Se um `eth_getLogs` falhar no
    meio, devolver a soma parcial publicaria um numero menor que o real sem
    nenhum sinal; o estado tambem nao avanca, para a proxima tentativa refazer
    o trecho.
    """
    linha = c.execute("select ate_bloco, posicoes from pool_v4_estado "
                      "where pool=?", (pool,)).fetchone()
    if linha:
        de = linha[0] + 1
        pos = {tuple(json.loads(k)): v
               for k, v in json.loads(linha[1]).items()}
    else:
        de, pos = desde, {}

    corte = foto["corte"]
    while de <= corte:
        ate = min(corte, de + 999_999)
        try:
            logs = _logs(rpc, {"address": POOL_MANAGER,
                               "topics": [TOPIC0["ModifyLiquidity"], pool]},
                         de, ate)
        except Exception:                                     # noqa: BLE001
            return None
        for entry in logs:
            d = decode_v4(entry)
            if not d:
                continue
            a = d[1]
            k = (int(a["tickLower"]), int(a["tickUpper"]))
            pos[k] = pos.get(k, 0) + int(a["liquidityDelta"])
        de = ate + 1

    c.execute("insert or replace into pool_v4_estado values (?,?,?,?)",
              (pool, corte, json.dumps({json.dumps(list(k)): v
                                        for k, v in pos.items()}),
               int(time.time())))
    c.commit()

    p, tvl = foto["preco"], 0.0
    for (tl, tu), L in pos.items():
        if L <= 0:
            continue
        a0, a1 = amounts_for_liquidity(foto["sq"], tick_to_sqrt_price_x96(tl),
                                       tick_to_sqrt_price_x96(tu), L)
        tvl += (a0 / 1e6 + a1 / 1e18 * p if par["usdg0"]
                else a0 / 1e18 * p + a1 / 1e6)
    return tvl


# Bit de taxa dinamica no `fee` da PoolKey da V4: o hook decide o lpFee a cada
# swap, entao nao ha lpFee declarado para separar a parte do protocolo.
V4_TAXA_DINAMICA = 0x800000


def taxa_lp_v4(fee_evento_pips: int, lp_fee_pips: int | None) -> float | None:
    """Fracao do volume que chega ao LP num `Swap` da V4, ou None se nao ha como saber.

    O campo `fee` do evento e o TOTAL: o protocolo cobra `pf` sobre a entrada e o
    LP cobra `lp` sobre o que sobra, entao `total = pf + lp x (1 - pf/1e6)`.
    Com o `lp` declarado na PoolKey (`pool_v4_chave`), `pf` sai do proprio
    evento e o LP fica com `total - pf`. Sem conta nenhuma no RPC.

    Antes era `taxa x 5/6` para toda pool: o 5/6 e o `feeProtocol = 102` da V3.
    Medido em 24/09 contra a taxa do LP lida do contrato: -2,8% nas de 0,30%,
    -3,3% na de 0,25%, **-8,3% nas de 1%** e +4,2% na SPY de 0,05%.

    Taxa dinamica ou chave ausente devolvem None: a fee da hora sai nula, que a
    pagina publica como "—", em vez de um numero com a fatia do protocolo
    suposta.
    """
    if lp_fee_pips is None or lp_fee_pips & V4_TAXA_DINAMICA:
        return None
    pf = (fee_evento_pips - lp_fee_pips) / (1 - lp_fee_pips / 1e6)
    if pf < -0.5:                           # total menor que o lpFee: chave errada
        return None
    return (fee_evento_pips - max(pf, 0.0)) / 1e6


def _volume(pool: str, par: dict, rpc, de: int, ate: int) -> dict:
    """Swaps, volume e fee da janela. O volume sai pelo lado que e DOLAR."""
    v4 = par["versao"] == "v4"
    try:
        logs = _logs(rpc, {
            "address": POOL_MANAGER if v4 else pool,
            "topics": ([TOPIC0["Swap"], pool] if v4 else [TOPIC_V3["Swap"]])},
            de, ate)
    except Exception as exc:                                  # noqa: BLE001
        return {"erro": type(exc).__name__}
    vol = fee = 0.0
    n = 0
    for entry in logs:
        d = decode_v4(entry) if v4 else decode_v3(entry)
        if not d or "amount0" not in d[1]:
            continue
        a = d[1]
        # Na V3 o `amount0` e do ponto de vista da POOL: positivo = token0
        # ENTROU, ou seja zeroForOne. E o sentido que escolhe o nibble do
        # feeProtocol.
        entrou0 = int(a["amount0"]) > 0
        a0, a1 = abs(int(a["amount0"])), abs(int(a["amount1"]))
        usd = (a0 / 1e6) if par["usdg0"] else (a1 / 1e6)
        vol += usd
        n += 1
        if v4:
            # V4: o `fee` do evento e o TOTAL; a parte do LP sai de `taxa_lp_v4`.
            lp = (taxa_lp_v4(int(a["fee"]), par.get("lp_fee_pips"))
                  if a.get("fee") is not None else None)
            if lp is None:
                fee = None
            elif fee is not None:
                fee += usd * lp
        elif fee is not None:
            # V3: a fracao do LP sai do `feeProtocol` LIDO do slot0 nesta
            # rodada, no sentido deste swap. Era 5/6 para toda V3; nas de 0,05%
            # o contrato diz 0x44 e o LP fica com 3/4 (fee 11,1% alta).
            # Sem o byte a fee da hora sai nula, nunca o 5/6 de volta.
            fp = par.get("fee_protocol")
            if fp is None:
                fee = None
            else:
                fee += usd * (par["taxa"] or 0.0) * fracao_lp_v3(fp, entrou0)
    return {"swaps": n, "volume": vol, "fee": fee}


def _relevancia(c: sqlite3.Connection, pool: str, volume: float) -> dict:
    """Volume da hora contra a mediana das ultimas leituras da MESMA pool."""
    base = [v for (v,) in c.execute(
        "select volume_usd from pool_hora where pool=? and volume_usd is not "
        "null order by ts desc limit ?", (pool, JANELA_BASE))]
    if len(base) < BASE_MINIMA:
        return {"razao": None, "n": len(base),
                "motivo": f"{len(base)}/{BASE_MINIMA} leituras de base"}
    med = st.median(base)
    if med <= 0:
        return {"razao": None, "n": len(base), "motivo": "mediana zero"}
    return {"razao": volume / med, "n": len(base), "mediana": med,
            "motivo": None}


# ------------------------------------------------------------------ coleta
def coletar(limite: int | None = None, so_relevantes: bool = False) -> dict:
    c = sqlite3.connect(BD)
    _esquema(c)
    NOMES.update(_nomes_do_banco(c))
    rpc = RpcClient()
    t0 = time.time()
    topo = int(rpc.call("eth_blockNumber"), 16)
    de = max(0, topo - BLOCOS_POR_HORA)
    agora = int(time.time())

    pools = [p for (p,) in c.execute(
        "select pool_address from pool_events group by pool_address "
        "order by count(*) desc") if p != NPM]
    if so_relevantes:
        alvo = _relevantes(c)
        if alvo:
            pools = [p for p in pools if p in alvo]
    if limite:
        pools = pools[:limite]

    linhas, erros = [], []
    for pool in pools:
        try:
            par = _parametros(c, pool, rpc)
            if not par:
                erros.append((pool, "sem swap para derivar a orientação"))
                continue
            if par["versao"] == "v3":
                foto = _foto_v3(pool, par, rpc)
                par = {**par, "fee_protocol": foto["fee_protocol"]}
                c.execute("insert or replace into pool_v3_protocolo values (?,?,?)",
                          (pool, foto["fee_protocol"], agora))
            else:
                (ultimo,) = c.execute(
                    "select max(block_number) from pool_events where "
                    "pool_address=? and event='Swap'", (pool,)).fetchone()
                foto = _foto_v4(pool, par, rpc, topo, piso=ultimo or 0)
                if foto is None:
                    erros.append((pool, "nenhum Swap entre o último do banco e o topo"))
                    continue
                foto["tvl"] = _tvl_v4(c, pool, par, foto, rpc,
                                      desde=max(0, topo - 40_000_000))
                foto["tvl_fonte"] = ("posições" if foto["tvl"] is not None
                                     else None)
            if par["versao"] == "v4" and "lp_fee_pips" not in par:
                try:
                    linha = c.execute("select fee_pips from pool_v4_chave where "
                                      "lower(pool) = lower(?)", (pool,)).fetchone()
                except sqlite3.OperationalError:    # banco sem `jobs/simbolos --v4`
                    linha = None
                par = {**par, "lp_fee_pips": linha[0] if linha else None}
            vol = _volume(pool, par, rpc, de, topo)
            if "erro" in vol:
                erros.append((pool, f"volume: {vol['erro']}"))
                vol = {"swaps": None, "volume": None, "fee": None}
            rel = _relevancia(c, pool, vol["volume"] or 0.0)
            # **Colunas NOMEADAS, e nao posicionais.** `alter table` no
            # SQLite acrescenta a coluna no FIM, enquanto o `create table`
            # deste arquivo a declara no meio — um `values (?,?,...)` grava
            # certo no banco novo e embaralha o antigo, sem erro nenhum. O
            # `ratio_stable` cairia em `swaps`, `swaps` em `volume_usd`, e o
            # painel publicaria os numeros trocados.
            c.execute(
                "insert or replace into pool_hora (pool, ts, bloco_de, "
                "bloco_ate, tick, preco, liquidez_ativa, tvl_usd, "
                "tvl_fonte, book_usd, ratio_stable, swaps, volume_usd, "
                "fee_usd) values (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (pool, agora, de, topo, foto["tick"], foto["preco"],
                 str(foto["L"]), foto["tvl"], foto["tvl_fonte"],
                 foto["book"], foto.get("ratio_stable"),
                 vol["swaps"], vol["volume"], vol["fee"]))
            linhas.append({"pool": pool, "nome": nome(pool), **par, **foto,
                           **vol, "rel": rel})
        except Exception as exc:                              # noqa: BLE001
            erros.append((pool, f"{type(exc).__name__}: {exc}"[:90]))
    c.commit()
    c.close()
    return {"ts": agora, "bloco_de": de, "bloco_ate": topo, "linhas": linhas,
            "erros": erros, "segundos": time.time() - t0}


# -------------------------------------------------------------------- HTML
def _d(v: float, casas: int = 2) -> str:
    return f"{v:,.{casas}f}".replace(",", "@").replace(".", ",").replace("@", ".")


def _usd(v, casas: int = 0) -> str:
    if v is None:
        return "—"
    if abs(v) >= 1_000_000:
        return f"${_d(v / 1e6, 2)}M"
    if abs(v) >= 1_000:
        return f"${_d(v / 1e3, 1)}k"
    return f"${_d(v, casas)}"


def _apr(fee_1h, base) -> float | None:
    """Anualiza a fee de UMA hora. Volatil por construcao, e a pagina diz."""
    if fee_1h is None or not base or base <= 0:
        return None
    return 100 * fee_1h * 8760 / base


# Filtros e cartoes de destaque: o estilo base nao tem essas classes.
_CSS = """
.faixa.f3{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:10px;
  background:none;border:0;padding:0}
.faixa.f3 .m{border:1px solid var(--line,#2a3340);border-radius:10px;padding:12px 14px}
.filtros{display:flex;flex-wrap:wrap;gap:10px 18px;align-items:center;margin:10px 0 14px}
.fg{display:flex;align-items:center;gap:4px;flex-wrap:wrap}
.fl{font:500 11px/1 'IBM Plex Mono',monospace;text-transform:uppercase;letter-spacing:.06em;
  opacity:.65;margin-right:4px}
.fb{font:500 12px/1 'IBM Plex Mono',monospace;color:inherit;background:transparent;
  border:1px solid var(--line,#2a3340);border-radius:999px;padding:6px 10px;cursor:pointer}
.fb:hover{border-color:currentColor}
.fb.on{background:rgba(110,168,254,.16);border-color:rgba(110,168,254,.7)}
.conta{font:500 12px/1 'IBM Plex Mono',monospace;opacity:.7;margin-left:auto}
"""


def _html(x: dict, css: str) -> str:
    t = dt.datetime.fromtimestamp(x["ts"], dt.UTC)
    linhas = sorted(x["linhas"], key=lambda r: -(r.get("volume") or 0))

    destaques = sorted(
        [r for r in linhas
         if (r["rel"]["razao"] or 0) >= RELEVANTE and (r.get("volume") or 0) > 0],
        key=lambda r: -r["rel"]["razao"])
    sem_base = sum(1 for r in linhas if r["rel"]["razao"] is None)

    corpo = []
    for r in linhas:
        cel_nome = html.escape(r["nome"])
        apr_tvl = _apr(r.get("fee"), r.get("tvl"))
        apr_book = _apr(r.get("fee"), r.get("book"))
        razao = r["rel"]["razao"]
        idade_d = ((x["ts"] - r["primeiro_ts"]) / 86400
                   if r.get("primeiro_ts") else None)
        cel_rel = (f'<span class="{"up" if razao >= RELEVANTE else ""}">'
                   f'{_d(razao, 2)}×</span>' if razao is not None
                   else f'<span class="fraco" title="{r["rel"]["motivo"]}">—</span>')
        corpo.append(
            f'<tr data-tvl="{r.get("tvl") or 0:.0f}" '
            f'data-apr="{apr_tvl if apr_tvl is not None else -1:.2f}" '
            f'data-vol="{r.get("volume") or 0:.0f}" '
            f'data-fee="{(r.get("taxa") or 0) * 100:.2f}" '
            f'data-versao="{r["versao"]}" '
            f'data-idade="{idade_d if idade_d is not None else 9999:.0f}">'
            f'<td class="par">{cel_nome}'
            f'<div class="s mono">{r["pool"][:12]}…</div></td>'
            f'<td><span class="pill {r["versao"]}">{r["versao"].upper()}</span></td>'
            f'<td>{_d((r.get("taxa") or 0) * 100, 2)}%</td>'
            f'<td>${_d(r["preco"], 2)}</td>'
            f'<td>{_usd(r.get("tvl"))}'
            + (f'<div class="s">{r.get("tvl_fonte") or ""}</div>'
               if r.get("tvl") else '<div class="s">V4 singleton</div>'
               if r["versao"] == "v4" else "")
            + f'</td>'
            f'<td>{_usd(r.get("book"))}</td>'
            f'<td>{_usd(r.get("volume"))}</td>'
            f'<td>{r.get("swaps") if r.get("swaps") is not None else "—"}</td>'
            f'<td>{_usd(r.get("fee"), 2)}</td>'
            f'<td>{_d(apr_tvl, 1) + "%" if apr_tvl is not None else "—"}</td>'
            f'<td class="forte">'
            f'{_d(apr_book, 1) + "%" if apr_book is not None else "—"}</td>'
            f'<td>{cel_rel}</td>'
            f'<td>{_d(idade_d, 0) + " d" if idade_d is not None else "—"}</td>'
            f"</tr>")

    if destaques:
        cartoes = "".join(
            f'<div class="m"><div class="k">{html.escape(r["nome"])}</div>'
            f'<div class="v up">{_d(r["rel"]["razao"], 2)}×</div>'
            f'<div class="s">{_usd(r.get("volume"))} nesta hora · '
            f'mediana {_usd(r["rel"].get("mediana"))} · '
            f'{r["rel"]["n"]} leituras</div></div>' for r in destaques[:6])
        bloco_destaque = f'<div class="faixa f3">{cartoes}</div>'
    else:
        bloco_destaque = (
            f'<p class="nota">Nenhuma pool passou de {RELEVANTE:.1f}× a própria '
            f'mediana nesta hora. <b>{sem_base} das {len(linhas)}</b> ainda não '
            f'têm as {BASE_MINIMA} leituras de base — a relevância só aparece '
            f'depois delas, porque chamar a primeira medição de recorde seria '
            f'comparar uma pool com nada.</p>')

    erros = "".join(
        f'<p class="nota"><b class="down">{html.escape(nome(p))}</b> — '
        f'{html.escape(m)}</p>' for p, m in x["erros"])

    return f"""<!doctype html>
<html lang="pt-BR" data-theme="dark"><head>
<meta name="color-scheme" content="dark">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>Pools · hora a hora</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500;600;700&display=swap">
<style>{css}{_CSS}</style></head><body>
<div class="w largo">

<header class="hd">
  <div>
    <p class="eyebrow"><a href="../">defime</a> · <a href="../heat/">heatmap dos tokens</a> · <a href="../heat/pools/">heatmap das pools</a></p>
    <h1>Pools · hora a hora</h1>
  </div>
  <div class="quando">
    {t:%d/%m/%Y %H:%M} UTC<br>
    janela de {x["bloco_ate"] - x["bloco_de"]:,} blocos · {len(linhas)} pools<br>
    {x["segundos"]:.0f}s de coleta
  </div>
</header>

<div class="sec"><h2>Ganharam relevância nesta hora</h2><span class="cnt">volume contra a mediana da própria pool</span></div>
{bloco_destaque}
<p class="nota"><b>Relevância se mede contra a própria pool, não contra as outras.</b>
$150k numa hora é rotina numa pool que gira $3M por dia e é um evento numa que gira $50k.
Comparar pools entre si diria apenas quem é grande.</p>

<div class="sec"><h2>Todas as pools</h2><span class="cnt">clique nos filtros · a tabela filtra sozinha</span></div>

<div class="filtros">
  <div class="fg"><span class="fl">TVL</span>
    <button class="fb on" data-f="tvl" data-v="0">tudo</button>
    <button class="fb" data-f="tvl" data-v="100000">&gt;$100k</button>
    <button class="fb" data-f="tvl" data-v="1000000">&gt;$1M</button></div>
  <div class="fg"><span class="fl">APR</span>
    <button class="fb on" data-f="apr" data-v="-1">tudo</button>
    <button class="fb" data-f="apr" data-v="5">&gt;5%</button>
    <button class="fb" data-f="apr" data-v="20">&gt;20%</button>
    <button class="fb" data-f="apr" data-v="50">&gt;50%</button>
    <button class="fb" data-f="apr" data-v="100">&gt;100%</button></div>
  <div class="fg"><span class="fl">Volume 1h</span>
    <button class="fb on" data-f="vol" data-v="0">tudo</button>
    <button class="fb" data-f="vol" data-v="10000">&gt;$10k</button>
    <button class="fb" data-f="vol" data-v="100000">&gt;$100k</button>
    <button class="fb" data-f="vol" data-v="1000000">&gt;$1M</button></div>
  <div class="fg"><span class="fl">Versão</span>
    <button class="fb on" data-f="versao" data-v="">tudo</button>
    <button class="fb" data-f="versao" data-v="v3">V3</button>
    <button class="fb" data-f="versao" data-v="v4">V4</button></div>
  <div class="fg"><span class="fl">Taxa</span>
    <button class="fb on" data-f="fee" data-v="">tudo</button>
    <button class="fb" data-f="fee" data-v="0.05">0,05%</button>
    <button class="fb" data-f="fee" data-v="0.30">0,30%</button>
    <button class="fb" data-f="fee" data-v="1.00">1,00%</button></div>
  <div class="fg"><span class="fl">Idade</span>
    <button class="fb on" data-f="idade" data-v="99999">tudo</button>
    <button class="fb" data-f="idade" data-v="7">&lt;7d</button>
    <button class="fb" data-f="idade" data-v="30">&lt;30d</button></div>
  <span class="conta" id="conta"></span>
</div>

<div class="tw"><table id="tab">
<thead><tr>
<th>Pool</th><th>Ver</th><th>Taxa</th><th>Preço</th>
<th title="total na pool. Na V3 sai de balanceOf e é exato; na V4 é reconstruído das posições">TVL</th>
<th title="capital dentro de ±1% do preço — é ele, e não o TVL, que dilui a sua fee">Book ±1%</th>
<th>Volume 1h</th><th>Swaps</th><th>Fee 1h</th>
<th title="fee da hora anualizada sobre o TVL — comparável com agregadores">APR (TVL)</th>
<th title="fee da hora anualizada sobre o book ±1% — é o que a sua posição encontra">APR (book)</th>
<th title="volume desta hora dividido pela mediana das últimas leituras da mesma pool">Relev.</th>
<th>Idade</th></tr></thead>
<tbody>{"".join(corpo)}</tbody></table></div>

<p class="nota"><b>Duas colunas de APR, e a segunda é a que decide.</b> O APR sobre TVL é
o número que os agregadores publicam. Mas a fee que você recebe é
<code>L_sua / (L_ativa + L_sua)</code>, e <code>L_ativa</code> é só o que está no tick
corrente: uma pool de $5M com a liquidez longe do preço dilui menos que uma de $400k
concentrada nele. <b>Os dois anualizam UMA hora</b> — são voláteis por construção e não
são projeção, não decisão com amostra.</p>
<p class="nota"><b>A fee é a que chega ao LP, lida do contrato.</b> Na V3 o
<code>feeProtocol</code> do <code>slot0</code>: 0x66 (LP fica com 5/6) nas de 0,30% e 1%,
<b>0x44 (3/4) nas de 0,05%</b>. Na V4, lpFee e protocolFee de cada pool. Publicar o tier
cheio inflaria todo APR em 20% a 33%.</p>
{erros}

<footer>
Somente leitura: nenhuma chave, nenhuma assinatura, nenhuma transação.
Gerada por <code>python -m defime.jobs.pools_hora</code>, de hora em hora. Volume lido
da chain, não do banco: o indexador pode cobrir só parte das pools, e zero por falta de
coleta é indistinguível de zero por falta de negócio.
</footer>
</div>

<script>
(function () {{
  var f = {{tvl: 0, apr: -1, vol: 0, versao: "", fee: "", idade: 99999}};
  var linhas = [].slice.call(document.querySelectorAll("#tab tbody tr"));
  function aplicar() {{
    var n = 0;
    linhas.forEach(function (tr) {{
      var d = tr.dataset;
      var ok = (+d.tvl >= f.tvl) && (+d.vol >= f.vol)
        && (f.apr < 0 || +d.apr >= f.apr)
        && (!f.versao || d.versao === f.versao)
        && (!f.fee || d.fee === f.fee)
        && (+d.idade <= f.idade);
      tr.style.display = ok ? "" : "none";
      if (ok) n++;
    }});
    document.getElementById("conta").textContent = n + " de " + linhas.length;
  }}
  document.querySelectorAll(".fb").forEach(function (b) {{
    b.addEventListener("click", function () {{
      var g = b.parentNode;
      g.querySelectorAll(".fb").forEach(function (o) {{ o.classList.remove("on"); }});
      b.classList.add("on");
      var v = b.dataset.v;
      f[b.dataset.f] = (b.dataset.f === "versao" || b.dataset.f === "fee")
        ? v : +v;
      aplicar();
    }});
  }});
  aplicar();
}})();
</script>
</body></html>"""


def main() -> int:
    t0 = time.time()
    limite = None
    if "--limite" in sys.argv:
        limite = int(sys.argv[sys.argv.index("--limite") + 1])
    # `--relevantes` e o ciclo curto (a cada 2h); sem ele varre tudo, que e o
    # que o ciclo diario faz. As duas cadencias existem porque a cauda das
    # pools nao muda de hora em hora, mas some da tela se nunca for lida.
    so_rel = "--relevantes" in sys.argv
    css_p = ESTILO
    css = css_p.read_text(encoding="utf-8") if css_p.exists() else ""
    try:
        x = coletar(limite, so_relevantes=so_rel)
        SAIDA.parent.mkdir(parents=True, exist_ok=True)
        SAIDA.write_text(_html(x, css), encoding="utf-8")
    except Exception as exc:                                  # noqa: BLE001
        gravar_batimento("pools_hora", ok=False, erro=str(exc)[:300],
                         duracao_s=time.time() - t0)
        print(f"FALHOU: {type(exc).__name__}: {exc}")
        return 1
    gravar_batimento("pools_hora", itens=len(x["linhas"]),
                     duracao_s=time.time() - t0,
                     nota=f"erros={len(x['erros'])} "
                          f"modo={'relevantes' if so_rel else 'todas'}")
    print(SAIDA)
    print(f"  {len(x['linhas'])} pools · {len(x['erros'])} erro(s) · "
          f"{x['segundos']:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
