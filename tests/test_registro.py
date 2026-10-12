"""Os tres gravadores de serie. Cada teste nomeia um erro que ja custou caro.

O que se testa aqui nao e "grava sem levantar" — e as tres regras que fazem a
serie ser confiavel: medicao datada nunca sobrescreve, ausencia nunca vira zero,
e o gravador le as chaves que o produtor realmente publica.
"""

from __future__ import annotations

import json
import time

import pytest
from sqlalchemy import func, select

from defime.db import registro as R
from defime.db.models import ColetorHeartbeat, Decisao, PosicaoSnapshot
from defime.db.session import session_scope

# Este modulo ESCREVE. Sem isto ele suja `defime.db`, que foi exatamente o que
# aconteceu na primeira execucao (8 linhas de fixture no banco de producao).
pytestmark = pytest.mark.usefixtures("banco_temporario")

POOL = "0x02175608000000000000000000000000d111d915"


def _conta(modelo) -> int:
    with session_scope() as s:
        return s.execute(select(func.count()).select_from(modelo)).scalar_one()


# --------------------------------------------------------------- batimento

def test_batimento_grava_ok_e_falha_como_linhas_diferentes():
    """Ciclo que TENTOU e falhou nao e o mesmo que ciclo ausente."""
    t = int(time.time()) + 900_000
    R.gravar_batimento("teste_x", ts=t, itens=7)
    R.gravar_batimento("teste_x", ts=t + 1, ok=False, erro="boom")
    with session_scope() as s:
        linhas = s.execute(
            select(ColetorHeartbeat.ok, ColetorHeartbeat.itens,
                   ColetorHeartbeat.erro)
            .where(ColetorHeartbeat.coletor == "teste_x")
            .order_by(ColetorHeartbeat.ts)).all()
    assert [x[0] for x in linhas] == [True, False]
    assert linhas[0][1] == 7 and linhas[1][2] == "boom"


def test_batimento_nunca_levanta():
    """Coletor 24/7 nao pode morrer porque o log falhou. Nome invalido de
    coletor, tipo errado, banco fechado — tudo tem que sair calado."""
    R.gravar_batimento("x" * 500, itens="nao e int")   # type: ignore[arg-type]


def test_batimento_repetido_no_mesmo_segundo_nao_duplica():
    t = int(time.time()) + 900_100
    R.gravar_batimento("teste_dup", ts=t)
    R.gravar_batimento("teste_dup", ts=t, itens=99)
    with session_scope() as s:
        n = s.execute(select(func.count()).select_from(ColetorHeartbeat)
                      .where(ColetorHeartbeat.coletor == "teste_dup")).scalar_one()
    assert n == 1, "on_conflict_do_nothing: medicao datada nao sobrescreve"


def test_batimento_periodico_estrangula_mas_o_primeiro_sempre_passa():
    """O throttle e EM PROCESSO de proposito: reiniciar o coletor sempre grava.

    Processo que voltou e exatamente o evento que interessa; um throttle
    persistido no banco o esconderia.
    """
    R._ULTIMO_BAT.pop("teste_thr", None)
    antes = _conta(ColetorHeartbeat)
    assert R.batimento_periodico("teste_thr", minimo_s=3600) is True
    assert R.batimento_periodico("teste_thr", minimo_s=3600) is False
    assert _conta(ColetorHeartbeat) == antes + 1

    R._ULTIMO_BAT.pop("teste_thr", None)          # simula reinicio do processo
    assert R.batimento_periodico("teste_thr", minimo_s=3600) is True


# ---------------------------------------------------------------- posicao

def test_posicao_fee_ausente_fica_none_e_nunca_zero():
    """`None` = nao medi. `0.0` = medi e deu zero. Sao coisas diferentes, e
    confundi-las ja fez o dashboard mostrar $0,00 numa posicao com $37,18."""
    t = int(time.time()) + 900_200
    R.gravar_posicao([{
        "pool_address": POOL, "token_id": 999_001, "ts": t, "bloco": 1,
        "preco_usd": 130.0, "tick_atual": 227_000, "em_range": True,
        "fee_pendente_usd": None, "pnl_total_usd": None}])
    with session_scope() as s:
        fee, pnl = s.execute(
            select(PosicaoSnapshot.fee_pendente_usd,
                   PosicaoSnapshot.pnl_total_usd)
            .where(PosicaoSnapshot.token_id == 999_001)).one()
    assert fee is None and pnl is None


