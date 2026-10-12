"""A janela de medicao vale DIAS DE MERCADO, nao dias de relogio.

Estes testes existem porque a alternativa que estava no ar era pior: a pagina
apagava o numero ("medindo") sempre que a amostra fosse menor que um dia. O
O usuário pediu o contrario em 05/09 — "sempre calcule" — e a unica forma
honesta de sempre calcular e consertar o denominador.
"""

import datetime as dt

from defime.core.sessoes import (FATOR_VOLUME, FIM_DE_SEMANA, PREGAO,
                                 dias_equivalentes, fator_de_volume,
                                 sessao_de)

SEG_9H = dt.datetime(2026, 9, 7, 9, 0, tzinfo=dt.UTC).timestamp()


def test_uma_semana_inteira_vale_exatamente_sete_dias():
    """**A correcao tem que ser neutra no limite longo.** Se nao fosse, todo
    numero maduro ja publicado neste projeto mudaria de valor ao ligar a
    reponderacao — e a mudanca seria indistinguivel de um bug. E o que
    `_MEDIA_SEMANAL` garante, e e por isso que ele existe."""
    assert abs(dias_equivalentes(SEG_9H, SEG_9H + 7 * 86400) - 7.0) < 0.002
    assert abs(dias_equivalentes(SEG_9H, SEG_9H + 28 * 86400) - 28.0) < 0.01


def test_o_pedaco_caro_do_dia_vale_MAIS_de_um_dia_por_hora():
    """Tres horas de pregao nao sao 0,125 dia: sao mais. Foi essa diferenca
    que fez a POS_D publicar $305/dia — 770% ao ano — a partir de 7,6 horas
    inteiras do horario de maior volume da chain."""
    inicio = SEG_9H + 5.5 * 3600            # 14:30 UTC = 10:30 ET, pregao
    assert sessao_de(int(inicio)) == PREGAO
    eq = dias_equivalentes(inicio, inicio + 3 * 3600)
    assert eq > 3 / 24, eq


def test_o_fim_de_semana_vale_MENOS_e_por_isso_a_taxa_dele_sobe():
    """0,462x de volume: tres horas de sabado sao pouco mais de uma hora de
    mercado medio. A taxa diaria projetada dali tem que ser MAIOR que a de
    relogio, nao menor — a posicao arrecadou aquilo no pedaco mais fraco da
    semana, e um dia medio pagaria mais."""
    sab = SEG_9H + 5 * 86400 + 7 * 3600
    assert sessao_de(int(sab)) == FIM_DE_SEMANA
    eq = dias_equivalentes(sab, sab + 3 * 3600)
    assert eq < 3 / 24, eq


def test_janela_vazia_nao_inventa_dia():
    assert dias_equivalentes(SEG_9H, SEG_9H) == 0.0
    assert dias_equivalentes(SEG_9H + 10, SEG_9H) == 0.0


def test_os_fatores_sao_os_MEDIDOS_nesta_pool():
    """Numero que veio da medicao nao se ajusta para o resultado ficar
    bonito. Estes sao os da secao "Fim de semana: sigma igual, volume pela
    metade" do documento de regras do projeto, e mexer neles muda toda taxa diaria da pagina."""
    assert FATOR_VOLUME == {"pre": 1.825, "pregao": 1.320, "pos": 0.800,
                            "madrugada": 0.721, "fim_de_semana": 0.462}
    # normalizado: a media de uma semana inteira e 1,0 por construcao
    amostra = [fator_de_volume(int(SEG_9H + i * 600)) for i in range(1008)]
    assert abs(sum(amostra) / len(amostra) - 1.0) < 0.01
