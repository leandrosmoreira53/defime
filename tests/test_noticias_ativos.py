"""Fase N1 do PLAN_NOTICIAS — manchetes por ativo. Nenhum teste faz rede.

As fixtures em `tests/fixtures/noticias/` foram gravadas das fontes reais em
23/09/2026 ~21:30 UTC; os casos de zero itens sao escritos a mao e dizem isso.
"""

from __future__ import annotations

import ast
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from defime.macro import noticias_ativos as N
from defime.macro import noticias_banco as B
from defime.macro.news import TERMOS, TERMOS_JOGOS, TERMOS_SEMICONDUTORES

RAIZ = Path(__file__).resolve().parents[1]
FIX = RAIZ / "tests" / "fixtures" / "noticias"
# Relogio congelado no instante da gravacao: frescor depende dele, e sem isto
# o teste mediria o dia em que alguem o rodou (a licao do test_macro_eia).
AGORA = int(datetime(2026, 9, 23, 22, 0, tzinfo=timezone.utc).timestamp())
UNIVERSO = {"MU", "NVDA", "SNDK", "QUBT", "SPY", "COST", "GOOGL"}


def _f(nome: str) -> str:
    return (FIX / nome).read_text(encoding="utf-8")


# ------------------------------------------------------------ Yahoo busca

def test_yahoo_busca_atribui_pela_fonte():
    ld = N.ler_yahoo_busca(_f("yahoo_search_mu.json"), "MU", UNIVERSO, AGORA)
    assert ld.ok and len(ld.itens) == 20
    assert all("MU" in it.simbolos for it in ld.itens)
    # a materia "Top Research Reports for Micron, Costco..." e tambem de COST
    reports = [it for it in ld.itens if it.titulo.startswith("Top Research")]
    assert reports and "COST" in reports[0].simbolos


def test_so_simbolos_do_nosso_universo_viram_ligacao():
    ld = N.ler_yahoo_busca(_f("yahoo_search_qubt.json"), "QUBT", UNIVERSO, AGORA)
    todos = {s for it in ld.itens for s in it.simbolos}
    assert todos <= UNIVERSO
    assert "IONQ" not in todos and "RGTI" not in todos
    # mas quantos a FONTE marcou fica registrado — materia com 17 tickers e
    # resumo de mercado, nao noticia da empresa
    assert max(it.n_tickers for it in ld.itens) >= 10


def test_item_sem_o_simbolo_pedido_e_descartado():
    texto = json.dumps({"news": [
        {"title": "Outra empresa", "link": "https://x/1",
         "providerPublishTime": AGORA - 60, "relatedTickers": ["IONQ"]},
        {"title": "Quantum Computing Inc sobe", "link": "https://x/2",
         "providerPublishTime": AGORA - 60, "relatedTickers": ["QUBT"]}]})
    ld = N.ler_yahoo_busca(texto, "QUBT", UNIVERSO, AGORA)
    assert [it.titulo for it in ld.itens] == ["Quantum Computing Inc sobe"]


def test_zero_itens_e_sem_leitura_nao_sem_noticia():
    # escrito a mao: e a forma do 200-com-lista-vazia medido no Google em 03/09
    ld = N.ler_yahoo_busca('{"news": []}', "MU", UNIVERSO, AGORA)
    assert not ld.ok and ld.itens == []
    assert "sem leitura" in ld.erro


def test_json_quebrado_nao_levanta():
    ld = N.ler_yahoo_busca("<html>captcha</html>", "MU", UNIVERSO, AGORA)
    assert not ld.ok and "JSON" in ld.erro


def test_feed_parado_e_recusado():
    texto = json.dumps({"news": [
        {"title": "velha", "link": "https://x/1", "relatedTickers": ["MU"],
         "providerPublishTime": AGORA - 40 * 86400}]})
    ld = N.ler_yahoo_busca(texto, "MU", UNIVERSO, AGORA)
    assert not ld.ok and "dias" in ld.erro


# ------------------------------------------------------------ RSS

def test_yahoo_rss_por_ticker():
    ld = N.ler_yahoo_rss(_f("yahoo_rss_qubt.xml"), "QUBT", AGORA)
    assert ld.ok and len(ld.itens) == 19
    assert all(it.simbolos == ("QUBT",) for it in ld.itens)
    # parametro de rastreio sai do link
    assert not any(".tsrc" in it.link for it in ld.itens)


