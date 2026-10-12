"""Fase N2 do PLAN_NOTICIAS — manchetes no `/heat/` e no `/grafico/`. Sem rede."""

from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path
from types import SimpleNamespace as NS

from defime.web import noticias_render as NR
from defime.macro import noticias_ativos as N
from defime.macro import noticias_banco as B

RAIZ = Path(__file__).resolve().parents[1]
FIX = RAIZ / "tests" / "fixtures" / "noticias"
AGORA = int(dt.datetime(2026, 9, 23, 22, 0, tzinfo=dt.UTC).timestamp())


def _tile(sim="MU", var=5.0, w=16.0, h=12.0):
    return NS(simbolo=sim, variacao_24h_pct=var, x=0.0, y=0.0,
              largura=w, altura=h)


def _m(titulo, nomeia=("MU",), eventos=(), idade=1.0, link="https://x/1"):
    return {"titulo": titulo, "link": link, "publisher": "Zacks",
            "fonte": "yahoo_busca", "idade_h": idade, "eventos": list(eventos),
            "nomeia": list(nomeia), "simbolos": ["MU"]}


VIVO = {"ultima_ok": AGORA, "ultima_tentativa": AGORA, "sem_leitura": False}
MUDO = {"ultima_ok": AGORA - 7200, "ultima_tentativa": AGORA,
        "sem_leitura": True}


# ------------------------------------------------------------------ estado

def test_estado_distingue_nao_coletado_mudo_e_zero():
    assert NR.estado("SGOV", None, None)["coletado"] is False
    assert NR.estado("MU", [], MUDO)["sem_leitura"] is True
    zero = NR.estado("MU", [], VIVO)
    assert zero["n"] == 0 and not zero["sem_leitura"]


def test_evento_conta_so_se_a_manchete_nomeia():
    ms = [_m("Micron guidance", eventos=["guidance"]),
          _m("General Mills guidance", nomeia=(), eventos=["guidance"])]
    assert NR.estado("MU", ms, VIVO)["eventos"] == 1


# ------------------------------------------------------------------ selo

def test_selo_mostra_contagem_e_evento():
    ms = [_m("a", eventos=["recall"]), _m("b")]
    s = NR.selo_svg(_tile(), NR.estado("MU", ms, VIVO))
    assert ">2 ●</text>" in s and 'class="selo ev"' in s
    assert "AÇÃO" in s                      # a noticia e da acao, e diz


def test_selo_mudo_e_interrogacao_nunca_zero():
    s = NR.selo_svg(_tile(), NR.estado("MU", [], MUDO))
    assert ">?</text>" in s and "nenhuma fonte" in s
    assert ">0</text>" not in s


def test_sem_coleta_ou_tile_pequeno_nao_tem_selo():
    assert NR.selo_svg(_tile(), NR.estado("SGOV", None, None)) == ""
    assert NR.selo_svg(_tile(w=4, h=3), NR.estado("MU", [_m("a")], VIVO)) == ""
    assert NR.selo_svg(_tile(), None) == ""


# ------------------------------------------------------------------ o que moveu

def test_o_que_moveu_ordena_por_magnitude_e_prefere_quem_nomeia():
    tiles = [_tile("SPY", 0.1), _tile("RBLX", -2.7), _tile("MU", 5.0),
             _tile("QUBT", None)]
    ms = [_m("Resumo de mercado", nomeia=(), link="https://x/r", idade=0.5),
          _m("Micron sobe", link="https://x/m", idade=2.0)]
    est = {"MU": NR.estado("MU", ms, VIVO),
           "RBLX": NR.estado("RBLX", [], VIVO),
           "SPY": NR.estado("SPY", [], MUDO)}
    h = NR.o_que_moveu(tiles, {"MU": ms}, est, {"MU": "/grafico/mu/"}, n=3)
    assert h.index("<b>MU</b>") < h.index("<b>RBLX</b>") < h.index("<b>SPY</b>")
    assert "QUBT" not in h                          # sem variacao nao entra
    assert h.index("Micron sobe") < h.index("Resumo de mercado")
    assert ">cita</span>" in h
    assert "+5,00%" in h and "−2,70%" in h and "-2,70%" not in h
    # zero com fonte viva e "nenhuma"; fonte muda e "sem leitura", nunca 0
    assert "nenhuma manchete em 24 h (a fonte respondeu)" in h
    assert "sem leitura, não é ausência" in h


def test_links_externos_sao_seguros_e_escapados():
    ms = [_m('<script>alert(1)</script>', link='https://x/"a')]
    h = NR.o_que_moveu([_tile()], {"MU": ms}, {"MU": NR.estado("MU", ms, VIVO)},
                       {})
    assert "<script>alert" not in h and "&lt;script&gt;" in h
    assert 'href="https://x/&quot;a"' in h
    assert h.count('rel="noopener nofollow"') == h.count('target="_blank"')


def test_tradingview_so_como_link_e_nunca_para_a_spcx():
    assert NR.link_tradingview("MU") == "https://www.tradingview.com/symbols/MU/"
    assert NR.link_tradingview("SPCX") is None


# ------------------------------------------------------------------ lista

def test_lista_da_pagina_de_grafico():
    ms = [_m("cita", nomeia=(), link="https://x/c"), _m("Micron beats",
                                                       eventos=["beats"])]
    h = NR.lista("MU", ms, NR.estado("MU", ms, VIVO))
    assert h.index("Micron beats") < h.index(">cita</a>")
    assert "TradingView" in h and "nenhuma entra em decisão" in h
    vazio = NR.lista("MU", [], NR.estado("MU", [], MUDO))
    assert "fonte muda desde" in vazio and "<li>" not in vazio


def test_selo_ancora_no_fim_pelo_css():
    # o `.tile text{text-anchor:middle}` do heatmap vence o atributo SVG
    assert ".tile .selo{text-anchor:end" in NR.CSS


def test_render_nao_carrega_script_de_terceiro():
    fonte = (RAIZ / "src" / "defime" / "web" / "noticias_render.py").read_text(encoding="utf-8")
    assert "<script" not in fonte and "sqlite3" not in fonte


# ------------------------------------------------------------------ jobs

def _banco(tmp_path) -> str:
    bd = str(tmp_path / "t.db")
    c = sqlite3.connect(bd)
    B.preparar(c)
    ld = N.ler_yahoo_busca((FIX / "yahoo_search_mu.json").read_text("utf-8"),
                           "MU", {"MU", "COST"}, AGORA)
    B.gravar(c, ld, AGORA)
    c.commit()
    c.close()
    return bd




def test_grafico_usa_a_mesma_leitura(tmp_path):
    import defime.jobs.grafico as J
    h = J.noticias_bloco(_banco(tmp_path), "MU",
                         dt.datetime.fromtimestamp(AGORA, dt.UTC))
    assert "<h2>Notícias</h2>" in h and "<li>" in h and "/heat/" in h


