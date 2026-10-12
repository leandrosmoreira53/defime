"""Assinaturas e decodificacao dos eventos da pool Uniswap V3.

Usa eth_abi/eth_utils (vem com web3) em vez de parsing manual: decodificar
int24 negativo e int256 em complemento de dois na mao e fonte classica de bug
silencioso, e isto e a fundacao de todo o resto.

Valores de 256 bits saem como str: int do Python e ilimitado, o do banco nao.
A conversao para int acontece na Camada 2, nunca aqui.
"""

from __future__ import annotations

from eth_abi import decode as abi_decode
from eth_utils import keccak

# nome -> (assinatura, tipos indexados, tipos nao-indexados, nomes nao-indexados)
EVENTS = {
    "Swap": (
        "Swap(address,address,int256,int256,uint160,uint128,int24)",
        [("sender", "address"), ("recipient", "address")],
        ["int256", "int256", "uint160", "uint128", "int24"],
        ["amount0", "amount1", "sqrtPriceX96", "liquidity", "tick"],
    ),
    "Mint": (
        "Mint(address,address,int24,int24,uint128,uint256,uint256)",
        [("owner", "address"), ("tickLower", "int24"), ("tickUpper", "int24")],
        ["address", "uint128", "uint256", "uint256"],
        ["sender", "amount", "amount0", "amount1"],
    ),
    "Burn": (
        "Burn(address,int24,int24,uint128,uint256,uint256)",
        [("owner", "address"), ("tickLower", "int24"), ("tickUpper", "int24")],
        ["uint128", "uint256", "uint256"],
        ["amount", "amount0", "amount1"],
    ),
    "Collect": (
        "Collect(address,address,int24,int24,uint128,uint128)",
        [("owner", "address"), ("tickLower", "int24"), ("tickUpper", "int24")],
        ["address", "uint128", "uint128"],
        ["recipient", "amount0", "amount1"],
    ),
    "Initialize": (
        "Initialize(uint160,int24)",
        [],
        ["uint160", "int24"],
        ["sqrtPriceX96", "tick"],
    ),
    "Flash": (
        "Flash(address,address,uint256,uint256,uint256,uint256)",
        [("sender", "address"), ("recipient", "address")],
        ["uint256", "uint256", "uint256", "uint256"],
        ["amount0", "amount1", "paid0", "paid1"],
    ),
    # Descoberto pelo criterio 3 do check-1: o saldo divergia 0,58%/0,67%
    # porque a taxa de protocolo sai da pool por aqui. feeProtocol=102 nesta
    # pool => 6 e 6 => o protocolo fica com 1/6 das fees dos dois lados.
    "CollectProtocol": (
        "CollectProtocol(address,address,uint128,uint128)",
        [("sender", "address"), ("recipient", "address")],
        ["uint128", "uint128"],
        ["amount0", "amount1"],
    ),
}

# Eventos do NonfungiblePositionManager.
#
# Por que existem separados: o Mint da POOL traz owner = contrato do NPM, nunca
# a NFT. Sem estes eventos e impossivel atribuir liquidez a uma posicao
# especifica — nossa ou de concorrente.
#
# `Collect` existe nos dois contratos com assinaturas diferentes, logo topic0
# diferente. Mantemos registries separados para que o nome nao colida.
NPM_EVENTS = {
    "IncreaseLiquidity": (
        "IncreaseLiquidity(uint256,uint128,uint256,uint256)",
        [("tokenId", "uint256")],
        ["uint128", "uint256", "uint256"],
        ["liquidity", "amount0", "amount1"],
    ),
    "DecreaseLiquidity": (
        "DecreaseLiquidity(uint256,uint128,uint256,uint256)",
        [("tokenId", "uint256")],
        ["uint128", "uint256", "uint256"],
        ["liquidity", "amount0", "amount1"],
    ),
    "NpmCollect": (
        "Collect(uint256,address,uint256,uint256)",
        [("tokenId", "uint256")],
        ["address", "uint256", "uint256"],
        ["recipient", "amount0", "amount1"],
    ),
    "NftTransfer": (
        "Transfer(address,address,uint256)",
        [("from", "address"), ("to", "address"), ("tokenId", "uint256")],
        [],
        [],
    ),
}

TOPIC0 = {name: "0x" + keccak(text=spec[0]).hex().replace("0x", "")
          for name, spec in EVENTS.items()}
NPM_TOPIC0 = {name: "0x" + keccak(text=spec[0]).hex().replace("0x", "")
              for name, spec in NPM_EVENTS.items()}
BY_TOPIC0 = {v.lower(): k for k, v in TOPIC0.items()}
NPM_BY_TOPIC0 = {v.lower(): k for k, v in NPM_TOPIC0.items()}

# Eventos que o indexer coleta. Flash e raro mas barato de guardar.
INDEXED_EVENTS = ["Swap", "Mint", "Burn", "Collect", "Initialize", "Flash",
                  "CollectProtocol"]


def _topic_to_int(topic_hex: str, signed: bool) -> int:
    raw = bytes.fromhex(topic_hex.replace("0x", ""))
    return int.from_bytes(raw, "big", signed=signed)


def _stringify(value):
    """int vira str (256 bits nao cabem no banco); bytes viram hex."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return str(value)
    if isinstance(value, (bytes, bytearray)):
        return "0x" + bytes(value).hex()
    return value


def decode_log(log: dict, registry: str = "pool") -> tuple[str, dict] | None:
    """Decodifica um log cru do eth_getLogs. Devolve (evento, args) ou None.

    registry: "pool" para eventos da UniswapV3Pool, "npm" para o
    NonfungiblePositionManager. Sao registries separados porque `Collect` existe
    nos dois com assinaturas distintas.
    """
    tabela, indice = ((EVENTS, BY_TOPIC0) if registry == "pool"
                      else (NPM_EVENTS, NPM_BY_TOPIC0))
    topics = log.get("topics") or []
    if not topics:
        return None
    name = indice.get(topics[0].lower())
    if not name:
        return None

    _, indexed, types, names = tabela[name]
    args: dict = {}

    for i, (field, kind) in enumerate(indexed, start=1):
        if i >= len(topics):
            return None
        if kind == "address":
            args[field] = "0x" + topics[i][-40:].lower()
        else:
            # int24 indexado vem estendido em sinal para 32 bytes
            args[field] = _topic_to_int(topics[i], signed=kind.startswith("int"))
            if kind.startswith("uint"):
                args[field] = _topic_to_int(topics[i], signed=False)

    data = log.get("data") or "0x"
    raw = bytes.fromhex(data.replace("0x", ""))
    if types:
        if len(raw) < 32 * len(types):
            return None
        values = abi_decode(types, raw)
        for field, value in zip(names, values):
            if isinstance(value, str) and field in ("sender", "recipient"):
                args[field] = value.lower()
            else:
                args[field] = value

    return name, {k: _stringify(v) for k, v in args.items()}


__all__ = ["EVENTS", "TOPIC0", "BY_TOPIC0", "INDEXED_EVENTS", "decode_log"]
