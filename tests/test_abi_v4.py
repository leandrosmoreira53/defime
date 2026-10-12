"""Uniswap V4 — decodificacao dos eventos do PoolManager. Sem rede: logs reais.

Os logs abaixo foram capturados da Robinhood Chain em 2026-08-28. Sao dado, nao
exemplo: `test_preco_da_ttwo_bate_com_a_bolsa` so passa porque a decodificacao
inteira esta certa — tick, decimais e ordem de token.
"""

from __future__ import annotations

from defime.chain import abi_v4 as V4

# Swap real da TTWO/USDG V4 (poolId 0xc9dad63b...), bloco ~47.9M.
SWAP_TTWO = {
    "topics": [V4.TOPIC0["Swap"],
               "0xc9dad63b6fb9afe1fe888ebc7b023c587c58b9498f5bd17166226c0995dbc4eb",
               "0x00000000000000000000000058daec3116aae6d93017baaea7749052e8a04fa7"],
    "data": "0x" + "".join([
        f"{(1 << 256) - 100000000000000000:064x}",   # amount0 negativo
        f"{23144:064x}",                              # amount1
        f"{1218965155460757705124581:064x}",          # sqrtPriceX96
        f"{391861877845202345:064x}",                 # liquidity
        f"{(1 << 256) - 221878:064x}",                # tick negativo
        f"{8992:064x}",                               # fee em pips
    ]),
}


def test_topicos_sao_os_do_v4():
    """Se mudarem, nada mais funciona — e o erro seria silencioso (zero logs)."""
    assert V4.TOPIC0["Swap"].startswith("0x40e9cecb")
    assert V4.TOPIC0["ModifyLiquidity"].startswith("0xf208f491")
    assert V4.TOPIC0["Initialize"].startswith("0xdd466e67")
    assert V4.POOL_MANAGER == "0x8366a39cc670b4001a1121b8f6a443a643e40951"


def test_decodifica_swap_real():
    nome, a = V4.decode_log(SWAP_TTWO)
    assert nome == "Swap"
    assert a["amount0"] == -100000000000000000      # sinal preservado
    assert a["amount1"] == 23144
    assert a["tick"] == -221878                     # int24 negativo
    assert a["liquidity"] == 391861877845202345
    assert a["fee"] == 8992
    assert a["sender"].endswith("58daec3116aae6d93017baaea7749052e8a04fa7")


def test_preco_da_ttwo_bate_com_a_bolsa():
    """A validacao que prova a decodificacao inteira.

    tick -221878 com `c0=TTWO(18)`, `c1=USDG(6)` da $231,44. A TTWO fechou a
    $233,00 no mesmo dia — 0,7% de diferenca, compativel com basis de pool.
    """
    p = V4.preco_do_tick(-221878, 18, 6, quote_e_token0=False)
    assert 225 < p < 240


def test_ordem_errada_de_token_nao_da_erro_da_numero():
    """O perigo real: errar o lado devolve NUMERO, nao excecao.

    Nas duas combinacoes erradas o resultado e absurdo por muitas ordens de
    grandeza — 2,3e-22 num sentido, 4,3e21 no outro. Nenhuma levanta. Foi assim
    que eu errei na primeira versao, e e por isso que `quote_e_token0` nao tem
    default.
    """
    for dec0, dec1, q0 in ((6, 18, False), (6, 18, True)):
        errado = V4.preco_do_tick(-221878, dec0, dec1, quote_e_token0=q0)
        assert not (1 < errado < 100_000), (
            f"combinacao errada devolveu {errado}, que parece plausivel")


def test_uso_v4_tem_a_ordem_oposta():
    """Na USO/USDG V4 o tick e POSITIVO e a ordem e c0=USDG(6), c1=USO(18)."""
    p = V4.preco_do_tick(227689, 6, 18, quote_e_token0=True)
    assert 120 < p < 140


def test_log_de_outra_pool_nao_levanta():
    """O singleton emite eventos de TODAS as pools — ignorar e o caso normal."""
    assert V4.decode_log({"topics": ["0x" + "ab" * 32], "data": "0x"}) is None
    assert V4.decode_log({"topics": [], "data": "0x"}) is None


def test_taxa_estatica_e_reconhecida():
    """Medido: 461 swaps da TTWO, todos a 0,8992%."""
    r = V4.taxa_medida([{"fee": 8992}] * 461)
    assert r["estatica"] is True
    assert abs(r["taxa_pct"] - 0.8992) < 1e-9
    assert r["aviso"] is None


def test_taxa_que_varia_gera_aviso():
    """Taxa variando entre swaps = hook dinamico, e projecao com taxa fixa erra."""
    r = V4.taxa_medida([{"fee": 3000}, {"fee": 8992}])
    assert r["estatica"] is False
    assert "hook dinamico" in r["aviso"]


def test_sem_swap_nao_inventa_taxa():
    assert V4.taxa_medida([])["estatica"] is None


def test_modify_liquidity_traz_o_tokenid_no_salt():
    log = {
        "topics": [V4.TOPIC0["ModifyLiquidity"], "0x" + "cd" * 32,
                   "0x" + "00" * 12 + "58daec3116aae6d93017baaea7749052e8a04fa7"],
        "data": "0x" + "".join([
            f"{(1 << 256) - 222000:064x}",
            f"{(1 << 256) - 221920:064x}",
            f"{990083606354914:064x}",
            f"{1021245:064x}",
        ]),
    }
    nome, a = V4.decode_log(log)
    assert nome == "ModifyLiquidity"
    assert a["tickLower"] == -222000 and a["tickUpper"] == -221920
    assert a["liquidityDelta"] == 990083606354914
    assert a["token_id"] == 1021245


def test_delta_negativo_e_retirada():
    """No V3 seriam dois eventos (Mint/Burn); no V4 e um so, com sinal."""
    log = {
        "topics": [V4.TOPIC0["ModifyLiquidity"], "0x" + "cd" * 32,
                   "0x" + "00" * 12 + "58daec3116aae6d93017baaea7749052e8a04fa7"],
        "data": "0x" + "".join([f"{0:064x}", f"{0:064x}",
                                f"{(1 << 256) - 2711664001034535:064x}",
                                f"{1:064x}"]),
    }
    assert V4.decode_log(log)[1]["liquidityDelta"] == -2711664001034535


def test_lado_da_stablecoin_e_obrigatorio():
    """Sem default: errar o lado devolve numero, nao excecao."""
    import inspect
    par = inspect.signature(V4.preco_do_tick).parameters["quote_e_token0"]
    assert par.default is inspect.Parameter.empty
    assert par.kind is inspect.Parameter.KEYWORD_ONLY
