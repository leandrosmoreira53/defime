"""Reconstrucao de posicoes a partir dos eventos. Camada 2 — sem rede.

Como as pecas se encaixam:

  pool.Mint / pool.Burn          tem tickLower/tickUpper, mas owner = NPM
  npm.IncreaseLiquidity          tem tokenId e liquidez, mas nao os ticks
  --> junta-se pelo tx_hash: acontecem na mesma transacao.

Decomposicao das fees, que e onde quase todo mundo erra:

  npm.Collect paga fees E principal ao mesmo tempo. O principal e o que
  DecreaseLiquidity creditou em tokensOwed. Logo:

      fees_coletadas = SOMA(npm.Collect) - SOMA(npm.DecreaseLiquidity)

  Testar Collect sozinho contra "fees" superestima sempre que houve saque de
  liquidez. Ver C9 do plano do projeto.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import select

from ..config import settings
from ..db.models import PoolEvent
from ..db.session import session_scope
from .v3math import (amounts_for_liquidity, price_to_usd,
                     sqrt_price_x96_to_price, tick_to_sqrt_price_x96,
                     tick_to_usd)


@dataclass
class Movimento:
    bloco: int
    ts: int
    tipo: str            # increase | decrease | collect | transfer
    liquidez: int = 0
    amount0: int = 0
    amount1: int = 0
    tx: str = ""


@dataclass
class Posicao:
    token_id: int
    tick_lower: int | None = None
    tick_upper: int | None = None
    liquidez: int = 0
    depositado0: int = 0
    depositado1: int = 0
    sacado0: int = 0
    sacado1: int = 0
    coletado0: int = 0
    coletado1: int = 0
    primeiro_ts: int | None = None
    ultimo_ts: int | None = None
    movimentos: list[Movimento] = field(default_factory=list)

    @property
    def fees_coletadas0(self) -> int:
        """Collect menos o principal creditado por DecreaseLiquidity."""
        return self.coletado0 - self.sacado0

    @property
    def fees_coletadas1(self) -> int:
        return self.coletado1 - self.sacado1

    @property
    def ativa(self) -> bool:
        return self.liquidez > 0

    def idade_dias(self, agora_ts: int | None = None) -> float | None:
        """Idade = primeiro mint -> AGORA (nao ate o ultimo evento).

        A primeira versao media primeiro->ultimo evento e dava 3,98 dias contra
        4,686 da Revert: uma posicao parada nao para de envelhecer, e APR
        calculado sobre a janela errada fica inflado.
        """
        if self.primeiro_ts is None:
            return None
        if agora_ts is None:
            import time as _t
            agora_ts = int(_t.time())
        return (agora_ts - self.primeiro_ts) / 86400

    @property
    def janela_ativa_dias(self) -> float | None:
        """Do primeiro ao ultimo evento — util para posicoes ja encerradas."""
        if self.primeiro_ts is None or self.ultimo_ts is None:
            return None
        return (self.ultimo_ts - self.primeiro_ts) / 86400

    def faixa_usd(self, dec0: int, dec1: int) -> tuple[Decimal, Decimal] | None:
        if self.tick_lower is None or self.tick_upper is None:
            return None
        # tick maior => USD menor nesta pool (preco nativo e invertido)
        return (tick_to_usd(self.tick_upper, dec0, dec1),
                tick_to_usd(self.tick_lower, dec0, dec1))

    def amounts_agora(self, sqrt_price_x96: int) -> tuple[int, int]:
        if self.tick_lower is None or self.tick_upper is None or not self.liquidez:
            return 0, 0
        return amounts_for_liquidity(sqrt_price_x96,
                                     tick_to_sqrt_price_x96(self.tick_lower),
                                     tick_to_sqrt_price_x96(self.tick_upper),
                                     self.liquidez)

    def em_range(self, tick_atual: int) -> bool | None:
        if self.tick_lower is None or self.tick_upper is None:
            return None
        return self.tick_lower <= tick_atual < self.tick_upper


def defasagem_npm(pool: str | None = None, *,
                  ate_bloco: int | None = None) -> dict:
    """Quanto o indice do NPM esta atrasado em relacao ao da pool.

    Existe porque a falha e SILENCIOSA e cara. Os eventos do NPM sao coletados
    por recibo de transacao (`ingest/npm_link.py`), num passo separado do
    backfill. Se esse passo ficar para tras, todo `DecreaseLiquidity` recente
    some — e `reconstruir_posicoes` devolve posicoes que ainda "tem" liquidez
    que a pool ja devolveu. Medido aqui: 312.437 blocos de atraso produziram
    **+6,49%** de liquidez fantasma, sem erro nenhum aparecer.

    Nenhum consumidor deve confiar nas posicoes com `em_dia=False`.

    `ate_bloco` responde uma pergunta diferente e igualmente necessaria: "o
    indice esta completo ATE este bloco?". Sem esse recorte, reconciliar num
    bloco antigo e seguro herda o atraso do topo do banco e reprova uma conta
    que esta certa. O `atraso_blocos` continua sendo do topo real — quem
    pergunta sobre o passado quer saber do passado, mas o relatorio quer saber
    do agora.
    """
    cfg = settings()
    pool = (pool or cfg.pool_address).lower()
    npm = cfg.npm_address.lower()
    with session_scope() as sess:
        stmt_pool = (select(PoolEvent.block_number)
                     .where(PoolEvent.pool_address == pool)
                     .order_by(PoolEvent.block_number.desc()).limit(1))
        ult_pool = sess.execute(stmt_pool).scalar()
        ult_npm = sess.execute(
            select(PoolEvent.block_number)
            .where(PoolEvent.pool_address == npm)
            .order_by(PoolEvent.block_number.desc()).limit(1)).scalar()
        # eventos de liquidez de NFT (owner = NPM) sem par no indice do NPM
        stmt_orfaos = (select(PoolEvent.event, PoolEvent.block_number,
                              PoolEvent.args)
                       .where(PoolEvent.pool_address == pool,
                              PoolEvent.event.in_(("Mint", "Burn")),
                              PoolEvent.block_number > (ult_npm or 0)))
        if ate_bloco is not None:
            stmt_orfaos = stmt_orfaos.where(PoolEvent.block_number <= ate_bloco)
        orfaos = sess.execute(stmt_orfaos).all()
    burns = [b for nome, b, a in orfaos
             if nome == "Burn" and int(a["amount"]) and a["owner"].lower() == npm]
    # Mint orfao tambem conta, e a primeira versao nao olhava para ele.
    # Achado pelo criterio 5 do check-HISTORY-1: com 13.630 blocos de atraso
    # havia 3 Mints de NFT (1,85e16 de liquidez) sem `IncreaseLiquidity`
    # correspondente, e `em_dia` dizia True. O efeito e o espelho do de Burn:
    # Burn faltando INFLA a liquidez reconstruida (+6,49% medido na Fase 4),
    # Mint faltando a SUBTRAI — e a reconciliacao com slot0 errava 0,40%
    # apontando para um bug que nao existia.
    mints = [b for nome, b, a in orfaos
             if nome == "Mint" and int(a["amount"]) and a["owner"].lower() == npm]
    return {
        "ultimo_bloco_pool": ult_pool,
        "ultimo_bloco_npm": ult_npm,
        "atraso_blocos": (ult_pool - ult_npm) if (ult_pool and ult_npm) else None,
        "burns_de_nft_sem_par": len(burns),
        "mints_de_nft_sem_par": len(mints),
        "ate_bloco": ate_bloco,
        "em_dia": bool(ult_pool and ult_npm and not burns and not mints),
    }


def liquidez_fora_do_npm(pool: str | None = None, *,
                         ate_bloco: int | None = None) -> dict:
    """Liquidez provida DIRETO na pool, sem NFT. Nao aparece em posicao nenhuma.

    Descoberto na Fase 4: `0xe9e25ad7...` faz Mint direto, sem passar pelo NPM.
    Hoje sao 0,00066% da liquidez ativa — desprezivel em tamanho, mas nao em
    principio: o criterio 1 do plano do projeto supoe que toda liquidez e NFT, e nao e.
    A conta que fecha e  NFT + direto == slot0, nao  NFT == slot0.
    """
    cfg = settings()
    pool = (pool or cfg.pool_address).lower()
    npm = cfg.npm_address.lower()
    with session_scope() as sess:
        stmt = (select(PoolEvent.event, PoolEvent.args)
                .where(PoolEvent.pool_address == pool,
                       PoolEvent.event.in_(("Mint", "Burn")))
                .order_by(PoolEvent.block_number, PoolEvent.log_index))
        if ate_bloco is not None:
            stmt = stmt.where(PoolEvent.block_number <= ate_bloco)
        eventos = sess.execute(stmt).all()
    net: dict[int, int] = {}
    donos: dict[str, int] = {}
    for nome, a in eventos:
        amount = int(a["amount"])
        if not amount or a["owner"].lower() == npm:
            continue
        sinal = 1 if nome == "Mint" else -1
        lo, hi = int(a["tickLower"]), int(a["tickUpper"])
        net[lo] = net.get(lo, 0) + sinal * amount
        net[hi] = net.get(hi, 0) - sinal * amount
        donos[a["owner"].lower()] = donos.get(a["owner"].lower(), 0) + sinal * amount
    return {"net": {t: v for t, v in net.items() if v != 0}, "donos": donos}


def tokens_da_pool(pool: str, npm: str, *,
                   ate_bloco: int | None = None) -> dict[int, tuple[int, int]]:
    """tokenId -> (tickLower, tickUpper) das NFTs QUE SAO DESTA POOL.

    A versao anterior mapeava tx_hash -> ticks e aceitava toda NFT cuja
    transacao tivesse tocado a pool. Isso esta errado por dois motivos, e os
    dois acontecem neste indice:

      1. Uma tx pode ter varios Mint (39 aqui, 34 com faixas diferentes) —
         um dicionario por tx guarda so o ultimo e as demais herdam a faixa
         errada.
      2. Um lote pode cunhar posicoes de POOLS DIFERENTES na mesma tx. A
         `0x43078808...` cunha 11 NFTs e so UMA e desta pool. As outras 10
         entravam com faixa e liquidez desta pool.

    Efeito medido do bug: a soma da liquidez das posicoes "ativas" dava
    1.445.694% do campo `liquidity` do Swap, e o capital ativo aparecia como
    $219 bilhoes. **119 dos 1.514 IncreaseLiquidity sao de outras pools.**

    Pareamento correto: dentro da mesma tx, casar cada `Mint` da pool com o
    `IncreaseLiquidity` do NPM de mesmo `liquidity` e log_index seguinte —
    o NPM emite logo apos o Mint. Cada log e consumido uma unica vez.

    Na V3 um tokenId pertence a uma unica pool para sempre, entao o mapa
    resolvido aqui basta para filtrar todos os eventos posteriores da NFT.

    `ate_bloco` existe pelo mesmo motivo que em `reconstruir_posicoes`: sem ele,
    o conjunto de tokens vem do banco VIVO enquanto os eventos vem de um bloco
    fixo, e o `tail` faz os dois discordarem. Medido: o criterio 1 do
    check-HISTORY-1 acusou "1 token da pool sem lifecycle" — era uma NFT cunhada
    depois do bloco de corte.
    """
    with session_scope() as sess:
        q_mints = (select(PoolEvent.tx_hash, PoolEvent.log_index, PoolEvent.args)
                   .where(PoolEvent.pool_address == pool.lower(),
                          PoolEvent.event == "Mint")
                   .order_by(PoolEvent.block_number, PoolEvent.log_index))
        q_incs = (select(PoolEvent.tx_hash, PoolEvent.log_index, PoolEvent.args)
                  .where(PoolEvent.pool_address == npm.lower(),
                         PoolEvent.event == "IncreaseLiquidity")
                  .order_by(PoolEvent.block_number, PoolEvent.log_index))
        if ate_bloco is not None:
            q_mints = q_mints.where(PoolEvent.block_number <= ate_bloco)
            q_incs = q_incs.where(PoolEvent.block_number <= ate_bloco)
        mints = sess.execute(q_mints).all()
        incs = sess.execute(q_incs).all()

    por_tx: dict[str, list[tuple[int, int, int]]] = {}
    for tx, li, a in incs:
        por_tx.setdefault(tx, []).append((li, int(a["tokenId"]),
                                          int(a["liquidity"])))
    for v in por_tx.values():
        v.sort()

    saida: dict[int, tuple[int, int]] = {}
    consumidos: dict[str, set[int]] = {}
    for tx, li_mint, a in mints:
        amount = int(a["amount"])
        if amount == 0:
            continue
        ja = consumidos.setdefault(tx, set())
        for li_inc, token_id, liquidez in por_tx.get(tx, ()):
            if li_inc <= li_mint or li_inc in ja or liquidez != amount:
                continue
            ja.add(li_inc)
            saida.setdefault(token_id, (int(a["tickLower"]), int(a["tickUpper"])))
            break
    return saida


def reconstruir_posicoes(pool: str | None = None, *,
                         ate_bloco: int | None = None) -> dict[int, Posicao]:
    """Todas as posicoes NFT desta pool, reconstruidas dos eventos.

    `ate_bloco` trava a reconstrucao num bloco. Existe porque comparar posicoes
    de AGORA com um `slot0` gravado num swap ANTIGO e comparar dois instantes
    diferentes — com o `tail` rodando ha Mint/Burn depois do ultimo swap, e o
    check-4 acusou 0,065% de erro que era so isso.
    """
    cfg = settings()
    pool = (pool or cfg.pool_address).lower()
    npm = cfg.npm_address.lower()
    tokens = tokens_da_pool(pool, npm, ate_bloco=ate_bloco)

    with session_scope() as sess:
        stmt = (select(PoolEvent.event, PoolEvent.args, PoolEvent.block_number,
                       PoolEvent.ts, PoolEvent.tx_hash)
                .where(PoolEvent.pool_address == npm)
                .order_by(PoolEvent.block_number, PoolEvent.log_index))
        if ate_bloco is not None:
            stmt = stmt.where(PoolEvent.block_number <= ate_bloco)
        eventos = sess.execute(stmt).all()

    posicoes: dict[int, Posicao] = {}
    for nome, args, bloco, ts, tx in eventos:
        token_id = int(args["tokenId"])
        # So NFTs cujo Mint casou com um Mint DESTA pool, por liquidez.
        if token_id not in tokens:
            continue
        pos = posicoes.get(token_id)
        if pos is None:
            pos = Posicao(token_id=token_id)
            posicoes[token_id] = pos
        if pos.tick_lower is None:
            pos.tick_lower, pos.tick_upper = tokens[token_id]
        if pos.primeiro_ts is None:
            pos.primeiro_ts = ts
        pos.ultimo_ts = ts

        if nome == "IncreaseLiquidity":
            liq, a0, a1 = int(args["liquidity"]), int(args["amount0"]), int(args["amount1"])
            pos.liquidez += liq
            pos.depositado0 += a0
            pos.depositado1 += a1
            pos.movimentos.append(Movimento(bloco, ts, "increase", liq, a0, a1, tx))
        elif nome == "DecreaseLiquidity":
            liq, a0, a1 = int(args["liquidity"]), int(args["amount0"]), int(args["amount1"])
            pos.liquidez -= liq
            pos.sacado0 += a0
            pos.sacado1 += a1
            pos.movimentos.append(Movimento(bloco, ts, "decrease", liq, a0, a1, tx))
        elif nome == "NpmCollect":
            a0, a1 = int(args["amount0"]), int(args["amount1"])
            pos.coletado0 += a0
            pos.coletado1 += a1
            pos.movimentos.append(Movimento(bloco, ts, "collect", 0, a0, a1, tx))

    return posicoes


def resumo_posicao(pos: Posicao, sqrt_price_x96: int, tick_atual: int,
                   dec0: int, dec1: int, agora_ts: int | None = None) -> dict:
    """Estado da posicao em unidades humanas, pronto para relatorio."""
    a0, a1 = pos.amounts_agora(sqrt_price_x96)
    preco = price_to_usd(sqrt_price_x96_to_price(sqrt_price_x96, dec0, dec1))
    q0 = Decimal(a0) / Decimal(10 ** dec0)
    q1 = Decimal(a1) / Decimal(10 ** dec1)
    faixa = pos.faixa_usd(dec0, dec1)

    fee0 = Decimal(pos.fees_coletadas0) / Decimal(10 ** dec0)
    fee1 = Decimal(pos.fees_coletadas1) / Decimal(10 ** dec1)

    return {
        "token_id": pos.token_id,
        "ativa": pos.ativa,
        "liquidez": pos.liquidez,
        "ticks": [pos.tick_lower, pos.tick_upper],
        "faixa_usd": [float(faixa[0]), float(faixa[1])] if faixa else None,
        "em_range": pos.em_range(tick_atual),
        "preco_usd": float(preco),
        "amount0": float(q0),
        "amount1": float(q1),
        "valor_usd": float(q0 + q1 * preco),
        "fees_coletadas0": float(fee0),
        "fees_coletadas1": float(fee1),
        "fees_coletadas_usd": float(fee0 + fee1 * preco),
        "depositado0": float(Decimal(pos.depositado0) / Decimal(10 ** dec0)),
        "depositado1": float(Decimal(pos.depositado1) / Decimal(10 ** dec1)),
        "idade_dias": pos.idade_dias(agora_ts),
        "janela_ativa_dias": pos.janela_ativa_dias,
        "movimentos": len(pos.movimentos),
    }



def fracao_em_range(pool: str, ts_inicio: int, ts_fim: int,
                    tick_lower: int, tick_upper: int,
                    *, limite_swaps: int = 200_000) -> dict | None:
    """Quanto do tempo a posicao passou DENTRO da faixa, desde que abriu.

    Diferente de `horas_fora_de_range`, que mede o episodio CORRENTE: esta
    responde "de tudo que ja passou, quanto foi dentro" — que e o numero que
    diz se a faixa foi bem escolhida, e nao se ela esta em apuros agora.

    **Pondera por TEMPO, nao por contagem de swaps.** O volume desta chain
    oscila 18x dentro do mesmo regime, e uma hora movimentada tem dezenas de
    swaps contra um de uma hora parada. Contar swaps daria peso a atividade e
    nao a duracao — e a fee que voce perde fora do range e proporcional ao
    tempo, nao ao numero de negocios.

    Cada swap vale ate o proximo: o tick dele descreve o preco no intervalo
    que se abre ali. O trecho entre a abertura da posicao e o primeiro swap
    herda o tick desse primeiro — e uma aproximacao, declarada em
    `aproximado_ate_o_primeiro_swap`, e ela vale no maximo um intervalo entre
    negocios.
    """
    from sqlalchemy import select

    from ..db.models import PoolEvent
    from ..db.session import session_scope

    if ts_fim <= ts_inicio:
        return None
    with session_scope() as sess:
        linhas = sess.execute(
            select(PoolEvent.ts, PoolEvent.args)
            .where(PoolEvent.pool_address == pool.lower(),
                   PoolEvent.event == "Swap",
                   PoolEvent.ts >= ts_inicio, PoolEvent.ts <= ts_fim)
            .order_by(PoolEvent.block_number, PoolEvent.log_index)
            .limit(limite_swaps)).all()
    if not linhas:
        return None

    dentro = 0.0
    anterior_ts = ts_inicio
    anterior_dentro = tick_lower <= int(linhas[0][1]["tick"]) < tick_upper
    for t, a in linhas:
        if anterior_dentro:
            dentro += max(0, t - anterior_ts)
        anterior_ts = t
        anterior_dentro = tick_lower <= int(a["tick"]) < tick_upper
    if anterior_dentro:
        dentro += max(0, ts_fim - anterior_ts)

    total = ts_fim - ts_inicio
    return {"fracao": dentro / total, "horas_dentro": dentro / 3600,
            "horas_total": total / 3600, "swaps": len(linhas),
            "aproximado_ate_o_primeiro_swap": linhas[0][0] > ts_inicio}


def horas_fora_de_range(pool: str, ts: int, tick_lower: int, tick_upper: int,
                        *, limite_swaps: int = 5000) -> float:
    """Ha quantas horas o preco esta CONTINUAMENTE fora de [lower, upper).

    Olha so o passado (`ts <= ts`), entao serve tanto para o agora quanto para
    replay historico sem lookahead.

    Existe aqui, e nao no check, porque o G2 depende dela para decidir. Ate
    2026-08-21 so `checks_3c` calculava isso; `report/build.py` montava o
    `Contexto` sem o campo e o G2 rodava com 0,0h em producao — bloqueando
    rerange indefinidamente enquanto a saida fosse rasa. Medido na posição POS_F:
    6,49h reais contra 0,0h vistos pelo gate.

    Zero significa "esta dentro do range", nao "acabou de sair".
    """
    from sqlalchemy import select

    from ..db.models import PoolEvent
    from ..db.session import session_scope

    with session_scope() as sess:
        linhas = sess.execute(
            select(PoolEvent.ts, PoolEvent.args)
            .where(PoolEvent.pool_address == pool.lower(),
                   PoolEvent.event == "Swap", PoolEvent.ts <= ts)
            .order_by(PoolEvent.block_number.desc(),
                      PoolEvent.log_index.desc()).limit(limite_swaps)).all()
    fora_desde = None
    for t, a in linhas:
        if tick_lower <= int(a["tick"]) < tick_upper:
            break
        fora_desde = t
    return (ts - fora_desde) / 3600 if fora_desde is not None else 0.0


__all__ = ["Posicao", "Movimento", "reconstruir_posicoes", "resumo_posicao",
           "tokens_da_pool", "defasagem_npm", "liquidez_fora_do_npm",
           "horas_fora_de_range", "fracao_em_range"]