def test_google_news_filtra_pelo_nome_e_tira_o_veiculo():
    xml = _f("google_news_usar.xml")
    bruto = xml.count("<item>")
    ld = N.ler_google_news(xml, "USAR", "USA Rare Earth", AGORA)
    # 100 itens de "terras raras" em geral; so fica o que cita a empresa
    assert bruto == 100 and len(ld.itens) < bruto
    for it in ld.itens:
        assert "usa rare earth" in N.normalizar_titulo(it.titulo)
        assert it.publisher and not it.titulo.endswith(" - " + it.publisher)


def test_google_sem_itens_do_nome_e_sem_leitura():
    xml = ('<rss><channel><item><title>Rare earths rally - Reuters</title>'
           '<link>https://n/1</link><pubDate>Wed, 23 Sep 2026 20:00:00 +0000'
           '</pubDate><source>Reuters</source></item></channel></rss>')
    ld = N.ler_google_news(xml, "USAR", "USA Rare Earth", AGORA)
    assert not ld.ok


# ------------------------------------------------------------ normalizacao

def test_titulo_norm_casa_a_mesma_materia():
    assert (N.normalizar_titulo("Micron&#39;s stock — ‘battleground’")
            == N.normalizar_titulo("Micron's Stock - battleground"))


def test_link_canonico():
    a = "https://Finance.Yahoo.com/a.html?.tsrc=rss&utm_source=x&id=3#top"
    assert N.link_canonico(a) == "https://finance.yahoo.com/a.html?id=3"
    assert N.id_noticia(a) == N.id_noticia("https://finance.yahoo.com/a.html?id=3")


# ------------------------------------------------------------ eventos

def test_evento_por_palavra_inteira():
    # `ban` e termo da RBLX; por substring casaria em "bank"
    assert N.eventos("Bank upgrades Roblox outlook", "RBLX") == ()
    assert "ban" in N.eventos("Country moves to ban Roblox", "RBLX")


def test_vocabulario_por_perfil_unido_ao_geral():
    assert N.perfil_do_simbolo("USO") is TERMOS
    assert N.perfil_do_simbolo("mu") is TERMOS_SEMICONDUTORES
    assert N.perfil_do_simbolo("TTWO") is TERMOS_JOGOS
    assert "guidance" in N.eventos("Costco raises guidance", "COST")
    # medido na primeira coleta: sem a uniao isto nao marcava
    assert "price target" in N.eventos(
        "Micron Price Target Raised to $1,300 by Citi", "MU")
    assert "export control" in N.eventos("New export control hits chips", "MU")


def test_nomeia_pelo_ticker_ou_pelo_nome():
    assert N.nomeia("Micron (MU) vs Western Digital", "MU")
    assert N.nomeia("Citi Resets Micron Target After Surprise", "MU")
    assert N.nomeia("Bank of America cuts UPS price target", "UPS")
    # ticker so em maiuscula; nome em palavra inteira
    assert not N.nomeia("Stocks see ups and downs", "UPS")
    assert not N.nomeia("A new investment strategy", "MSTR")
    assert not N.nomeia("Much ado", "MU")
    # o caso que motivou: a fonte marcou NVDA numa materia da General Mills
    assert not N.nomeia("General Mills Reaffirms Full-Year Guidance", "NVDA")


def test_evento_de_resumo_de_mercado_nao_vira_destaque_de_quem_nao_e_nomeado():
    c = _banco()
    it = N.Item("yahoo_busca", "General Mills Reaffirms Full-Year Guidance",
                "https://y/gm", AGORA - 100, "Zacks", ("NVDA",), 2,
                ("guidance",), ())
    B.gravar(c, N.Lida("yahoo_busca", "NVDA", True, [it]), AGORA)
    assert len(B.ler(c, agora=AGORA, simbolo="NVDA")) == 1    # continua la
    assert B.ler(c, agora=AGORA, simbolo="NVDA", so_eventos=True) == []
    assert B.ler(c, agora=AGORA, so_eventos=True) == []


