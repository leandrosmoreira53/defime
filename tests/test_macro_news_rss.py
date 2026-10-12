"""RSS de noticias — o degrau sem chave e sem cadastro. Sem rede: fixtures.

`rss_google_news.xml` e `rss_oilprice.xml` sao a forma real dos dois feeds,
capturados em 2026-08-22 e podados para 3 itens. Foram escolhidos justamente
por discordarem entre si no que quebra parser:

  - Google News: `pubDate` em `GMT`, e itens VELHOS num feed novo (13/08 num
    feed de 22/08) — o caso que mata quem julga o feed pelo item mais antigo.
  - OilPrice: `pubDate` com offset `-0500`, e entidade HTML no titulo
    (`Isn&#039;t`) — o caso que mata quem compara string sem desescapar.

Todo teste passa `agora` explicito: teste cujo resultado depende do relogio
nao e teste.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from defime.macro import news as N

FIXTURES = Path(__file__).parent / "fixtures"
# OilPrice: item mais novo 2026-08-22 16:00 -0500 == 21:00 UTC.
# Google News mais novo: 2026-08-21 18:30:00 GMT.
AGORA = dt.datetime(2026, 8, 22, 22, 0, tzinfo=dt.timezone.utc)


def _xml(nome: str) -> str:
    return (FIXTURES / nome).read_text(encoding="utf-8")


def _feeds() -> dict:
    return {"google_news": _xml("rss_google_news.xml"),
            "oilprice": _xml("rss_oilprice.xml")}


# ---------------------------------------------------------------- parsing

def test_le_rss_com_pubdate_em_gmt():
    itens = N.RssNoticias.itens(_xml("rss_google_news.xml"))
    assert len(itens) == 3
    assert all(q is not None and q.tzinfo is not None for _, q in itens)


def test_le_rss_com_offset_negativo():
    """-0500 nao e UTC. Tratar como UTC erraria 5h na janela."""
    itens = N.RssNoticias.itens(_xml("rss_oilprice.xml"))
    assert len(itens) == 3
    topo = max(q for _, q in itens)
    # 16:00 -0500 == 21:00 UTC. Lido como UTC, cairia 5h antes e sumiria de
    # qualquer janela curta.
    assert topo.utcoffset() == dt.timedelta(hours=-5)
    assert topo.astimezone(dt.timezone.utc) == dt.datetime(
        2026, 8, 22, 21, 0, tzinfo=dt.timezone.utc)


def test_desescapa_entidade_html_no_titulo():
    """`Isn&#039;t` tem que virar `Isn't` antes de bater com os TERMOS."""
    titulos = [t for t, _ in N.RssNoticias.itens(_xml("rss_oilprice.xml"))]
    assert any("Isn't" in t for t in titulos)
    assert not any("&#039;" in t for t in titulos)


def test_xml_quebrado_nao_levanta():
    """Feed alheio quebrado e caso normal, nao excecao (regra do base.py)."""
    assert N.RssNoticias.itens("<rss><item><title>sem fechar") == []
    assert N.RssNoticias.itens("") == []


# ----------------------------------------------------------------- janela

def test_item_velho_em_feed_novo_nao_entra_na_janela():
    """O caso medido: item de 13/08 num feed de 22/08.

    Contar essa materia como choque de agora seria ler noticia vencida como
    evento corrente — exatamente o que o gate existe para nao fazer.
    """
    leitura = N.RssNoticias().montar(_feeds(), agora=AGORA, horas=12.0)
    assert leitura.disponivel
    # na janela de 12h so cabe o OilPrice (21:00, 21:00 e 19:00 UTC)
    assert len(leitura.dados["titulos"]) == 3


def test_janela_maior_alcanca_o_feed_mais_lento():
    curta = N.RssNoticias().montar(_feeds(), agora=AGORA, horas=12.0)
    larga = N.RssNoticias().montar(_feeds(), agora=AGORA, horas=48.0)
    assert len(larga.dados["titulos"]) > len(curta.dados["titulos"])


def test_frescor_se_mede_no_item_mais_novo_nao_no_mais_velho():
    """O Google News tem item de 9 dias atras e continua sendo feed vivo."""
    leitura = N.RssNoticias().montar(_feeds(), agora=AGORA, horas=48.0)
    assert "google_news" in leitura.dados["feeds_ok"]


# ------------------------------------------------------------ duplicacao

def test_titulo_repetido_entre_feeds_conta_uma_vez():
    """`choque` e SOMA de pesos: a mesma materia em 3 feeds triplicaria o peso.

    Com o limiar em 6,0, uma unica noticia sobre `production cut` (peso 3,0)
    replicada em tres feeds estouraria o gate sozinha.
    """
    a = _xml("rss_oilprice.xml")
    leitura = N.RssNoticias().montar({"f1": a, "f2": a, "f3": a},
                                     agora=AGORA, horas=12.0)
    assert len(leitura.dados["titulos"]) == 3      # nao 9


def test_deduplicacao_ignora_caixa_e_espaco():
    base = _xml("rss_oilprice.xml")
    variante = base.replace("The Gas Turbine", "the   gas turbine")
    leitura = N.RssNoticias().montar({"a": base, "b": variante},
                                     agora=AGORA, horas=12.0)
    assert len(leitura.dados["titulos"]) == 3


# ------------------------------------------------------ falha e degradacao

def test_um_feed_morto_nao_derruba_a_leitura():
    """Falha parcial reduz amostra; nao apaga a fonte."""
    feeds = _feeds()
    feeds["quebrado"] = "<html>404</html>"
    leitura = N.RssNoticias().montar(feeds, agora=AGORA, horas=12.0)
    assert leitura.disponivel
    assert "quebrado" in leitura.dados["feeds_falhos"]
    assert "oilprice" in leitura.dados["feeds_ok"]


def test_todos_os_feeds_parados_e_indisponivel():
    """Mesma regra da EIA e da CFTC: HTTP 200 com dado morto nao e dado."""
    leitura = N.RssNoticias().montar(
        _feeds(), agora=dt.datetime(2026, 12, 1, tzinfo=dt.timezone.utc))
    assert not leitura.disponivel
    assert "parado" in (leitura.motivo or "")


def test_sem_feed_nenhum_e_indisponivel_e_nao_zero():
    leitura = N.RssNoticias().montar({}, agora=AGORA)
    assert not leitura.disponivel
    assert "titulos" not in leitura.dados


def test_feed_sem_data_e_recusado():
    """Sem data nao da para saber se e de hoje — e supor que e seria inventar."""
    leitura = N.RssNoticias().montar(
        {"x": "<rss><channel><item><title>OPEC production cut</title>"
              "</item></channel></rss>"}, agora=AGORA)
    assert not leitura.disponivel
    assert "data" in (leitura.motivo or "")


# ------------------------------------------------------------------ cota

def test_segunda_chamada_no_intervalo_devolve_cache_declarado():
    """`contexto.montar()` roda em cada rerun do dashboard.

    RSS de graca nao e RSS sem custo para quem serve.
    """
    import time
    p = N.RssNoticias
    p.rearmar()
    try:
        p._ultima_chamada = time.time()
        p._ultima_leitura = N.Leitura(p.nome, True, {"titulos": ["a"]},
                                      ao_vivo=True)
        t0 = time.time()
        leitura = N.RssNoticias().ler()
        assert time.time() - t0 < 1.0
        assert leitura.disponivel and leitura.ao_vivo is False
    finally:
        p.rearmar()


# ---------------------------------------------------------------- escada

def test_escada_poe_rss_antes_de_quem_pede_chave():
    """RSS nao pede cadastro nem tem cota: funciona no `.env` como ele esta."""
    nomes = [p.nome for p in N.provider_noticias().providers]
    assert nomes == ["manual_csv", "rss_noticias", "alphavantage_news", "gdelt"]


def test_feeds_padrao_incluem_consulta_arbitraria():
    """O Google News RSS substitui o que se usava do GDELT: consulta livre."""
    assert any("{q}" in url for _, url in N.FEEDS_PADRAO)


def test_gate_le_os_titulos_do_rss():
    """Fecha o circuito: a Leitura do RSS chega ao `choque` pelo mesmo caminho."""
    leitura = N.RssNoticias().montar(
        {"x": "<rss><channel><item><title>OPEC agrees production cut</title>"
              "<pubDate>Sat, 22 Aug 2026 21:00:00 GMT</pubDate></item>"
              "</channel></rss>"}, agora=AGORA, horas=12.0)
    titulos = N._titulos(leitura, AGORA, 12.0)
    total = sum(peso for t in titulos
                for termo, peso in N.TERMOS.items() if termo in t.lower())
    assert total >= N.TERMOS["production cut"]


# ------------------------------------------------- TERMOS (2026-08-22)

def test_nenhum_termo_e_substring_de_outro():
    """Invariante estrutural: `choque` e SOMA, entao sobreposicao conta duas vezes.

    Havia um caso real e grave: `opec` dentro de `opec+` fazia qualquer
    manchete com "OPEC+" somar 3,0 + 3,0 = 6,0 — exatamente o `CHOQUE_ALTO`.
    Uma noticia rotineira sobre a OPEP+ disparava o gate sozinha.

    Este teste vale mais do que o caso que o originou: ele impede a proxima
    pessoa (ou eu) de acrescentar `strait of hormuz` ao lado de `hormuz` e
    reintroduzir a familia inteira do bug.
    """
    pares = [(a, b) for a in N.TERMOS for b in N.TERMOS if a != b and a in b]
    assert pares == [], f"termos sobrepostos somam duas vezes: {pares}"


def test_hormuz_curto_pontua():
    """Regressao da leitura ao vivo de 2026-08-22.

    A manchete abaixo e real e pontuava **0**, porque o termo cadastrado era
    `strait of hormuz` e o texto corrente diz so `Hormuz`.
    """
    manchete = ("Global oil prices rise: Brent crude surpasses $94 "
                "amid the Hormuz crisis")
    pontos = sum(peso for termo, peso in N.TERMOS.items()
                 if termo in manchete.lower())
    assert pontos >= 3.0


def test_opec_mais_nao_conta_dobrado():
    manchete = "OPEC+ meets in Vienna next week"
    pontos = sum(peso for termo, peso in N.TERMOS.items()
                 if termo in manchete.lower())
    assert pontos == 3.0                      # nao 6,0
    assert pontos < N.CHOQUE_ALTO             # nao dispara o gate sozinha


def test_evento_grave_de_verdade_ainda_dispara():
    """Fechar a contagem dupla nao pode ter tornado o gate inerte."""
    manchete = ("OPEC announces production cut as attack closes the "
                "Hormuz shipping lane")
    pontos = sum(peso for termo, peso in N.TERMOS.items()
                 if termo in manchete.lower())
    assert pontos >= N.CHOQUE_ALTO
