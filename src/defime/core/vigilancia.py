"""Quanto da fee do tick e sua, e quanto seria se os vizinhos sairem.

A fee que uma LP recebe e `L_sua / L_ativa_no_tick` — e `L_ativa` e so o que
esta **no tick corrente**, nao o TVL. Uma faixa estreita concentra liquidez
enorme num pedaco pequeno de preco: enquanto o preco fica ali, ela leva quase
tudo; assim que sai, some do denominador inteiro e quem restou herda a fee.

**Medido na MU/USDG 0,30% em 2026-09-01, e o numero e o motivo deste modulo
existir:** oito posicoes com largura abaixo de 5% detinham 99,65% da liquidez
do tick. A nossa, de 21,9%, ficava com **0,35%**. Se aquelas oito saissem, a
nossa fatia iria a **53,02%** — cento e cinquenta e uma vezes.

E elas saem. Medido nas mesmas posicoes, sobre 8.858 swaps de 25 dias:

    tempo fora do range     48% a 67% da vida de cada uma
    episodios fora          mediana 13 min, p75 3,4h, p90 36h
    acima de 1h             27,3% dos episodios
    acima de 4h             21,2%

A distribuicao e BIMODAL: ou remontam em minutos, ou ficam horas. E a cauda
que paga — e ela e um quinto dos casos.

---

**Tudo aqui e puro.** Recebe posicoes e tick, devolve numeros. Quem le a chain
e o chamador.

**Nao ha recomendacao neste modulo.** Ele mede uma assimetria que existe; se
ela justifica abrir, fechar ou esperar e decisao do motor, e o motor cobra
custo de rerange e divergencia que nada aqui enxerga.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Abaixo disto uma faixa e "estreita" para efeito de vigilancia: ela vive perto
# do preco e sai com frequencia. Nao e limiar de qualidade — e o ponto em que a
# posicao deixa de ser um vizinho estavel e vira um que entra e sai.
LARGURA_ESTREITA_PCT = 5.0


@dataclass(frozen=True)
class Vizinha:
    """Uma posicao de outra carteira, do ponto de vista de quem divide a fee."""

    token_id: int
    liquidez: int
    tick_lower: int
    tick_upper: int
    capital_usd: float
    largura_pct: float
    em_range: bool
    dono: str | None = None

    @property
    def estreita(self) -> bool:
        return self.largura_pct < LARGURA_ESTREITA_PCT


@dataclass
class Cenario:
    """O que acontece com a sua fatia se um conjunto de vizinhas sair."""

    rotulo: str
    saem: int
    capital_que_sai: float
    fatia_depois: float
    multiplicador: float


@dataclass
class Vigilancia:
    nossa: Vizinha | None
    todas: list[Vizinha]
    tick: int
    liquidez_no_tick: int
    fatia_atual: float | None
    cenarios: list[Cenario] = field(default_factory=list)
    # Quanto da liquidez REAL do tick o indice de posicoes conhece. `None`
    # quando ninguem passou a verdade para conferir.
    cobertura: float | None = None
    motivo_sem_fatia: str | None = None

    @property
    def concentracao_estreita(self) -> float:
        """Fracao da liquidez do tick que esta em faixas estreitas.

        Perto de 1 significa que a fee do tick e refem de posicoes que saem —
        e o sinal de que a assimetria existe.
        """
        if not self.liquidez_no_tick:
            return 0.0
        return sum(v.liquidez for v in self.todas
                   if v.em_range and v.estreita) / self.liquidez_no_tick

    def resumo(self) -> dict:
        return {
            "tick": self.tick,
            "liquidez_no_tick": str(self.liquidez_no_tick),
            "nossa_fatia": self.fatia_atual,
            "cobertura_do_indice": self.cobertura,
            "motivo_sem_fatia": self.motivo_sem_fatia,
            "vizinhas_em_range": sum(1 for v in self.todas if v.em_range),
            "concentracao_estreita": self.concentracao_estreita,
            "cenarios": [{"rotulo": c.rotulo, "saem": c.saem,
                          "capital_que_sai": c.capital_que_sai,
                          "fatia_depois": c.fatia_depois,
                          "multiplicador": c.multiplicador}
                         for c in self.cenarios],
        }


# O indice de NFT pode ficar para tras em horas — e documentado que isso
# acontece sozinho. Fora desta banda a fatia NAO e publicada.
COBERTURA_MINIMA = 0.98
COBERTURA_MAXIMA = 1.02


def montar(vizinhas: list[Vizinha], tick: int,
           nosso_token: int | None = None,
           liquidez_da_chain: int | None = None) -> Vigilancia:
    """A foto: quem esta no tick, quanto e seu, e os cenarios de saida.

    `liquidez_no_tick` soma **so quem esta em range** — e a definicao do
    denominador da fee na V3. Somar as fora daria um numero maior e um
    resultado menor, que e o erro de 5,6x que a Fase 5 deste projeto pagou ao
    usar `L_pool` no lugar de `L_pool + L_nossa`.
    """
    ativas = [v for v in vizinhas if v.em_range and v.liquidez > 0]
    total = sum(v.liquidez for v in ativas)
    nossa = next((v for v in vizinhas if v.token_id == nosso_token), None)
    fatia = (nossa.liquidez / total) if (nossa and nossa.em_range and total) else 0.0

    # **A fatia so sai se o indice conhecer a liquidez que a CHAIN tem.**
    #
    # Medido em 2026-09-04 na RBLX: o `npm_link` estava 47.807 blocos atras,
    # com 26 Mint de NFT sem par, e a soma das posicoes conhecidas dava 241e15
    # contra 8.886e15 de `liquidity()`. A pagina publicou a fatia do usuário
    # como **13,60%** quando ela era **0,37%** — erro de 37x, e para MAIS, que
    # e a direcao que anima em vez de alarmar.
    #
    # A guarda compara com a VERDADE (`liquidity()` do contrato) e nao com uma
    # heuristica de atraso: indice adiantado, atrasado ou com buraco antigo
    # cai na mesma rede. `defasagem_npm()` ja existia e dizia `em_dia: False`
    # — ninguem a chamava aqui. Regra escrita que o codigo nao cumpria, de
    # novo.
    cobertura = motivo = None
    if liquidez_da_chain and liquidez_da_chain > 0:
        cobertura = total / liquidez_da_chain
        if not COBERTURA_MINIMA <= cobertura <= COBERTURA_MAXIMA:
            motivo = (f"indice conhece {100 * cobertura:.1f}% da liquidez do "
                      f"tick — fatia nao medida")
            return Vigilancia(nossa=nossa, todas=vizinhas, tick=tick,
                              liquidez_no_tick=liquidez_da_chain,
                              fatia_atual=None, cenarios=[],
                              cobertura=cobertura, motivo_sem_fatia=motivo)

    cenarios: list[Cenario] = []
    if nossa and nossa.em_range and total:
        grupos = [
            ("as estreitas (< %.0f%%)" % LARGURA_ESTREITA_PCT,
             [v for v in ativas if v.estreita and v.token_id != nosso_token]),
            ("a maior de todas",
             sorted([v for v in ativas if v.token_id != nosso_token],
                    key=lambda v: -v.liquidez)[:1]),
            ("as tres maiores",
             sorted([v for v in ativas if v.token_id != nosso_token],
                    key=lambda v: -v.liquidez)[:3]),
        ]
        for rotulo, saindo in grupos:
            if not saindo:
                continue
            depois = total - sum(v.liquidez for v in saindo)
            if depois <= 0:
                continue
            f = nossa.liquidez / depois
            cenarios.append(Cenario(
                rotulo=rotulo, saem=len(saindo),
                capital_que_sai=sum(v.capital_usd for v in saindo),
                fatia_depois=f,
                multiplicador=f / fatia if fatia else float("inf")))
    return Vigilancia(nossa=nossa, todas=vizinhas, tick=tick,
                      liquidez_no_tick=total, fatia_atual=fatia,
                      cobertura=cobertura, motivo_sem_fatia=motivo,
                      cenarios=cenarios)



# ------------------------------------------------------------------- score
# Peso de cada ingrediente no score de oportunidade. Nao sao ajustados a um
# resultado: sao a forma mais simples de dizer "os dois importam e a
# concentracao importa mais", e mexer neles depois de ver o backtest seria
# ajustar o modelo ate passar — o que o plano do projeto proibe.
PESO_CONCENTRACAO = 0.6
PESO_BORDA = 0.4

# Acima disto a borda ja nao e "perto": a posicao tem espaco e nao vai sair
# tao cedo. 300 ticks sao ~3% de preco.
TICKS_PERTO = 300


def score_oportunidade(v: Vigilancia) -> dict:
    """Quao perto esta a janela em que a fee do tick fica sua.

    ****************************************************************
    * REPROVADO NO BACKTEST DE 2026-09-01. NAO USAR PARA DECIDIR.   *
    ****************************************************************

    A ideia era forward-looking: concentracao estreita alta + vizinhos na
    beirada => a janela vai abrir, posicione-se antes. Testado contra a fee
    REALIZADA nas 24h seguintes, com anti-lookahead (o score de `t` so olha
    posicoes vivas em `t`; a fee vem dos swaps depois de `t`):

        ativo   n     corr(score, fee)  corr(conc)  corr(borda)
        MU     137          -0,248        -0,140      -0,334
        USO    266          +0,155        +0,537      +0,093

    **Os dois discordam de SINAL.** Na MU o score alto preve fee MENOR: os
    quartis caem de $36,30 para $9,41. Na USO sobem de $5,37 para $708,19. Um
    sinal que inverte de direcao entre dois ativos nao e sinal.

    **Por que falha, e o mecanismo importa mais que o numero.** Quando as
    estreitas estao na beirada, o preco esta exatamente onde elas concentraram
    — ou seja, onde ha MUITA liquidez agora, e a sua fatia esta no minimo. O
    score marca o instante de menor fee, nao de maior. O "elas vao sair em
    breve" nao chega dentro de 24h com frequencia suficiente: a mediana do
    episodio fora e 13 minutos e as janelas longas (p90 = 36h) sao raras
    demais para dominar a media.

    **E a correlacao de +0,537 na USO nao e descoberta, e confusao.**
    Concentracao alta e fee alta sao as duas consequencias do mesmo terceiro
    fator, o volume: pool movimentada atrai liquidez concentrada E paga mais
    fee. Se eu tivesse testado so na USO teria publicado isso como sinal.

    **Fica no codigo, reprovado e documentado, para nao ser reinventado.** O
    que continua valendo e a parte DESCRITIVA deste modulo — `montar()`,
    `distancia_da_borda()`, os cenarios — que mede o que esta acontecendo
    agora em vez de prever.

    Dois ingredientes, os dois necessarios:

      `concentracao`  quanto da liquidez do tick esta em faixa estreita. Alta
                      significa que a fee de agora e refem de posicoes que saem.
      `borda`         quao perto essas posicoes estao de sair. Concentracao
                      alta com todo mundo no meio da faixa nao promete nada:
                      elas podem ficar ali por dias.

    O sinal e FORWARD-LOOKING de proposito. "Os grandes sairam" nao serve para
    decidir: quando se ve isso o movimento ja aconteceu e entrar e comprar o
    topo. O que decide e "eles ainda estao dentro, e na beirada".
    """
    if not v.liquidez_no_tick:
        return {"score": None, "motivo": "sem liquidez no tick"}
    estreitas = [x for x in v.todas if x.em_range and x.estreita]
    if not estreitas:
        return {"score": 0.0, "concentracao": 0.0, "borda": 0.0,
                "motivo": "nenhuma faixa estreita em range"}

    conc = v.concentracao_estreita
    # Media da proximidade PONDERADA POR LIQUIDEZ: uma posicao minuscula na
    # beirada nao move a fee de ninguem, e sem o peso ela contaria igual a de
    # $120 mil.
    peso_total = sum(x.liquidez for x in estreitas)
    prox = 0.0
    for x in estreitas:
        d = distancia_da_borda(x, v.tick)
        p = max(0.0, 1.0 - abs(d["ticks"]) / TICKS_PERTO)
        prox += p * x.liquidez
    borda = prox / peso_total if peso_total else 0.0
    return {"score": PESO_CONCENTRACAO * conc + PESO_BORDA * borda,
            "concentracao": conc, "borda": borda,
            "estreitas_em_range": len(estreitas),
            "capital_estreito": sum(x.capital_usd for x in estreitas),
            "motivo": None}


def distancia_da_borda(v: Vizinha, tick: int) -> dict:
    """Quantos ticks faltam para esta posicao sair, e para que lado.

    Em TICKS e nao em dolar: a distancia em dolar depende do nivel do preco e
    nao compara entre pools. O sinal diz o lado — negativo e a borda de baixo.
    """
    if not v.em_range:
        return {"em_range": False, "ticks": 0, "lado": None}
    ate_cima = v.tick_upper - tick
    ate_baixo = tick - v.tick_lower
    if ate_cima <= ate_baixo:
        return {"em_range": True, "ticks": ate_cima, "lado": "cima",
                "pct": 100 * (1.0001 ** ate_cima - 1)}
    return {"em_range": True, "ticks": ate_baixo, "lado": "baixo",
            "pct": -100 * (1 - 1.0001 ** -ate_baixo)}


__all__ = ["Vizinha", "Cenario", "Vigilancia", "montar", "distancia_da_borda",
           "score_oportunidade", "PESO_CONCENTRACAO", "PESO_BORDA", "TICKS_PERTO",
           "LARGURA_ESTREITA_PCT"]
