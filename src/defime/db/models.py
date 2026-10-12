"""Modelos do banco — Camada 1 (coleta).

Principios do documento de regras do projeto aplicados aqui:
  - append-only e idempotente: PK natural (block, log_index) por pool
  - toda tabela tem pool_address
  - timestamps em UTC, guardados como epoch int (sem ambiguidade de tz)
  - valores de 256 bits guardados como TEXT: int do Python e ilimitado, o do
    banco nao. Converter para int so na Camada 2.
"""

from __future__ import annotations

from sqlalchemy import (BigInteger, Boolean, Float, Index, Integer, JSON,
                        PrimaryKeyConstraint, String, Text)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Block(Base):
    """Cabecalho minimo. Existe para detectar reorg por divergencia de hash."""

    __tablename__ = "blocks"

    number: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    hash: Mapped[str] = mapped_column(String(66), nullable=False)
    parent_hash: Mapped[str] = mapped_column(String(66), nullable=False)
    ts: Mapped[int] = mapped_column(BigInteger, nullable=False)  # epoch UTC

    __table_args__ = (Index("ix_blocks_ts", "ts"),)


class PoolEvent(Base):
    """Log cru da pool. Fonte de verdade de tudo que vem depois.

    Nao interpreta nada: guarda o evento decodificado e segue. Quem entende de
    posicao, fee e liquidez e a Camada 2.
    """

    __tablename__ = "pool_events"

    pool_address: Mapped[str] = mapped_column(String(42), nullable=False)
    block_number: Mapped[int] = mapped_column(BigInteger, nullable=False)
    log_index: Mapped[int] = mapped_column(Integer, nullable=False)

    tx_hash: Mapped[str] = mapped_column(String(66), nullable=False)
    tx_index: Mapped[int] = mapped_column(Integer, nullable=False)
    block_hash: Mapped[str] = mapped_column(String(66), nullable=False)
    ts: Mapped[int] = mapped_column(BigInteger, nullable=False)

    event: Mapped[str] = mapped_column(String(32), nullable=False)
    args: Mapped[dict] = mapped_column(JSON, nullable=False)

    __table_args__ = (
        # Idempotencia: rodar o backfill duas vezes nao duplica nada.
        PrimaryKeyConstraint("pool_address", "block_number", "log_index"),
        Index("ix_pool_events_event_block", "pool_address", "event", "block_number"),
        Index("ix_pool_events_ts", "pool_address", "ts"),
    )


class IndexerState(Base):
    """Ponto de retomada por stream. Permite matar e reiniciar sem perder nada."""

    __tablename__ = "indexer_state"

    stream: Mapped[str] = mapped_column(String(64), primary_key=True)
    pool_address: Mapped[str] = mapped_column(String(42), nullable=False)
    last_block: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    updated_at: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")


class BackfillRange(Base):
    """Faixas JA confirmadas como completas.

    Sem isto, "tem buraco?" viraria adivinhacao. Com isto, a cobertura e um
    fato registrado e o check-1 vira uma query, nao uma inspecao.
    """

    __tablename__ = "backfill_ranges"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    pool_address: Mapped[str] = mapped_column(String(42), nullable=False)
    from_block: Mapped[int] = mapped_column(BigInteger, nullable=False)
    to_block: Mapped[int] = mapped_column(BigInteger, nullable=False)
    logs: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completed_at: Mapped[int] = mapped_column(BigInteger, nullable=False)

    __table_args__ = (
        Index("ix_backfill_pool_from", "pool_address", "from_block", "to_block"),
    )


