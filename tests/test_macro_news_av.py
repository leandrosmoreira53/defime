"""Alpha Vantage NEWS_SENTIMENT — provider de noticias. Sem rede: fixture.

A fixture `alphavantage_news.json` e a **forma real** da API, capturada em
2026-08-22 com a chave `demo` e podada para 3 artigos. Os `time_published`
sao os verdadeiros, e por isso todo teste que depende de frescor passa `agora`
explicito — teste que muda de resultado conforme o relogio nao e teste.

Os dois testes que justificam o modulo sao `test_cota_estourada_*` e
`test_chave_demo_*`: os dois casos em que a API responde **HTTP 200** com algo
que nao e a resposta pedida. E a mesma familia das armadilhas ja pagas neste
projeto — futuros da EIA congelados em 2024-04-05 e o contrato da CFTC
renomeado — e a regra que sai delas e sempre a mesma: 200 nao e sinonimo de
dado bom, e ausencia se declara em vez de virar zero.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from defime.macro import news as N

FIXTURES = Path(__file__).parent / "fixtures"
# O artigo mais novo da fixture e 2026-08-22T18:06:59Z.
AGORA = dt.datetime(2026, 8, 22, 19, 0, tzinfo=dt.timezone.utc)


def _fixture() -> dict:
    return json.loads((FIXTURES / "alphavantage_news.json").read_text(
        encoding="utf-8"))


# --------------------------------------------------------------- montagem

def test_monta_titulos_da_fixture():
    leitura = N.AlphaVantageNews().montar(_fixture(), agora=AGORA, horas=12.0)
    assert leitura.disponivel and leitura.ao_vivo
    assert leitura.dados["n_no_feed"] == 3
    assert len(leitura.dados["titulos"]) == 3
    assert leitura.dados["fontes"]


def test_titulo_carrega_o_resumo_junto():
    """Os TERMOS do gate aparecem no corpo mais do que na manchete.

    Sem o resumo, `production cut` num texto cuja manchete diz so "OPEC meets"
    nao contaria — e o gate existe justamente para pegar esse caso.
    """
    dados = _fixture()
    dados["feed"][0]["title"] = "Manchete curta"
    dados["feed"][0]["summary"] = "corpo com production cut declarado"
    leitura = N.AlphaVantageNews().montar(dados, agora=AGORA, horas=12.0)
    assert "production cut" in leitura.dados["titulos"][0]


def test_sentimento_e_contexto_e_nao_entra_no_gate():
    """O paragrafo 7 do PLAN.md proibe sinal novo antes de o replay julga-lo.

    O sentimento do terceiro fica visivel em `dados` — util para ler — mas o
    `choque` continua saindo da contagem grosseira de `TERMOS`.
    """
    def gate(leitura):
        return sum(peso
                   for t in N._titulos(leitura, AGORA, 12.0)
                   for termo, peso in N.TERMOS.items() if termo in t.lower())

    neutro = _fixture()
    panico = _fixture()
    for a in panico["feed"]:
        a["overall_sentiment_score"] = -0.99
        a["overall_sentiment_label"] = "Bearish"

    l_neutro = N.AlphaVantageNews().montar(neutro, agora=AGORA, horas=12.0)
    l_panico = N.AlphaVantageNews().montar(panico, agora=AGORA, horas=12.0)

    # a nota do terceiro muda e fica visivel...
    assert l_neutro.dados["sentimento_medio"] != l_panico.dados["sentimento_medio"]
    assert l_panico.dados["sentimento_medio"] == -0.99
    # ...e o gate nao se mexe.
    assert gate(l_neutro) == gate(l_panico)


def test_artigo_fora_da_janela_nao_entra():
    dados = _fixture()
    dados["feed"][1]["time_published"] = "20260820T100000"   # 2 dias antes
    leitura = N.AlphaVantageNews().montar(dados, agora=AGORA, horas=12.0)
    assert len(leitura.dados["titulos"]) == 2


def test_janela_vazia_ainda_e_disponivel():
    """Zero artigo NA JANELA e uma medida ('nada aconteceu'), nao uma falha.

    Diferente de zero artigo NO FEED, que e a fonte nao ter respondido.
    """
    leitura = N.AlphaVantageNews().montar(_fixture(), agora=AGORA, horas=0.5)
    assert leitura.disponivel
    assert leitura.dados["titulos"] == []
    assert leitura.dados["n_no_feed"] == 3


# ------------------------------------------------------ 200 que nao e dado

def test_cota_estourada_nao_vira_zero_artigos():
    """A armadilha principal: 25 chamadas/dia estouradas devolvem HTTP 200.

    O corpo vem sem `feed` e com `Information`. Lido como "zero artigos", o
    `choque` sairia 0,0 — que nao e "sem fonte", e sim a AFIRMACAO de que nao
    ha noticia relevante. Falsa, e capaz de liberar um rerange que o gate
    deveria segurar.
    """
    aviso = {"Information": "Thank you for using Alpha Vantage! Our standard "
                            "API rate limit is 25 requests per day."}
    leitura = N.AlphaVantageNews().montar(aviso, agora=AGORA)
    assert not leitura.disponivel
    assert "25 requests per day" in (leitura.motivo or "")
    assert "titulos" not in leitura.dados


def test_feed_parado_e_recusado():
    """Mesma familia dos futuros da EIA congelados em 2024-04-05."""
    dados = _fixture()
    for a in dados["feed"]:
        a["time_published"] = "20260101T120000"
    leitura = N.AlphaVantageNews().montar(dados, agora=AGORA)
    assert not leitura.disponivel
    assert "parada" in (leitura.motivo or "")
    assert "2026-01-01" in (leitura.motivo or "")


def test_data_ilegivel_nao_vira_agora():
    dados = {"feed": [{"title": "x", "time_published": "ontem"}]}
    leitura = N.AlphaVantageNews().montar(dados, agora=AGORA)
    assert not leitura.disponivel
    assert "legivel" in (leitura.motivo or "")


# ------------------------------------------------------------ chave e cota

def test_sem_chave_nao_toca_a_rede():
    leitura = N.AlphaVantageNews(chave="").ler()
    assert not leitura.disponivel
    assert "alphavantage_api_key" in (leitura.motivo or "")


def test_chave_demo_e_recusada():
    """`demo` responde 200 com dado REAL — de AAPL.

    Aceita-la entregaria noticia de acao de tecnologia como se fosse do
    petroleo: o pior tipo de falha, porque nao parece falha.
    """
    leitura = N.AlphaVantageNews(chave="demo").ler()
    assert not leitura.disponivel
    assert "demo" in (leitura.motivo or "")


def test_intervalo_minimo_cabe_no_orcamento_diario():
    p = N.AlphaVantageNews
    assert 86400 / p.INTERVALO_MINIMO_S <= p.ORCAMENTO_DIARIO


def test_segunda_chamada_na_mesma_hora_devolve_cache_e_nao_ao_vivo():
    """Defende a cota por construcao, nao por disciplina de quem chama.

    `contexto.montar()` roda em cada coleta, em cada relatorio e em cada rerun
    do dashboard. Sem isto, um dia de dashboard aberto queima as 25.
    """
    import time
    p = N.AlphaVantageNews
    p.rearmar()
    try:
        p._ultima_chamada = time.time()
        p._ultima_leitura = N.Leitura(p.nome, True, {"titulos": ["a"]},
                                      ao_vivo=True)
        t0 = time.time()
        leitura = N.AlphaVantageNews(chave="k").ler()
        assert time.time() - t0 < 1.0          # nao foi a rede
        assert leitura.disponivel
        assert leitura.ao_vivo is False        # cache se declara cache
        assert leitura.dados["titulos"] == ["a"]
    finally:
        p.rearmar()


def test_sem_cache_e_dentro_do_intervalo_declara_o_motivo():
    import time
    p = N.AlphaVantageNews
    p.rearmar()
    try:
        p._ultima_chamada = time.time()
        leitura = N.AlphaVantageNews(chave="k").ler()
        assert not leitura.disponivel
        assert "orcamento" in (leitura.motivo or "")
    finally:
        p.rearmar()


# ---------------------------------------------------------------- escada

def test_escada_poe_alphavantage_antes_do_gdelt():
    """Ordem por confiabilidade medida, nao por qualidade de dado."""
    nomes = [p.nome for p in N.provider_noticias().providers]
    assert nomes.index("alphavantage_news") < nomes.index("gdelt")


def test_gdelt_continua_na_escada_como_ultimo_recurso():
    """Sem chave nenhuma configurada, o sistema nao pode ficar sem fonte."""
    nomes = [p.nome for p in N.provider_noticias().providers]
    assert "gdelt" in nomes
