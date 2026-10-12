"""O perfil de semicondutores carrega o mesmo invariante que o de petroleo.

`choque` e uma SOMA sobre os termos que casam. Se um termo for substring de
outro, a mesma manchete soma duas vezes — foi o que `opec` dentro de `opec+`
fez, dando 6,0 (exatamente o `CHOQUE_ALTO`) para uma noticia rotineira.

O teste do invariante vale mais que os casos que o originaram: e ele que
impede a reintroducao num perfil novo, escrito meses depois por alguem que
nao viu o bug original.
"""

import pytest

from defime.macro import news


def _todos_os_perfis():
    saida = [("petroleo", news.TERMOS)]
    for nome, cfg in news.PERFIS.items():
        if cfg[0]:
            saida.append((nome, cfg[0]))
    return saida


@pytest.mark.parametrize("nome,termos", _todos_os_perfis())
def test_nenhum_termo_e_substring_de_outro(nome, termos):
    for a in termos:
        for b in termos:
            if a != b:
                assert a not in b, (
                    f"perfil {nome}: '{a}' esta dentro de '{b}' — a mesma "
                    f"manchete somaria {termos[a]} + {termos[b]}")


@pytest.mark.parametrize("nome,termos", _todos_os_perfis())
def test_os_termos_sao_minusculos_porque_a_comparacao_e(nome, termos):
    """`choque` compara com `titulo.lower()`. Termo com maiuscula nunca casa —
    falharia calado, entregando choque MENOR que o real."""
    for t in termos:
        assert t == t.lower(), f"perfil {nome}: '{t}' tem maiuscula"


@pytest.mark.parametrize("nome,termos", _todos_os_perfis())
def test_nenhum_termo_sozinho_dispara_o_gate(nome, termos):
    """Um termo isolado nao pode valer `CHOQUE_ALTO`.

    Se valesse, uma unica manchete com uma palavra comum abriria o gate — que
    e exatamente o efeito que `opec+` tinha por soma.
    """
    for t, peso in termos.items():
        assert peso < news.CHOQUE_ALTO, (
            f"perfil {nome}: '{t}' vale {peso} e sozinho ja atinge "
            f"{news.CHOQUE_ALTO}")


def test_o_perfil_de_semicondutores_nao_usa_o_csv_manual():
    """O CSV e um arquivo unico com noticia de petroleo. Servi-lo para uma
    pergunta sobre memoria seria dado errado entregue em silencio."""
    escada = news.provider_de_perfil("semicondutores")
    tipos = [type(p).__name__ for p in escada.providers]
    assert "ManualCSV" not in tipos, tipos


def test_perfil_desconhecido_cai_no_padrao_em_vez_de_levantar():
    c = news.provider_de_perfil("inexistente")
    assert c is not None
    assert news.limiar_do_perfil("inexistente") == news.CHOQUE_ALTO


def test_termo_de_ASSUNTO_nao_pontua():
    """A primeira versao somou `hbm`, `dram` e `nand` como se fossem evento,
    e o choque saiu 35,0 contra limiar 6,0 — media que a CONSULTA funcionou,
    nao que houve noticia. Assunto agora filtra; nao pontua."""
    for a in news.ASSUNTO_SEMICONDUTORES:
        assert a not in news.TERMOS_SEMICONDUTORES, (
            f"'{a}' e palavra de assunto e nao pode valer pontos")


def test_o_filtro_de_assunto_descarta_manchete_de_fora_do_setor():
    """`tariff` sozinho, numa materia de comercio exterior, nao e choque de
    memoria. Sem o filtro ela somaria 2,5."""
    _, _, assunto, _ = news.PERFIS["semicondutores"]
    fora = "new tariff on brazilian steel announced"
    dentro = "new tariff hits dram imports"
    assert not any(a in fora for a in assunto)
    assert any(a in dentro for a in assunto)