def test_geral_e_recorte_dos_tres_e_sem_substring():
    todos = {**TERMOS, **TERMOS_SEMICONDUTORES, **TERMOS_JOGOS}
    for t, peso in N.TERMOS_GERAL.items():
        assert todos[t] == peso            # nao nasce uma quarta lista
    ks = list(N.TERMOS_GERAL)
    for a in ks:
        for b in ks:
            assert a == b or a not in b, (a, b)


# ------------------------------------------------------------ banco

def _banco() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    B.preparar(c)
    return c


def test_gravar_e_idempotente_e_deduplica_por_titulo():
    c = _banco()
    ld = N.ler_yahoo_busca(_f("yahoo_search_mu.json"), "MU", UNIVERSO, AGORA)
    # a fixture REAL traz "Earnings Outlook Remains Upbeat" duas vezes, com
    # links diferentes: vira uma linha
    titulos = [it.titulo_norm for it in ld.itens]
    assert len(set(titulos)) < len(titulos)
    n1 = B.gravar(c, ld, AGORA)
    assert n1 == len(set(titulos))
    assert B.gravar(c, ld, AGORA + 1800) == 0          # segundo ciclo: nada novo
    assert c.execute("select count(*) from noticia").fetchone()[0] == n1


def test_mesma_materia_de_outra_fonte_so_acrescenta_simbolo():
    c = _banco()
    it = N.Item("yahoo_busca", "Micron beats estimates", "https://y/1",
                AGORA - 100, "Zacks", ("MU",))
    B.gravar(c, N.Lida("yahoo_busca", "MU", True, [it]), AGORA)
    it2 = N.Item("google_news", "Micron Beats Estimates!", "https://g/9",
                 AGORA - 90, "Reuters", ("SNDK",))
    assert B.gravar(c, N.Lida("google_news", "SNDK", True, [it2]), AGORA) == 0
    (linha,) = B.ler(c, agora=AGORA)
    assert linha["simbolos"] == ["MU", "SNDK"]


def test_outra_fonte_que_nomeia_promove_o_no_titulo():
    c = _banco()
    a = N.Item("yahoo_busca", "Chip stocks drop", "https://y/2", AGORA - 50,
               None, ("MU",), 5, (), ())
    b = N.Item("google_news", "Chip Stocks Drop", "https://g/2", AGORA - 40,
               None, ("MU",), 1, (), ("MU",))
    B.gravar(c, N.Lida("yahoo_busca", "MU", True, [a]), AGORA)
    B.gravar(c, N.Lida("google_news", "MU", True, [b]), AGORA)
    assert B.ler(c, agora=AGORA)[0]["nomeia"] == ["MU"]


def test_ler_filtra_janela_simbolo_e_evento():
    c = _banco()
    B.gravar(c, N.ler_yahoo_busca(_f("yahoo_search_mu.json"), "MU",
                                  UNIVERSO, AGORA), AGORA)
    todas = B.ler(c, agora=AGORA, horas=24 * 30, limite=50)
    # as 20 da MU cobrem so ~6 h (acao muito coberta): o corte que discrimina
    # e de 1 h
    curtas = B.ler(c, agora=AGORA, horas=1, limite=50)
    assert 0 < len(curtas) < len(todas)
    assert all(r["idade_h"] <= 1 for r in curtas)
    assert B.ler(c, agora=AGORA, simbolo="AAPL", horas=24 * 30) == []
    ev = B.ler(c, agora=AGORA, horas=24 * 30, so_eventos=True, limite=50)
    assert all(r["eventos"] for r in ev)
    assert [r["publicado_em"] for r in todas] == sorted(
        (r["publicado_em"] for r in todas), reverse=True)


def test_falha_vira_linha_de_leitura_e_marca_sem_leitura():
    c = _banco()
    B.gravar(c, N.Lida("yahoo_busca", "MU", True, []), AGORA - 1800)
    B.gravar(c, N.Lida("yahoo_busca", "MU", False, erro="HTTP 429"), AGORA)
    est = B.ultima_leitura(c)["MU"]
    assert est["sem_leitura"] and est["ultima_ok"] == AGORA - 1800


# ------------------------------------------------------------ o job, sem rede

