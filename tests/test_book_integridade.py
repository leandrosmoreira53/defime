"""O livro incompleto tem que se denunciar sozinho.

Regressao de 2026-09-01: a MU/USDG foi indexada a partir do bloco 30.480.683
quando a pool nasceu em 20.656.675. O `backfill_ranges` registrava a faixa
pedida como concluida — corretamente — e ninguem perguntou pelo resto. O
sintoma foi um grafico de liquidez com dois espetos, nao um erro.
"""

from defime.core.book import TickBook


def test_livro_completo_e_integro():
    # Uma posicao em [100, 200): soma em 100, subtrai em 200.
    b = TickBook(net={100: 5_000, 200: -5_000})
    r = b.integridade()
    assert r["integro"] is True
    assert r["motivo"] is None
    assert r["resto_no_topo"] == "0"


def test_burn_sem_mint_da_liquidez_negativa():
    """Exatamente o caso da MU: o `Burn` entrou no indice, o `Mint` ficou fora."""
    b = TickBook(net={100: -5_000, 200: 5_000})
    r = b.integridade()
    assert r["integro"] is False
    assert r["segmentos_negativos"] == 1
    assert int(r["pior_liquidez"]) < 0
    assert "backfill" in r["motivo"]


def test_resto_no_topo_pega_o_que_a_curva_positiva_esconde():
    """A invariante do resto e MAIS FORTE que a do sinal.

    Aqui a curva nunca fica negativa — 8.000, depois 3.000 — e mesmo assim o
    livro esta furado: falta a perna que devolveria os 3.000 ao fim. Testar so
    o sinal deixaria este caso passar.
    """
    b = TickBook(net={100: 8_000, 200: -5_000})
    r = b.integridade()
    assert r["segmentos_negativos"] == 0
    assert r["integro"] is False
    assert r["resto_no_topo"] == "3000"


def test_livro_vazio_nao_e_falso_positivo():
    assert TickBook(net={}).integridade()["integro"] is True