def test_posicao_sem_pool_ou_token_e_descartada():
    """Linha sem chave nao e linha incompleta: e linha que nao identifica nada.
    O inviolavel 8 pede `pool_address` em toda tabela."""
    assert R.gravar_posicao([{"token_id": 1, "ts": 1}]) == 0
    assert R.gravar_posicao([{"pool_address": POOL, "ts": 1}]) == 0
    assert R.gravar_posicao([]) == 0


# ---------------------------------------------------------------- decisao

def _rel(acao="HOLD"):
    return {
        "instante": {"ts": 1_787_000_000, "preco_usd": 126.2176},
        "calibracao": {"sigma_anualizado_pct": 40.67,
                       "unidade_da_grade_usd": 2.79},
        "macro": {"fator_sigma": 1.45},
        "nossa_posicao": {"token_id": 741_646, "capital_usd": 13_024.99,
                          "em_range": True, "faixa_usd": [120.32, 136.48]},
        "decisao": {
            "acao": acao, "motivo": "nenhum candidato sobrevive ao p25",
            "custo_usd": 21.38, "simulacoes_executadas": 165,
            "alvo_de_vantagem_usd": 32.06,
            "vantagem_usd": {"p25": -95.69, "mediana": 45.22},
            "melhor_candidato": {"ticks": [227_700, 228_060]},
            "gates_bloqueantes": ["G4"], "gates": [{"gate": "G1"}],
            "hold": {"n": 1500}, "seed": 20_260_815},
    }


def test_decisao_le_as_chaves_QUE_O_MOTOR_PUBLICA():
    """A primeira versao procurava `vantagem` e `detalhes`, que nao existem.

    Nada teria falhado: as colunas ficariam NULL — que se le como "nao medido"
    quando na verdade foi medido. E a familia de bug mais cara deste projeto.
    """
    rel = _rel()
    assert R.gravar_decisao(rel, pool=POOL, ts=1_787_000_000) is True
    with session_scope() as s:
        med, p25, alvo, custo, sim = s.execute(
            select(Decisao.vantagem_mediana_usd, Decisao.vantagem_p25_usd,
                   Decisao.alvo_usd, Decisao.custo_usd, Decisao.simulacoes)
            .where(Decisao.ts == 1_787_000_000)).one()
    assert med == pytest.approx(45.22)
    assert p25 == pytest.approx(-95.69)
    assert alvo == pytest.approx(32.06)
    assert custo == pytest.approx(21.38)
    assert sim == 165


def test_decisao_guarda_o_vetor_de_features_do_inviolavel_7():
    R.gravar_decisao(_rel(), pool=POOL, ts=1_787_000_001)
    with session_scope() as s:
        (f,) = s.execute(select(Decisao.features)
                         .where(Decisao.ts == 1_787_000_001)).one()
    f = f if isinstance(f, dict) else json.loads(f)
    # `hold` e `melhor_candidato` inteiros: sem eles o replay reconstitui o
    # veredito, nao a comparacao que o produziu.
    for k in ("faixa_usd", "capital_usd", "vantagem_usd", "melhor_candidato",
              "hold", "seed", "basis_pct"):
        assert k in f, k
    assert f["hold"] == {"n": 1500}


def test_relatorio_sem_decisao_devolve_false_e_nao_grava():
    """Gate bloqueante e posicao nao encontrada produzem relatorio sem decisao.
    Gravar `acao=None` seria inventar uma que nao houve."""
    antes = _conta(Decisao)
    assert R.gravar_decisao({}, pool=POOL) is False
    assert R.gravar_decisao({"decisao": None}, pool=POOL) is False
    assert R.gravar_decisao({"decisao": {"acao": None}}, pool=POOL) is False
    assert _conta(Decisao) == antes


def test_decisao_no_mesmo_ts_nao_sobrescreve():
    R.gravar_decisao(_rel("HOLD"), pool=POOL, ts=1_787_000_002)
    R.gravar_decisao(_rel("RERANGE"), pool=POOL, ts=1_787_000_002)
    with session_scope() as s:
        acoes = s.execute(select(Decisao.acao)
                          .where(Decisao.ts == 1_787_000_002)).scalars().all()
    assert acoes == ["HOLD"], "a primeira medicao do instante e a que vale"


# ------------------------------------------------- uptime nao vaza entre pools

