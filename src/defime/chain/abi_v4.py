"""Uniswap V4 — eventos do `PoolManager`. Validado contra a chain, nao suposto.

O V3 tem um contrato por pool e o endereco identifica. O V4 tem um **singleton**
e a identidade e o `PoolId`, um `bytes32`. Por isso `pool_address` (a coluna que
o inviolavel 8 exige em toda tabela) recebe 66 caracteres aqui em vez de 42 — e
por isso ela sempre foi texto, nao binario de 20 bytes.

**Medido em 2026-08-28**, PoolManager `0x8366a39c…` na Robinhood Chain:

  - `Swap` decodificado e conferido: o tick recalculado de `sqrtPriceX96` bate
    exatamente com o campo `tick` do evento (−221653 nos dois).
  - A ordem dos tokens foi testada nas duas hipoteses e so uma fecha: na
    USO/USDG V4, `amount0` e USDG (6 casas) e `amount1` e USO (18). A razao
    entre as duas pernas deu 1,0020 — com a ordem trocada daria 1e24.
  - `ModifyLiquidity` traz `salt`, e o salt carrega o **tokenId** da posicao.
    59 salts distintos em 177 eventos, esparsos entre 905.736 e 1.033.101,
    porque o tokenId e global entre todas as pools V4.

**A taxa se MEDE, nao se le.** Cada `Swap` carrega o campo `fee` aplicado
naquele swap. Medido:

    USO/USDG   V4   0,2899%  constante em 137 swaps  (API dizia 0,30%)
    TTWO/USDG  V4   0,8992%  constante em 511 swaps  (API dizia 0,80%)
    TTWO/USDG  V4   0,8793%  constante em  45 swaps  (API dizia 0,78%)

Nas tres o valor observado difere do declarado, e para cima nas duas TTWO. A
causa esta em aberto — pode ser hook de taxa dinamica, pode ser o campo incluir
a parcela do protocolo. **Enquanto nao se sabe, vale o medido**, que e o que o
LP de fato cobrou.

O que ainda NAO foi resolvido, e esta declarado como tal:
  - `Initialize` nao foi achado em 6M blocos (a pool tem ~12M) — sem ele nao ha
    `tickSpacing` nem endereco do **hook**;
  - nao existe evento `Collect` no V4. A coleta de fee vira `ModifyLiquidity`
    com delta zero e o valor sai pelo flash accounting (`take`/`settle`). Isso
    quebra a reconstrucao de fee que o `lifecycle.py` faz no V3.
"""

from __future__ import annotations

from eth_utils import keccak

# Singleton na Robinhood Chain. Descoberto filtrando `Swap` por um `poolId`
# conhecido SEM filtrar endereco: todos os 36 logs vieram deste contrato.
POOL_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"

EVENTOS = {
    "Initialize": ("Initialize(bytes32,address,address,uint24,int24,address,"
                   "uint160,int24)"),
    "ModifyLiquidity": "ModifyLiquidity(bytes32,address,int24,int24,int256,bytes32)",
    "Swap": "Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)",
    "Donate": "Donate(bytes32,address,uint256,uint256)",
}

TOPIC0 = {nome: "0x" + keccak(text=sig).hex().replace("0x", "")
          for nome, sig in EVENTOS.items()}
BY_TOPIC0 = {v.lower(): k for k, v in TOPIC0.items()}

# O que vale coletar. `Donate` e raro e barato de guardar, como o `Flash` no V3.
INDEXED_EVENTS = ["Swap", "ModifyLiquidity", "Initialize", "Donate"]


# `extsload(bytes32)` do PoolManager: le um slot de storage cru. E por ele que o
# estado de uma pool V4 sai do contrato, ja que nao ha contrato por pool.
SEL_EXTSLOAD = "0x1e2eaeaf"


def slot_da_pool(pool_id: str) -> int:
    """Slot do estado no mapping `_pools` (indice 6) do `PoolManager`.

    A primeira palavra desse slot empacota sqrtPriceX96 (160 bits) e o tick (int24).
    """
    return int.from_bytes(
        keccak(bytes.fromhex(pool_id[2:]) + (6).to_bytes(32, "big")), "big")


def _palavra(dados: bytes, i: int, com_sinal: bool = True) -> int:
    """Palavra `i` de 32 bytes. `int` sempre — float so na apresentacao."""
    bruto = int.from_bytes(dados[i * 32:(i + 1) * 32], "big")
    if com_sinal and bruto >= 1 << 255:
        bruto -= 1 << 256
    return bruto


