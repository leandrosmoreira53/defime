"""Distribuicao de liquidez por tick, reconstruida dos eventos.

Ideia central da V3 do plano (F2): Mint e Burn da pool carregam tickLower,
tickUpper e amount. Somando os deltas por tick ao longo do historico, obtem-se
`liquidityNet` de cada tick inicializado — e dai a liquidez ativa em qualquer
preco, **em qualquer bloco do passado**, sem snapshot e sem no archive.

Prova de que a reconstrucao esta certa (criterio 4 do check-2):

    soma de liquidityNet dos ticks <= tick_atual  ==  slot0.liquidity

Se um unico Mint ou Burn faltar no indice, ou se o sinal estiver invertido em
algum lugar, essa igualdade nao fecha.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass

from sqlalchemy import select

from ..db.models import PoolEvent
from ..db.session import session_scope
from .v3math import tick_to_usd


@dataclass
class TickBook:
    """liquidityNet por tick, mais a liquidez ativa no tick corrente."""

    net: dict[int, int]
    block: int | None = None

    @property
    def ticks(self) -> list[int]:
        return sorted(self.net)

    def active_liquidity_at(self, tick: int) -> int:
        """Soma dos deltas de todos os ticks <= tick.

        E a definicao de liquidez ativa na V3: cruzar um tick para cima soma
        seu liquidityNet, para baixo subtrai.
        """
        total = 0
        for t in self.ticks:
            if t > tick:
                break
            total += self.net[t]
        return total

    def cumulative(self) -> list[tuple[int, int]]:
        """[(tick, liquidez ativa a partir dele)] — a curva do grafico da Revert."""
        out, running = [], 0
        for t in self.ticks:
            running += self.net[t]
            out.append((t, running))
        return out

    def integridade(self) -> dict:
        """O livro esta completo? Duas perguntas que so o proprio dado responde.

        **Nasceu de um buraco real.** Em 2026-09-01 a MU/USDG foi indexada a
        partir do bloco 30.480.683 quando a pool nasceu em 20.656.675 — 9,8M de
        blocos a menos. O resultado foi um livro com `Burn` sem o `Mint` que o
        originou, curva cumulativa **negativa** em 20 de 66 segmentos, e um
        grafico de liquidez com dois espetos e um vale vazio.

        Nada acusou, e o motivo importa: `backfill_ranges` registrava
        corretamente que a faixa PEDIDA tinha fechado. O log nao mentiu —
        ninguem perguntou pelo resto. Verificar "o backfill rodou" e verificar
        o que foi pedido, nao o que falta; e a mesma tautologia do H4 deste
        projeto ("os 317 transfers estao 317/317 em tx com evento da pool").

        As duas invariantes abaixo sao do **dado**, nao do coletor, e por isso
        pegam o buraco sem depender de ninguem lembrar de nada:

        1. **A liquidez cumulativa nunca e negativa.** Ela e a soma do que esta
           depositado em cada preco. Negativo significa Burn sem Mint.
        2. **A soma de todo `liquidityNet` fecha em ZERO.** Todo Mint soma em
           `lower` e subtrai em `upper`; acima do ultimo tick nao ha liquidez
           ativa. Resto diferente de zero significa que faltam pernas.

        A segunda e mais forte que a primeira: um buraco pode, por azar, deixar
        a curva sempre positiva e ainda assim nao fechar no fim.
        """
        curva = self.cumulative()
        pior = min((l for _, l in curva), default=0)
        resto = curva[-1][1] if curva else 0
        negativos = sum(1 for _, l in curva if l < 0)
        return {
            "ticks": len(self.net),
            "segmentos_negativos": negativos,
            "pior_liquidez": str(pior),
            "resto_no_topo": str(resto),
            "integro": negativos == 0 and resto == 0,
            "motivo": None if (negativos == 0 and resto == 0) else (
                "livro incompleto: %d segmento(s) com liquidez negativa e resto "
                "%s no topo — faltam Mint/Burn no indice, provavelmente porque o "
                "backfill nao alcanca o bloco de criacao da pool"
                % (negativos, resto)),
        }

    def liquidity_profile(self, dec0: int, dec1: int,
                          tick_low: int | None = None,
                          tick_high: int | None = None) -> list[dict]:
        """Faixas [tick_i, tick_{i+1}) com a liquidez ativa e o preco em USD."""
        curva = self.cumulative()
        perfil = []
        for i, (t, liq) in enumerate(curva):
            prox = curva[i + 1][0] if i + 1 < len(curva) else None
            if tick_low is not None and (prox is not None and prox <= tick_low):
                continue
            if tick_high is not None and t >= tick_high:
                continue
            perfil.append({
                "tick_de": t,
                "tick_ate": prox,
                # preco inverte a ordem: tick maior = USD menor nesta pool
                "usd_de": float(tick_to_usd(prox, dec0, dec1)) if prox is not None else None,
                "usd_ate": float(tick_to_usd(t, dec0, dec1)),
                "liquidez": liq,
            })
        return perfil

    def center_of_mass(self, dec0: int, dec1: int) -> float | None:
        """Centro de massa da liquidez em USD — base do Liquidity Migration Score."""
        num = den = 0.0
        for faixa in self.liquidity_profile(dec0, dec1):
            if faixa["usd_de"] is None or faixa["liquidez"] <= 0:
                continue
            meio = (faixa["usd_de"] + faixa["usd_ate"]) / 2
            peso = float(faixa["liquidez"])
            num += meio * peso
            den += peso
        return num / den if den else None


def build_book(pool: str, upto_block: int | None = None) -> TickBook:
    """Reconstroi o livro somando Mint (+) e Burn (-) ate `upto_block`.

    Convencao da V3: um Mint em [lower, upper) soma `amount` ao liquidityNet de
    `lower` e subtrai de `upper`. Burn faz o inverso.
    """
    stmt = (select(PoolEvent.event, PoolEvent.args, PoolEvent.block_number)
            .where(PoolEvent.pool_address == pool.lower(),
                   PoolEvent.event.in_(("Mint", "Burn")))
            .order_by(PoolEvent.block_number, PoolEvent.log_index))
    if upto_block is not None:
        stmt = stmt.where(PoolEvent.block_number <= upto_block)

    net: dict[int, int] = {}
    ultimo = None
    with session_scope() as sess:
        for evento, args, bloco in sess.execute(stmt):
            amount = int(args["amount"])
            if amount == 0:
                continue
            sinal = 1 if evento == "Mint" else -1
            lower, upper = int(args["tickLower"]), int(args["tickUpper"])
            net[lower] = net.get(lower, 0) + sinal * amount
            net[upper] = net.get(upper, 0) - sinal * amount
            ultimo = bloco

    # ticks que zeraram nao sao mais ticks inicializados
    net = {t: v for t, v in net.items() if v != 0}
    return TickBook(net=net, block=upto_block if upto_block is not None else ultimo)


def verify_against_slot0(pool: str, tick_atual: int, liquidez_onchain: int,
                         upto_block: int | None = None) -> dict:
    """Criterio 4 do check-2: a soma reconstruida bate com slot0.liquidity?"""
    book = build_book(pool, upto_block)
    calc = book.active_liquidity_at(tick_atual)
    erro = abs(calc - liquidez_onchain) / liquidez_onchain if liquidez_onchain else None
    return {
        "tick_atual": tick_atual,
        "liquidez_calculada": calc,
        "liquidez_onchain": liquidez_onchain,
        "diferenca": calc - liquidez_onchain,
        "erro_relativo": erro,
        "ticks_inicializados": len(book.net),
        "bloco": book.block,
    }


def verify_against_swaps(pool: str, amostras: int = 200) -> dict:
    """Validacao forte da reconstrucao — e 100% offline.

    Todo evento Swap carrega `liquidity` e `tick` do instante em que ocorreu.
    Entao, para qualquer swap do historico:

        livro_reconstruido(ate aquele ponto).liquidez_ativa(swap.tick) == swap.liquidity

    Melhor que comparar com slot0 por tres motivos: nao precisa de no archive,
    nao sofre com deriva de bloco entre a leitura e o banco, e valida centenas
    de pontos ao longo de toda a historia em vez de um unico agora.
    """
    pool = pool.lower()
    with session_scope() as sess:
        mint_burn = sess.execute(
            select(PoolEvent.block_number, PoolEvent.log_index,
                   PoolEvent.event, PoolEvent.args)
            .where(PoolEvent.pool_address == pool,
                   PoolEvent.event.in_(("Mint", "Burn")))
            .order_by(PoolEvent.block_number, PoolEvent.log_index)).all()
        swaps = sess.execute(
            select(PoolEvent.block_number, PoolEvent.log_index, PoolEvent.args)
            .where(PoolEvent.pool_address == pool, PoolEvent.event == "Swap")
            .order_by(PoolEvent.block_number, PoolEvent.log_index)).all()

    if not swaps:
        return {"erro": "sem swaps indexados"}

    passo = max(1, len(swaps) // amostras)
    alvos = swaps[::passo]

    chaves = [(b, li) for b, li, _, _ in mint_burn]
    net: dict[int, int] = {}
    cursor = 0
    resultados, falhas = [], []

    for bloco, log_index, args in alvos:
        # aplica todo Mint/Burn anterior a este swap, em ordem
        while cursor < len(chaves) and chaves[cursor] < (bloco, log_index):
            _, _, evento, a = mint_burn[cursor]
            amount = int(a["amount"])
            if amount:
                sinal = 1 if evento == "Mint" else -1
                lo, hi = int(a["tickLower"]), int(a["tickUpper"])
                net[lo] = net.get(lo, 0) + sinal * amount
                net[hi] = net.get(hi, 0) - sinal * amount
            cursor += 1

        tick = int(args["tick"])
        esperado = int(args["liquidity"])
        calc = sum(v for t, v in net.items() if t <= tick)
        ok = calc == esperado
        item = {"bloco": bloco, "tick": tick, "calculado": calc,
                "no_evento": esperado, "exato": ok}
        resultados.append(item)
        if not ok:
            falhas.append(item)

    exatos = sum(1 for r in resultados if r["exato"])
    return {
        "amostras": len(resultados),
        "exatos": exatos,
        "fracao_exata": exatos / len(resultados),
        "falhas": falhas[:5],
        "primeiro_bloco": resultados[0]["bloco"],
        "ultimo_bloco": resultados[-1]["bloco"],
    }


__all__ = ["TickBook", "build_book", "verify_against_slot0", "verify_against_swaps"]
