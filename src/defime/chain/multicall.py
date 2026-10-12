"""Multicall3: muitas `eth_call` numa chamada so, todas no MESMO bloco.

**Existe na chain 4663** — `eth_getCode` em 11/09 devolveu 3.808 bytes no
endereco canonico, o mesmo em quase toda EVM. E o equivalente, para contratos
comuns, do `extsload(bytes32[])` que derrubou o custo da fee pendente na V4:
la o `PoolManager` le slots em lote; aqui o Multicall3 empacota chamadas a
qualquer contrato.

**O bloco e FIXADO pelo chamador, e isso nao e detalhe.** Quando a lista nao
cabe num lote so, os lotes seguintes iriam para o `latest` do momento em que
saem — e a fee pendente mistura `feeGrowthGlobal` de um bloco com
`feeGrowthOutside` de outro. E a familia numero um deste projeto (comparar
dois instantes diferentes) dentro de uma unica leitura.

**O lote ENCOLHE em vez de desistir.** Um `eth_call` grande demais estoura o
teto de gas ou de tempo do no, e a resposta certa e dividir e repetir — a
licao que o `pools_hora` pagou com 22 de 31 pools e o backfill com `log query
timed out`. So uma chamada isolada que falha sobe como erro.

Chamada individual que reverte (`allowFailure=True`) volta `None` na posicao
dela, nunca bytes vazios lidos como zero.

Nao assina nada: e `eth_call`.
"""

from __future__ import annotations

from eth_abi import decode, encode

MULTICALL3 = "0xca11bde05977b3631167028862be2a173976ca11"
SEL_AGGREGATE3 = "0x82ad56cb"          # aggregate3((address,bool,bytes)[])


def _hex_para_bytes(h: str) -> bytes:
    return bytes.fromhex(h[2:] if h.startswith("0x") else h)


def codificar(chamadas: list[tuple[str, str]]) -> str:
    """Calldata do `aggregate3` para `[(alvo, calldata_hex), ...]`."""
    itens = [(_hex_para_bytes(alvo), True, _hex_para_bytes(dados))
             for alvo, dados in chamadas]
    return SEL_AGGREGATE3 + encode(["(address,bool,bytes)[]"], [itens]).hex()


def decodificar(resposta: str) -> list[str | None]:
    """`[(sucesso, bytes)]` do `aggregate3` -> hex de cada retorno, ou `None`."""
    (res,) = decode(["(bool,bytes)[]"], _hex_para_bytes(resposta))
    return ["0x" + b.hex() if ok and b else None for ok, b in res]


def aggregate3(rpc, chamadas: list[tuple[str, str]], bloco: int,
               lote: int = 250) -> list[str | None]:
    """Retornos de `chamadas`, na mesma ordem, lidos todos em `bloco`."""
    saida: list[str | None] = [None] * len(chamadas)
    pendentes = [(i, min(i + lote, len(chamadas)))
                 for i in range(0, len(chamadas), lote)]
    while pendentes:
        a, b = pendentes.pop(0)
        try:
            bruto = rpc.eth_call(MULTICALL3, codificar(chamadas[a:b]), bloco)
            partes = decodificar(bruto)
        except Exception:
            if b - a == 1:
                raise
            meio = (a + b) // 2
            pendentes[:0] = [(a, meio), (meio, b)]
            continue
        if len(partes) != b - a:
            raise ValueError(f"aggregate3 devolveu {len(partes)} retornos "
                             f"para {b - a} chamadas")
        saida[a:b] = partes
    return saida


__all__ = ["MULTICALL3", "aggregate3", "codificar", "decodificar"]