def decode_log(entry: dict) -> tuple[str, dict] | None:
    """(nome, args) de um log do PoolManager, ou None se nao for dos nossos.

    Nunca levanta por log desconhecido: o singleton emite eventos de TODAS as
    pools da chain, entao encontrar coisa que nao interessa e o caso normal.
    """
    topics = entry.get("topics") or []
    if not topics:
        return None
    nome = BY_TOPIC0.get(topics[0].lower())
    if nome is None:
        return None
    d = bytes.fromhex(entry["data"][2:]) if entry.get("data") else b""
    pool_id = topics[1] if len(topics) > 1 else None

    if nome == "Swap":
        return nome, {
            "pool_id": pool_id,
            "sender": "0x" + topics[2][-40:] if len(topics) > 2 else None,
            "amount0": _palavra(d, 0),
            "amount1": _palavra(d, 1),
            "sqrtPriceX96": _palavra(d, 2, False),
            "liquidity": _palavra(d, 3, False),
            "tick": _palavra(d, 4),
            # Taxa APLICADA neste swap, em pips (1e-6). Ver o cabecalho: e ela
            # que vale, nao o tier declarado pelo agregador.
            "fee": _palavra(d, 5, False),
        }
    if nome == "ModifyLiquidity":
        return nome, {
            "pool_id": pool_id,
            "sender": "0x" + topics[2][-40:] if len(topics) > 2 else None,
            "tickLower": _palavra(d, 0),
            "tickUpper": _palavra(d, 1),
            # Negativo = retirada. No V3 isso seriam dois eventos (Mint/Burn).
            "liquidityDelta": _palavra(d, 2),
            "salt": "0x" + d[3 * 32:4 * 32].hex() if len(d) >= 128 else None,
            "token_id": (int.from_bytes(d[3 * 32:4 * 32], "big")
                         if len(d) >= 128 else None),
        }
    if nome == "Initialize":
        return nome, {
            "pool_id": pool_id,
            "currency0": "0x" + topics[2][-40:] if len(topics) > 2 else None,
            "currency1": "0x" + topics[3][-40:] if len(topics) > 3 else None,
            "fee": _palavra(d, 0, False),
            "tickSpacing": _palavra(d, 1),
            "hooks": "0x" + d[2 * 32 + 12:3 * 32].hex(),
            "sqrtPriceX96": _palavra(d, 3, False),
            "tick": _palavra(d, 4),
        }
    if nome == "Donate":
        return nome, {
            "pool_id": pool_id,
            "sender": "0x" + topics[2][-40:] if len(topics) > 2 else None,
            "amount0": _palavra(d, 0, False),
            "amount1": _palavra(d, 1, False),
        }
    return None


def preco_do_tick(tick: int, dec0: int, dec1: int, *,
                  quote_e_token0: bool) -> float:
    """Preco do ativo VOLATIL na stablecoin. `quote_e_token0` e obrigatorio.

    A conta bruta do V3/V4 e `1,0001^tick`, que da token1 por token0. O que se
    quer e sempre o preco do ativo em dolar, e **de que lado a stablecoin esta
    muda a formula** — nao ha default seguro, por isso o parametro e keyword e
    sem valor padrao.

    Medido em 2026-08-28, as duas pools do MESMO agregador, ordens opostas:

        USO/USDG  V4   c0=USDG(6)  c1=USO(18)    tick +227.689  ->  $129,45
        TTWO/USDG V4   c0=TTWO(18) c1=USDG(6)    tick -221.878  ->  $231,44

    A TTWO fechou a $233,00 na bolsa no mesmo dia — 0,7%, compativel com basis
    de pool. E essa conferencia que valida a decodificacao inteira.

    **Errar o lado nao levanta excecao, devolve numero:** com a convencao da
    USO, o tick da TTWO da 4,3e21 dolares. Foi assim que eu errei na primeira
    versao deste modulo, e por isso o parametro e obrigatorio.
    """
    bruto = (1.0001 ** tick) * (10 ** (dec0 - dec1))   # token1 por token0
    if quote_e_token0:
        # stablecoin e o token0 => `bruto` e "quanto do volatil por 1 dolar"
        return 1.0 / bruto if bruto else float("inf")
    return bruto


def taxa_medida(swaps: list[dict]) -> dict:
    """Taxa REAL da pool, a partir dos swaps. Nao le tier de lugar nenhum.

    Devolve a distribuicao, nao so a moda: taxa que varia entre swaps significa
    hook dinamico, e isso muda toda projecao de fee. Uma taxa so significa
    estatica — que e o que se mediu nas tres pools ate agora.
    """
    fees = [s["fee"] for s in swaps if s.get("fee") is not None]
    if not fees:
        return {"n": 0, "estatica": None, "motivo": "nenhum swap com fee"}
    distintas = sorted(set(fees))
    return {
        "n": len(fees),
        "distintas": len(distintas),
        "estatica": len(distintas) == 1,
        "taxa_pct": distintas[0] / 10_000 if len(distintas) == 1 else None,
        "min_pct": distintas[0] / 10_000,
        "max_pct": distintas[-1] / 10_000,
        "aviso": (None if len(distintas) == 1 else
                  "taxa VARIA entre swaps: ha hook dinamico, e projecao de fee "
                  "com taxa fixa fica errada"),
    }


__all__ = ["SEL_EXTSLOAD", "slot_da_pool", "POOL_MANAGER", "EVENTOS", "TOPIC0", "BY_TOPIC0", "INDEXED_EVENTS",
           "decode_log", "taxa_medida", "preco_do_tick"]