def test_coletar_desce_a_escada_so_quando_falha(monkeypatch):
    import defime.jobs.noticias as J

    chamadas = []

    def falso_get(url):
        chamadas.append(url)
        if "search" in url:
            return '{"news": []}' if "QUBT" in url else _f("yahoo_search_mu.json")
        return _f("yahoo_rss_qubt.xml")

    monkeypatch.setattr(J, "_get", falso_get)
    monkeypatch.setattr(J.N, "agora_utc", lambda: AGORA)
    c = _banco()
    r = J.coletar(c, ["MU", "QUBT"], pausa_s=0)
    assert r["sem_fonte"] == []
    assert sum("search" in u for u in chamadas) == 2
    assert sum("feeds.finance" in u for u in chamadas) == 1   # so a QUBT
    assert not any("news.google" in u for u in chamadas)
    fontes = {f for (f,) in c.execute(
        "select fonte from noticia_leitura where simbolo='QUBT'")}
    assert fontes == {"yahoo_busca", "yahoo_rss"}              # a falha ficou


def test_sem_nenhuma_fonte_e_dito(monkeypatch):
    import defime.jobs.noticias as J

    def cai(url):
        raise TimeoutError("sem rede")

    monkeypatch.setattr(J, "_get", cai)
    c = _banco()
    r = J.coletar(c, ["USAR"], pausa_s=0)
    assert r["sem_fonte"] == ["USAR"] and r["novas"] == 0
    assert c.execute("select count(*) from noticia_leitura").fetchone()[0] == 3


def test_simbolos_sem_cripto_e_com_spcx():
    import defime.jobs.noticias as J
    assert "SPCX" in J.SIMBOLOS and not any(s.endswith("-USD") for s in J.SIMBOLOS)
    assert set(J.SIMBOLOS) <= set(N.NOMES)      # todo simbolo tem ultimo degrau


# ------------------------------------------------------------ fronteira

def test_decision_nao_importa_noticias():
    for arq in (RAIZ / "src" / "defime" / "decision").rglob("*.py"):
        arvore = ast.parse(arq.read_text(encoding="utf-8"))
        for no in ast.walk(arvore):
            nomes = []
            if isinstance(no, ast.Import):
                nomes = [a.name for a in no.names]
            elif isinstance(no, ast.ImportFrom):
                nomes = [no.module or ""] + [a.name for a in no.names]
            assert not any("noticias" in n for n in nomes), arq


def test_tradingview_fora_da_coleta():
    fonte = (RAIZ / "src" / "defime" / "jobs" / "noticias.py").read_text(encoding="utf-8")
    codigo = "\n".join(l for l in fonte.splitlines()
                       if not l.lstrip().startswith("#"))
    assert "tradingview.com" not in codigo.split('"""', 2)[-1]


@pytest.mark.parametrize("nome", ["noticias_ativos.py"])
def test_modulo_puro_sem_rede(nome):
    src = (RAIZ / "src" / "defime" / "macro" / nome).read_text(encoding="utf-8")
    assert "urllib.request" not in src and "requests" not in src


# ------------------------------------------------------------ estado do MCP (N3)

def test_estado_noticias_carrega_leitura_e_nada_de_corpo():
    import defime.jobs.noticias as J
    c = _banco()
    B.gravar(c, N.ler_yahoo_busca(_f("yahoo_search_mu.json"), "MU",
                                  UNIVERSO, AGORA), AGORA)
    B.gravar(c, N.Lida("yahoo_busca", "QUBT", False, erro="HTTP 429"), AGORA)
    e = J.estado_noticias(c, AGORA, ["MU", "QUBT"])
    assert e["entra_na_decisao"] is False and e["janela_h"] == 72
    assert e["leitura"]["QUBT"]["sem_leitura"] is True
    assert e["noticias"] and all(set(m) == {
        "titulo", "publisher", "link", "publicado_em", "fonte", "eventos",
        "simbolos", "nomeia", "n_tickers"} for m in e["noticias"])
    json.dumps(e)                                   # serializavel


def test_estado_sai_do_banco_e_nao_da_rodada():
    src = (RAIZ / "src" / "defime" / "jobs" / "noticias.py").read_text(encoding="utf-8")
    assert "sorted(set(SIMBOLOS) | set(simbolos))" in src
