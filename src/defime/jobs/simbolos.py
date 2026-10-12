"""Nome e decimais de cada token da chain — lidos uma vez, guardados para sempre.

**O que motivou:** as paginas publicavam `V3 0x0f4227d2…` e `V4 0x8674c1c5…`
em metade das linhas, porque os nomes bons vinham de uma lista **escrita a mao**
— e pool nova nunca esta nela. Remendar a lista adiaria o problema ate a
proxima pool.

O nome verdadeiro esta no proprio token: `symbol()` e `decimals()`. Sao dois
`eth_call` por token e o resultado **nunca muda** — token nao troca de
simbolo. Entao isto e cache permanente (`token_simbolo`), nao trabalho por
ciclo: rodado uma vez, quem so le o cache continua com ZERO chamadas de RPC.

---

**Os dois lados da chain precisam de caminhos diferentes, e e por isso que
este job existe em vez de duas linhas no `pools_hora`.**

Na V3 `pool_param` ja tem `token0`/`token1`: o `_parametros` os le do proprio
contrato da pool. Sao 26 das 42 pools, e para elas basta resolver o simbolo.

Na V4 **nao ha contrato de pool** — o `PoolManager` e singleton e o "endereco"
e um hash da `PoolKey`. As moedas so aparecem no evento `Initialize`, e no
banco havia **um** desses para dezesseis pools V4. A saida e varrer o
`Initialize` do PoolManager: um `eth_getLogs` filtrado por topico devolve a
`PoolKey` de TODAS as pools V4 de uma vez, e ele preenche `token0`/`token1`
em `pool_param` — que estavam nulos e nunca teriam como se preencher sozinhos.

**A varredura encolhe a janela ate passar**, como o `pools_hora` e o backfill
ja fazem: `log query timed out` depende de quantos logs ha dentro, nao do
tamanho da faixa, e a primeira versao daquele padrao engoliu a excecao e
declarou "nenhum evento" onde havia consulta que nao voltou.

---

**Decimais entram no cache junto, e nao e detalhe de completude.** O
codigo antigo assumia `6` para a stable e `18` para o ativo — convencao
MEDIDA nas 18 pools RWA/USDG desta chain, mas convencao. Um token com outra
contagem produziria preco errado por um fator de dez elevado a alguma coisa,
silenciosamente, exatamente como supor a orientacao fez GME e SLV valerem
ZERO. Com os decimais lidos, quem sabe manda; a convencao vira so o fallback
declarado.

    python -m defime.jobs.simbolos            # resolve o que falta
    python -m defime.jobs.simbolos --v4       # varre tambem o Initialize da V4
    python -m defime.jobs.simbolos --listar   # mostra o cache
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

from defime.chain.abi_v4 import POOL_MANAGER, TOPIC0                # noqa: E402
from defime.chain.rpc import RpcClient                              # noqa: E402
from defime.db.registro import gravar_batimento                     # noqa: E402
from defime.logging import get_logger                               # noqa: E402

log = get_logger(__name__)

SEL_SYMBOL = "0x95d89b41"       # symbol()
SEL_DECIMALS = "0x313ce567"     # decimals()

# A stable desta chain. Fica nomeada aqui porque o nome do PAR sai dela: a
# pool e "<ativo>/USDG", e sem saber qual lado e a stable o rotulo sairia
# invertido — a mesma armadilha da orientacao, um nivel acima.
USDG = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"

# **`0x800000` no campo `fee` da V4 NAO e uma taxa: e a flag de taxa
# DINAMICA** (`LPFeeLibrary.DYNAMIC_FEE_FLAG`), que diz "quem define a taxa e
# o hook, a cada swap". Lida como numero da 8.388.608 pips = **838,86%**, e foi
# exatamente o que o primeiro rotulo publicou numa pool de SPY.
#
# Este arquivo ja registra a mesma armadilha na direcao oposta: "o agregador ja
# errou taxa em quatro pools V4 lendo o flag de taxa dinamica como 0,00%". As
# duas leituras erradas saem do mesmo campo — uma arredonda para baixo, a
# outra para cima, e nenhuma das duas levanta erro.
FLAG_TAXA_DINAMICA = 0x800000


def _banco() -> sqlite3.Connection:
    from defime.config import settings
    caminho = (settings().database_url.replace("sqlite:///", "")
               .replace("sqlite://", ""))
    c = sqlite3.connect(caminho, timeout=30)
    c.row_factory = sqlite3.Row
    return c


def criar_tabela(c: sqlite3.Connection) -> None:
    c.execute("""create table if not exists token_simbolo(
        endereco text primary key,
        simbolo text,
        decimais integer,
        visto_em integer not null)""")
    # **A `PoolKey` da V4 em tabela PROPRIA, e nao em colunas novas de
    # `pool_param`.** O `pools_hora._parametros` grava com
    # `insert or replace into pool_param values (?,?,?,?,?,?,?,?)` —
    # POSICIONAL. Uma coluna nova entraria no fim para o `create table` e no
    # meio para o banco antigo, e o `alter table` do SQLite acrescenta sempre
    # no fim: os valores sairiam embaralhados sem erro nenhum. O proprio
    # `pools_hora` ja documenta essa armadilha na tabela `pool_hora`.
    c.execute("""create table if not exists pool_v4_chave(
        pool text primary key,
        fee_pips integer,
        tick_spacing integer,
        hooks text,
        visto_em integer not null)""")
    c.commit()


def _texto(rpc, addr: str) -> str | None:
    """`symbol()` — e ele vem em DUAS formas nesta chain.

    O ERC-20 canonico devolve `string` (offset + tamanho + bytes); tokens
    antigos devolvem `bytes32` cru, com o nome preenchido de zeros a direita.
    Ler so a primeira forma devolveria lixo na segunda, e ler so a segunda
    devolveria o offset (`0x20`) como se fosse o nome. As duas sao tratadas —
    e o mesmo cuidado que `pool_raiox.texto` ja tinha.
    """
    try:
        b = bytes.fromhex(rpc.eth_call(addr, SEL_SYMBOL)[2:])
    except Exception as exc:                                  # noqa: BLE001
        log.warning("symbol_falhou", token=addr, erro=str(exc)[:200])
        return None
    if not b:
        return None
    if len(b) > 64:
        n = int.from_bytes(b[32:64], "big")
        s = b[64:64 + n].decode("utf8", "replace")
    else:
        s = b.rstrip(b"\0").decode("utf8", "replace")
    s = s.strip()
    return s or None


def _decimais(rpc, addr: str) -> int | None:
    try:
        return int(rpc.eth_call(addr, SEL_DECIMALS), 16)
    except Exception as exc:                                  # noqa: BLE001
        log.warning("decimals_falhou", token=addr, erro=str(exc)[:200])
        return None


def resolver(c: sqlite3.Connection, rpc, enderecos: list[str]) -> dict:
    """Resolve os que ainda nao estao no cache. Dois `eth_call` por token NOVO.

    **Falha de um token nao grava linha**, e a diferenca importa: gravar
    `simbolo = None` marcaria o token como resolvido e ele nunca mais seria
    tentado — o 429 de um minuto viraria um nome ausente para sempre. Ausencia
    se declara e se tenta de novo; e a mesma regra do `faixa_fechada_v3`, que
    tambem nao cacheia `None`.
    """
    ja = {r["endereco"] for r in c.execute(
        "select endereco from token_simbolo where simbolo is not null")}
    faltam = [e for e in dict.fromkeys(x.lower() for x in enderecos if x)
              if e not in ja]
    novos, falhos = 0, 0
    for end in faltam:
        sim = _texto(rpc, end)
        dec = _decimais(rpc, end)
        if sim is None:
            falhos += 1
            continue
        c.execute("insert or replace into token_simbolo values (?,?,?,?)",
                  (end, sim, dec, int(time.time())))
        novos += 1
    c.commit()
    return {"ja_tinha": len(ja), "novos": novos, "falhos": falhos,
            "pedidos": len(faltam)}


def _i(h: str) -> int:
    return int(h, 16)


def varrer_initialize(c: sqlite3.Connection, rpc, passo: int = 2_000_000) -> dict:
    """Preenche `token0`/`token1` das pools V4 a partir do `Initialize`.

    Na V4 nao existe contrato de pool para perguntar `token0()`: o
    `PoolManager` e singleton e o "endereco" da pool e o hash da `PoolKey`. As
    moedas so existem no evento `Initialize`, emitido uma vez na criacao — e
    ele e emitido pelo PoolManager, entao **uma varredura por topico devolve a
    chave de todas as pools V4 de uma vez**.

    Sem isto os dezesseis V4 do catalogo ficam com `token0` nulo para sempre,
    porque `_parametros` so le `token0()` quando ha contrato (`if not v4`).
    """
    topo = _i(rpc.call("eth_blockNumber"))
    achados, de = {}, 0
    janela = passo
    while de <= topo:
        ate = min(topo, de + janela - 1)
        try:
            logs = rpc.call("eth_getLogs", [{
                "address": POOL_MANAGER,
                "topics": [TOPIC0["Initialize"]],
                "fromBlock": hex(de), "toBlock": hex(ate)}])
        except Exception as exc:                              # noqa: BLE001
            # **Encolhe ate passar**, e nao engole a falha. `log query timed
            # out` depende de quantos logs ha dentro, nao do tamanho; a
            # primeira versao deste padrao no `pools_hora` aceitou a excecao e
            # treze pools sairam como "nenhum Swap" quando o que tinham era
            # uma consulta que nao voltou (22 -> 31 pools depois da correcao).
            if janela <= 20_000:
                raise
            janela = max(20_000, janela // 4)
            log.info("janela_encolheu", blocos=janela, de=de,
                     erro=str(exc)[:200])
            continue
        for lg in logs:
            # topics: [assinatura, id, currency0, currency1]
            # data:   fee, tickSpacing, hooks, sqrtPriceX96, tick
            t = lg["topics"]
            if len(t) < 4:
                continue
            d = (lg.get("data") or "0x")[2:]
            fee = int(d[0:64], 16) if len(d) >= 64 else None
            sp = int(d[64:128], 16) if len(d) >= 128 else None
            if sp is not None and sp >= (1 << 255):
                sp -= 1 << 256
            achados[t[1].lower()] = ("0x" + t[2][-40:], "0x" + t[3][-40:],
                                     fee, sp)
        de = ate + 1
        janela = min(passo, janela * 2)

    gravados, chaves = 0, 0
    agora = int(time.time())
    for pid, (c0, c1, fee, sp) in achados.items():
        cur = c.execute("select token0 from pool_param where lower(pool)=?",
                        (pid,)).fetchone()
        if cur is None:
            continue
        if cur["token0"] is None:
            c.execute("update pool_param set token0=?, token1=? "
                      "where lower(pool)=?", (c0, c1, pid))
            gravados += 1
        if fee is not None:
            # **A taxa NOMINAL, que e outra coisa que a medida.** Na V4 o campo
            # `fee` do evento `Swap` e o total (lpFee + protocolFee sobre a
            # entrada) — na UPS 3499 contra os 3000 declarados. O `pool_param`
            # guarda a medida, que e a certa para o CUSTO; o rotulo precisa da
            # declarada, que e a que a tela da Uniswap mostra. Chamar a pool
            # de "0,35%" quando o app dela diz "0,30%" quebraria justamente o
            # que o nome existe para fazer: identificar.
            c.execute("insert or replace into pool_v4_chave values (?,?,?,?,?)",
                      (pid, fee, sp, None, agora))
            chaves += 1
    c.commit()
    return {"initialize_lidos": len(achados), "pool_param_preenchidos": gravados,
            "chaves_gravadas": chaves, "ate_bloco": topo}


def nomes(c: sqlite3.Connection) -> dict[str, str]:
    """`{pool: "UPS/USDG"}` para o que o cache ja permite nomear.

    O par sai do lado que NAO e a stable. Com `usdg0` do `pool_param` — que e
    fato lido do contrato, nunca suposto — o lado certo se escolhe sem
    adivinhar; sem ele o rotulo poderia sair `USDG/UPS`, que e a mesma
    inversao que fez GME e SLV valerem zero, so que no nome.
    """
    sim = {r["endereco"]: r["simbolo"] for r in c.execute(
        "select endereco, simbolo from token_simbolo where simbolo is not null")}
    try:
        nominal, dinamicas = {}, set()
        for r in c.execute("select pool, fee_pips from pool_v4_chave "
                           "where fee_pips is not null"):
            if r["fee_pips"] & FLAG_TAXA_DINAMICA:
                dinamicas.add(r["pool"])
            else:
                nominal[r["pool"]] = r["fee_pips"]
    except sqlite3.OperationalError:
        nominal, dinamicas = {}, set()
    saida = {}
    for r in c.execute("select pool, usdg0, token0, token1, taxa, versao "
                       "from pool_param"):
        t0, t1 = (r["token0"] or "").lower(), (r["token1"] or "").lower()
        if not t0 or not t1:
            continue
        ativo = sim.get(t1 if r["usdg0"] else t0)
        estavel = sim.get(t0 if r["usdg0"] else t1)
        if not ativo or not estavel:
            continue
        nome = f"{ativo}/{estavel}"
        # A taxa entra no nome porque o MESMO par tem varias pools nesta chain
        # (GME 0,30% e GME 1%, RBLX 0,30% e RBLX 1%) e um seletor com duas
        # linhas identicas e pior que um com endereco: nao da para escolher.
        # A nominal (do `Initialize`) manda no rotulo; a medida so entra
        # quando nao ha nominal. Ver o comentario em `varrer_initialize`.
        chave = r["pool"].lower()
        if chave in dinamicas:
            # Pool de taxa dinamica NAO TEM tier estatico — publicar um numero
            # ali seria inventar o que o contrato se recusa a fixar. A taxa
            # medida continua saindo no cartao "Taxa da pool" da pagina, com a
            # fonte dita; aqui o rotulo so precisa IDENTIFICAR.
            nome += " dinâmica"
            saida[r["pool"]] = nome + (" v4" if r["versao"] == "v4" else "")
            continue
        taxa = nominal.get(chave)
        taxa = taxa / 1e6 if taxa is not None else r["taxa"]
        if taxa:
            # Virgula decimal e duas casas, como o resto do painel — mas taxa
            # inteira sai sem as casas ("1%", nao "1,00%"), que e como a lista
            # conferida a mao ja escrevia.
            t = f"{taxa * 100:.2f}"
            nome += " " + (t[:-3] if t.endswith(".00") else t.replace(".", ",")) + "%"
        if r["versao"] == "v4":
            nome += " v4"
        saida[r["pool"]] = nome
    return saida


def main() -> int:
    t0 = time.time()
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--v4", action="store_true",
                    help="varre o Initialize do PoolManager para achar as "
                         "moedas das pools V4")
    ap.add_argument("--listar", action="store_true")
    a = ap.parse_args()

    c = _banco()
    criar_tabela(c)
    if a.listar:
        for r in c.execute("select endereco, simbolo, decimais from "
                           "token_simbolo order by simbolo"):
            print(f"  {r['simbolo']:<12} {r['decimais']:>3}  {r['endereco']}")
        print(f"\n{len(nomes(c))} pools nomeaveis")
        for pool, nome in sorted(nomes(c).items(), key=lambda kv: kv[1]):
            print(f"  {nome:<22} {pool}")
        return 0

    rpc = RpcClient()
    v4 = {}
    try:
        if a.v4:
            v4 = varrer_initialize(c, rpc)
            print(f"Initialize: {v4['initialize_lidos']} lidos, "
                  f"{v4['pool_param_preenchidos']} pools V4 preenchidas")
        enderecos = []
        for r in c.execute("select token0, token1 from pool_param"):
            enderecos += [r["token0"], r["token1"]]
        r = resolver(c, rpc, enderecos)
        n = len(nomes(c))
        print(f"tokens: {r['novos']} novos, {r['ja_tinha']} em cache, "
              f"{r['falhos']} falharam")
        print(f"pools nomeaveis: {n}")
    except Exception as exc:                                  # noqa: BLE001
        gravar_batimento("simbolos", ok=False, erro=str(exc)[:300],
                         duracao_s=time.time() - t0)
        print(f"FALHOU: {type(exc).__name__}: {exc}")
        return 1
    finally:
        c.close()
    gravar_batimento("simbolos", itens=r["novos"], duracao_s=time.time() - t0,
                     nota=f"nomeaveis={n} v4={v4.get('pool_param_preenchidos', 0)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
