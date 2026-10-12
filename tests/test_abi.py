"""Decodificacao de eventos. Sem rede."""

from __future__ import annotations

from eth_abi import encode as abi_encode
from eth_utils import keccak

from defime.chain.abi import BY_TOPIC0, TOPIC0, decode_log


def _pad_addr(addr: str) -> str:
    return "0x" + addr.lower().replace("0x", "").rjust(64, "0")


def _pad_int(value: int) -> str:
    return "0x" + (value & ((1 << 256) - 1)).to_bytes(32, "big").hex()


def test_topic0_bate_com_valores_canonicos():
    # Valores publicos e estaveis do Uniswap V3. Se mudarem, o indexer para
    # de enxergar eventos silenciosamente — por isso viram teste.
    assert TOPIC0["Swap"].lower() == (
        "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67")
    assert TOPIC0["Mint"].lower() == (
        "0x7a53080ba414158be7ec69b987b5fb7d07dee101fe85488f0853ae16239d0bde")
    assert TOPIC0["Burn"].lower() == (
        "0x0c396cd989a39f4459b5fa1aed6a9a8dcdbc45908acfd67e028cd568da98982c")


def test_topic0_deriva_da_assinatura():
    from defime.chain.abi import EVENTS
    for nome, (assinatura, *_rest) in EVENTS.items():
        assert TOPIC0[nome].lower() == "0x" + keccak(text=assinatura).hex().replace("0x", "")
        assert BY_TOPIC0[TOPIC0[nome].lower()] == nome


def test_decode_swap_com_amount_negativo():
    """O caso que quebra parser manual: int256 negativo em complemento de dois."""
    amount0, amount1 = 1_000_000, -2_500_000_000_000_000_000
    sqrt_price, liquidity, tick = 7_057_716_242_524_677_591_777_035_769_655_174, 1_392_623_218_260_537_858, 227_955
    data = "0x" + abi_encode(
        ["int256", "int256", "uint160", "uint128", "int24"],
        [amount0, amount1, sqrt_price, liquidity, tick]).hex()

    log = {"topics": [TOPIC0["Swap"],
                      _pad_addr("0x1111111111111111111111111111111111111111"),
                      _pad_addr("0x2222222222222222222222222222222222222222")],
           "data": data}
    nome, args = decode_log(log)
    assert nome == "Swap"
    assert args["amount0"] == str(amount0)
    assert args["amount1"] == str(amount1)          # negativo preservado
    assert args["sqrtPriceX96"] == str(sqrt_price)  # 256 bits como str
    assert args["tick"] == str(tick)
    assert args["sender"] == "0x1111111111111111111111111111111111111111"


def test_decode_mint_com_tick_negativo_indexado():
    """tick negativo indexado vem estendido em sinal para 32 bytes."""
    tick_lower, tick_upper = -227_940, -100
    data = "0x" + abi_encode(
        ["address", "uint128", "uint256", "uint256"],
        ["0x3333333333333333333333333333333333333333", 500, 10, 20]).hex()
    log = {"topics": [TOPIC0["Mint"],
                      _pad_addr("0x4444444444444444444444444444444444444444"),
                      _pad_int(tick_lower),
                      _pad_int(tick_upper)],
           "data": data}
    nome, args = decode_log(log)
    assert nome == "Mint"
    assert args["tickLower"] == str(tick_lower)
    assert args["tickUpper"] == str(tick_upper)
    assert args["amount"] == "500"


def test_evento_desconhecido_e_ignorado_sem_explodir():
    assert decode_log({"topics": ["0x" + "ab" * 32], "data": "0x"}) is None
    assert decode_log({"topics": [], "data": "0x"}) is None


def test_data_truncado_nao_produz_lixo():
    """Preferir None a devolver numero errado."""
    log = {"topics": [TOPIC0["Swap"],
                      _pad_addr("0x1" + "0" * 39),
                      _pad_addr("0x2" + "0" * 39)],
           "data": "0x" + "00" * 32}          # falta dado
    assert decode_log(log) is None