class UsoQuote(Base):
    """Referencia de preco do USO. Fora do pregao a NYSE nao publica nada —
    por isso `source` e `stale` sao colunas, nao detalhes."""

    __tablename__ = "uso_quotes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[int] = mapped_column(BigInteger, nullable=False)
    bid: Mapped[float | None] = mapped_column(Float, nullable=True)
    ask: Mapped[float | None] = mapped_column(Float, nullable=True)
    mid: Mapped[float | None] = mapped_column(Float, nullable=True)
    volume: Mapped[float | None] = mapped_column(Float, nullable=True)
    halted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    multiplier: Mapped[float | None] = mapped_column(Float, nullable=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    # stale: o provedor republicou o mesmo boletim (generatedAt igual).
    # preco_novo: o PRECO mudou. Sao coisas diferentes — medido em 2026-08-16,
    # 877 cotacoes tinham 875 generatedAt distintos e UM unico preco.
    stale: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    preco_novo: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    raw: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    __table_args__ = (Index("ix_uso_quotes_ts", "ts"),)


class PosicaoSnapshot(Base):
    """Estado economico de UMA posicao no tempo. A tabela que faltava.

    **Por que ela e fundamental e as outras duas nao sao:** preco e divergencia
    se reconstroem dos eventos a qualquer momento; **fee pendente nao**. Saber
    quanto ela era as 4h de 25/08 exigiria o `feeGrowthOutside` de cada tick
    naquele bloco — o problema que a Fase 4 declarou caro e que
    `pending_fees.py` contornou lendo o estado ATUAL do contrato.

    Medido em 2026-08-29: a serie de divergencia da POS_C teve a fee
    **interpolada em 4 dos 9 dias**, porque so havia 6 medicoes em 8,7 dias. O
    pico de $14.005,54 em 25/08 carrega fee estimada, nao medida.

    Mesmo argumento que criou `macro_snapshots`: noticia nao se reconstroi
    retroativamente. Aqui, fee pendente tambem nao.

    PK `(pool_address, token_id, ts)` — medicao datada, nunca sobrescreve.
    """

    __tablename__ = "posicao_snapshots"

    pool_address: Mapped[str] = mapped_column(String(66), primary_key=True)
    token_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    ts: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    bloco: Mapped[int] = mapped_column(BigInteger)
    preco_usd: Mapped[float] = mapped_column(Float)
    tick_atual: Mapped[int] = mapped_column(Integer)
    em_range: Mapped[bool] = mapped_column(Boolean, default=True)

    liquidez: Mapped[int] = mapped_column(BigInteger, default=0)
    amount0: Mapped[float] = mapped_column(Float, default=0.0)
    amount1: Mapped[float] = mapped_column(Float, default=0.0)

    valor_lp_usd: Mapped[float] = mapped_column(Float, default=0.0)
    valor_hodl_usd: Mapped[float] = mapped_column(Float, default=0.0)
    divergencia_usd: Mapped[float] = mapped_column(Float, default=0.0)

    # A unica coluna que NAO se reconstroi. Se vier None, ficou sem medicao —
    # e `None` nunca deve virar 0,0: seria afirmar que a fee era zero.
    fee_pendente_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    fee_realizada_usd: Mapped[float] = mapped_column(Float, default=0.0)
    pnl_total_usd: Mapped[float | None] = mapped_column(Float, nullable=True)

    posicao_na_faixa: Mapped[float | None] = mapped_column(Float, nullable=True)
    distancia_borda_sigmas: Mapped[float | None] = mapped_column(Float,
                                                                nullable=True)
    preco_medio_usd: Mapped[float | None] = mapped_column(Float, nullable=True)


class Decisao(Base):
    """Cada decisao do motor, com o vetor de features que a gerou.

    O inviolavel 7 do documento de regras do projeto diz: *"Toda decisao persiste o vetor de features
    que a gerou. Sem isso nao ha replay."* Ate 2026-08-29 isso vivia em 45
    arquivos JSON em `relatorios/` — o que atende a letra e nao a intencao:
    arquivo nao responde "quantas vezes o motor disse RERANGE em 30 dias?", nem
    "o G4 bloqueia mais no fim de semana?".

    Diferente de `posicao_snapshots`, o conteudo daqui E reconstruivel: os
    eventos estao no banco e o motor e deterministico. A tabela existe por
    auditabilidade e por consulta, nao por perda de dado.
    """

    __tablename__ = "decisoes"

    pool_address: Mapped[str] = mapped_column(String(66), primary_key=True)
    ts: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    token_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    acao: Mapped[str] = mapped_column(String(16))
    motivo: Mapped[str] = mapped_column(String(400), default="")
    gates_bloqueantes: Mapped[dict] = mapped_column(JSON, default=list)
    gates: Mapped[dict] = mapped_column(JSON, default=list)

    vantagem_mediana_usd: Mapped[float | None] = mapped_column(Float,
                                                              nullable=True)
    vantagem_p25_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    alvo_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    custo_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    simulacoes: Mapped[int] = mapped_column(Integer, default=0)

    preco_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    sigma_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    fator_macro: Mapped[float | None] = mapped_column(Float, nullable=True)

    # O vetor de features exigido pelo inviolavel 7.
    features: Mapped[dict] = mapped_column(JSON, default=dict)


class ColetorHeartbeat(Base):
    """Um batimento por ciclo de coletor. Existe porque eles morrem calados.

    Medido em 2026-08-27/29: os coletores caíram **tres vezes na mesma semana**
    e nenhuma delas foi registrada. Descobriu-se por acaso, notando preco velho
    — 23h de atraso, depois 17,7h, depois 42h no feed.

    O `check-1` criterio 6 ja mede uptime, mas so de dois coletores, e so
    porque eles deixam rastro por ACIDENTE (`uso_quotes.ts` e
    `backfill_ranges.completed_at`). `npm_link`, `macro` e `pools` nao deixam
    rastro nenhum — para eles, "parou ha dois dias" e indistinguivel de "nunca
    rodou".

    `ok=False` com `erro` preenchido tambem e batimento: ciclo que tentou e
    falhou e informacao diferente de ciclo que nao aconteceu.
    """

    __tablename__ = "coletor_heartbeat"

    coletor: Mapped[str] = mapped_column(String(32), primary_key=True)
    ts: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    itens: Mapped[int] = mapped_column(Integer, default=0)
    duracao_s: Mapped[float] = mapped_column(Float, default=0.0)
    erro: Mapped[str | None] = mapped_column(String(300), nullable=True)
    nota: Mapped[str | None] = mapped_column(String(200), nullable=True)


class PoolSnapshot(Base):
    """Catalogo de pools da chain, gravado de hora em hora.

    Existe porque APR **nao se reconstroi retroativamente**: a API do agregador
    entrega `apr`, `apr7d` e `apr30d` do agora, e nada guarda o historico
    daquele instante. Sem esta tabela, "o COST sustentou o volume?" so pode ser
    respondido esperando de novo.

    Chaveada por `(pool_address, ts)`: e medicao datada, nao estado. Mesma
    decisao de `pending_fees` — fee pendente e APR de janela sao fotos, e
    sobrescrever apagaria a serie que justifica a coleta.

    **Fonte SECUNDARIA.** TVL e volume vem do indexador deles; para decidir de
    verdade, medir na chain. O campo `confianca` e a nota que eles mesmos dao.
    """

    __tablename__ = "pool_snapshots"

    pool_address: Mapped[str] = mapped_column(String(42), primary_key=True)
    ts: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    nome: Mapped[str] = mapped_column(String(64))
    dex: Mapped[str] = mapped_column(String(32))
    fee_tier: Mapped[float] = mapped_column(Float, default=0.0)
    tvl_usd: Mapped[float] = mapped_column(Float, default=0.0)
    volume24h_usd: Mapped[float] = mapped_column(Float, default=0.0)

    # As tres janelas SEMPRE juntas: guardar so a de 24h reproduziria em banco
    # o erro que o modulo existe para impedir.
    apr24h: Mapped[float | None] = mapped_column(Float, nullable=True)
    apr7d: Mapped[float | None] = mapped_column(Float, nullable=True)
    apr30d: Mapped[float | None] = mapped_column(Float, nullable=True)

    risco: Mapped[str] = mapped_column(String(16), default="desconhecido")
    confianca: Mapped[int | None] = mapped_column(Integer, nullable=True)
    atualizado_em: Mapped[str | None] = mapped_column(String(40), nullable=True)


class MacroSnapshot(Base):
    """Contexto macro gravado no tempo. Sem isto, hipotese sobre macro morre.

    Por que a tabela existe: `macro/contexto.montar()` le tudo sob demanda e
    descarta. Se a OPEP anunciar corte amanha e o USO andar 4%, nao havera
    registro para correlacionar — a hipotese morreria por falta de dado, nao
    por falta de metodo. Noticia, ao contrario de preco e de calendario, **nao
    se reconstroi retroativamente**.

    Chaveada por `simbolo`, nao por `pool_address`. O inviolavel 8 do documento de regras do projeto
    manda `pool_address` em toda tabela, e a intencao dele e suportar varias
    pools depois. Macro de petroleo nao e propriedade da pool: e do ATIVO. Duas
    pools de USO compartilham o mesmo macro; uma pool de outro ativo teria
    outro. `simbolo` e a generalizacao correta da mesma intencao, e segue o
    precedente de `uso_quotes`, que tambem nao tem pool_address.
    """

    __tablename__ = "macro_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[int] = mapped_column(BigInteger, nullable=False)
    simbolo: Mapped[str] = mapped_column(String(16), nullable=False, default="USO")

    # o que entra na decisao
    fator_sigma: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    n_eventos: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # noticia — o unico componente que nao se reconstroi depois
    choque: Mapped[float | None] = mapped_column(Float, nullable=True)
    choque_disponivel: Mapped[bool] = mapped_column(Boolean, nullable=False,
                                                    default=False)
    artigos: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # roll e curva — reconstrutiveis, mas gravados para nao ter que recalcular
    em_roll: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    roll_progresso: Mapped[float | None] = mapped_column(Float, nullable=True)
    contrato_front: Mapped[str | None] = mapped_column(String(16), nullable=True)
    dias_ate_vencimento: Mapped[int | None] = mapped_column(Integer, nullable=True)
    curva_forma: Mapped[str | None] = mapped_column(String(16), nullable=True)

    # --- curva do CL, spot e estoques -------------------------------------
    #
    # Gravados como COLUNA e nao so dentro de `raw`, porque correlacionar exige
    # consulta. Ate 2026-08-21 eram calculados a cada coleta e descartados, e
    # sem serie nenhum deles pode virar hipotese testavel no replay.
    #
    # `None` significa fonte indisponivel naquela coleta — nunca zero. A
    # distincao importa: zero de estoque e um fato economico, ausencia nao.
    curva_inclinacao_pct: Mapped[float | None] = mapped_column(Float,
                                                               nullable=True)
    wti_front_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    wti_spot_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    brent_spot_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    spread_brent_wti_usd: Mapped[float | None] = mapped_column(Float,
                                                              nullable=True)
    estoque_cru_mbbl: Mapped[float | None] = mapped_column(Float, nullable=True)
    variacao_cru_mbbl: Mapped[float | None] = mapped_column(Float, nullable=True)
    cushing_mbbl: Mapped[float | None] = mapped_column(Float, nullable=True)
    variacao_cushing_mbbl: Mapped[float | None] = mapped_column(Float,
                                                                nullable=True)
    ovx_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    dxy: Mapped[float | None] = mapped_column(Float, nullable=True)

    fontes_vivas: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    raw: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    __table_args__ = (
        Index("ix_macro_simbolo_ts", "simbolo", "ts"),
    )


class AddressKind(Base):
    """EOA ou contrato. Existe por causa do inviolavel H5: contrato nao e LP.

    Medido na Fase A: das 157 carteiras com NFT nesta pool, **uma detem 572 das
    1.114 posicoes (51,3%)**. Um endereco com 572 posicoes numa pool de 32 dias
    nao e um LP com uma estrategia: e vault, roteador ou custodiante, e a
    "estrategia" dele e a soma de muitos usuarios. Perfilar comportamento dele
    junto com LPs individuais mistura duas coisas diferentes, e o teto de peso
    do consenso limita a INFLUENCIA sem corrigir a INTERPRETACAO.

    A classificacao exige `eth_getCode`, ou seja rede — por isso e persistida:
    e caro descobrir e nao muda nunca (um endereco nao deixa de ser contrato).
    Enquanto nao houver linha aqui, o rotulo e NAO_VERIFICADO, nunca "EOA".

    Sem `pool_address` de proposito, pelo mesmo motivo de `macro_snapshots`: a
    natureza de um endereco e propriedade da CHAIN, nao da pool. Duas pools
    compartilham a mesma resposta. `chain_id` e a generalizacao correta da
    intencao do inviolavel 8.
    """

    __tablename__ = "address_kinds"

    address: Mapped[str] = mapped_column(String(42), primary_key=True)
    chain_id: Mapped[int] = mapped_column(Integer, nullable=False, default=4663)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # EOA|CONTRATO
    code_size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    checked_at: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    __table_args__ = (Index("ix_address_kinds_kind", "chain_id", "kind"),)


class PendingFee(Base):
    """Fee acumulada e NAO coletada de uma posicao, lida do contrato.

    Existe para que o dashboard e o relatorio mostrem esse numero **sem tocar a
    rede** — o inviolavel da Fase 7 proibe RPC na apresentacao, e o check-7
    criterio 1 falha se `RpcClient` aparecer no dashboard. Quem le a chain e
    `run_lp_history.py --fees-pendentes`; quem consome le esta tabela.

    Por que a tabela precisou existir: o projeto declarou fee pendente
    "bloqueada por `feeGrowthOutside`" desde a Fase 4 e mostrava `$0,00` numa
    posicao com **$37,18** acumulados. O diagnostico confundiu reconstruir a
    serie historica (caro) com ler o estado atual (6 `eth_call`). Ver
    `lp_history/pending_fees.py`.

    `bloco` faz parte da PK: fee pendente e uma medicao datada, nao um estado.
    Guardar a serie permite ver a fee acumulando entre coletas.
    """

    __tablename__ = "pending_fees"

    pool_address: Mapped[str] = mapped_column(String(42), nullable=False)
    token_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    bloco: Mapped[int] = mapped_column(BigInteger, nullable=False)

    ts: Mapped[int] = mapped_column(BigInteger, nullable=False)
    tick_atual: Mapped[int] = mapped_column(Integer, nullable=False)
    liquidez: Mapped[str] = mapped_column(String(80), nullable=False)
    # 256 bits: TEXT, como todo valor grande neste banco
    fee0: Mapped[str] = mapped_column(String(80), nullable=False)
    fee1: Mapped[str] = mapped_column(String(80), nullable=False)
    tokens_owed0: Mapped[str] = mapped_column(String(80), nullable=False)
    tokens_owed1: Mapped[str] = mapped_column(String(80), nullable=False)
    preco_uso_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    usd: Mapped[float | None] = mapped_column(Float, nullable=True)

    __table_args__ = (
        PrimaryKeyConstraint("pool_address", "token_id", "bloco"),
        Index("ix_pending_fees_token_ts", "pool_address", "token_id", "ts"),
    )


__all__ = ["Base", "Block", "PoolEvent", "IndexerState", "BackfillRange",
           "UsoQuote", "MacroSnapshot", "AddressKind", "PendingFee"]
