"""A pagina de grafico: o que ela publica e o que ela se recusa a esconder.

Tres testes aqui nasceram de defeito medido durante a construcao, e sao os que
valem mais que os de formatacao:

1. **`window.DADOS` e nao `const DADOS`.** `const` no escopo global cria uma
   binding lexical e nao uma propriedade de `window`; o guarda do desenho saia
   cedo, sem erro no console, e a pagina publicava uma moldura vazia — que
   este projeto ja aprendeu a ler como medicao.
2. **O corte de janela acontece DEPOIS do calculo.** Invertido, a SMA 200 e o
   canal de 144 aqueceriam dentro da janela visivel e o comeco do desenho
   sairia sem media longa.
3. **Serie sem amostra para um indicador e DITA.** Uma serie semanal tem ~150
   barras e a SMA 200 sai inteira nula; botao que acende sem mudar a tela e
   indistinguivel de defeito.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import re
import sqlite3
import sys
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[1]
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from defime.web import grafico_render as G                                       # noqa: E402
from defime.jobs import grafico as J                                    # noqa: E402
from defime.macro.acoes import Barra, agregar, agrupar_por       # noqa: E402


# 14:00 UTC e uma hora de pregao (9:30-16:00 ET). **O inicio tem de cair
# dentro do dia**: a primeira versao deste gerador comecava as 22:13 UTC e a
# terceira barra ja virava o dia, entao `agregar(quebrar_no_dia=True)` partia
# o "pregao" em dois e o teste acusava o agregador. O agregador estava certo.
_PREGAO_UTC = dt.datetime(2024, 1, 2, 14, 0, tzinfo=dt.UTC)


def _barras(n=600, inicio=None):
    """Barras sinteticas com dia de pregao de 7 horas, deterministicas."""
    inicio = int((inicio or _PREGAO_UTC).timestamp())
    out, x = [], 100.0
    for i in range(n):
        x *= 1 + 0.003 * math.sin(i * 0.37) + 0.0009 * math.cos(i * 1.7)
        # 7 barras por dia; o resto do dia nao existe, como no pregao real
        dia, hora = divmod(i, 7)
        ts = inicio + dia * 86400 + hora * 3600
        out.append(Barra(ts=ts, abertura=x * 0.999, maxima=x * 1.004,
                         minima=x * 0.996, fechamento=x, volume=1000 + i))
    return out


# -------------------------------------------------------------- alinhamento

def test_serie_traz_todo_indicador_com_o_comprimento_dos_candles():
    d = G._serie_de(_barras(400))
    n = len(d["t"])
    for k, arr in d.items():
        if isinstance(arr, list) and not k.startswith("_"):
            assert len(arr) == n, k


def test_serie_recusa_array_desalinhado_em_vez_de_publicar(monkeypatch):
    """O gerador confere o proprio trabalho: indicador curto desenharia
    deslocado sobre os candles, sem levantar nada."""
    monkeypatch.setattr(G.I, "rsi", lambda c, *a, **k: [None] * (len(c) - 1))
    with pytest.raises(ValueError, match="rsi"):
        G._serie_de(_barras(200))


# ------------------------------------------------------- o corte de janela

def test_o_corte_acontece_depois_do_calculo():
    """A prova: com corte, o PRIMEIRO ponto visivel ja tem SMA 200 — o que so
    e possivel se ela foi calculada sobre as barras anteriores ao corte."""
    barras = _barras(600)
    cortada = G._serie_de(barras, ultimas=100)
    assert len(cortada["t"]) == 100
    assert cortada["sma200"][0] is not None
    assert cortada["gcm"][0] is not None

    # e o corte nao muda valor nenhum: e a mesma serie, fatiada
    inteira = G._serie_de(barras)
    assert cortada["sma200"] == inteira["sma200"][-100:]
    assert cortada["c"] == inteira["c"][-100:]


def test_o_corte_publica_de_quantas_barras_ele_saiu():
    """Publicar "2.000 barras" sem dizer que ha 5.081 deixaria o leitor
    concluir que a serie acaba ali."""
    d = G._serie_de(_barras(600), ultimas=100)
    assert d["_cortada_de"] == 600
    assert "_cortada_de" not in G._serie_de(_barras(60), ultimas=100)


def test_o_rodape_diz_o_total_quando_cortou():
    series = {"1h": G._serie_de(_barras(600), ultimas=100)}
    html = G.bloco("MU", series, {"velha": False, "ultima": None})
    assert "as mais recentes de 600" in html


# ---------------------------------------------- ausencia de amostra e DITA

def test_serie_curta_declara_quais_indicadores_ficaram_sem_amostra():
    d = G._serie_de(_barras(120))          # < 200 barras
    assert "sma200" in d["_vazias"]
    assert "c" not in d["_vazias"]


def test_serie_longa_nao_declara_falta_do_que_tem():
    d = G._serie_de(_barras(600))
    assert d["_vazias"] == []


# ------------------------------------------------ o bug da tela em branco

def test_os_dados_vao_em_window_para_o_desenho_enxergar():
    """`const DADOS` no escopo global nao vira `window.DADOS`, e o desenho
    saia calado. Este teste e o que impede a volta."""
    js = G._js({"simbolo": "MU", "series": {}})
    assert "window.DADOS =" in js
    assert not re.search(r"\bconst DADOS\b", js)


def test_sem_dados_o_desenho_diz_o_motivo_em_vez_de_deixar_a_moldura_vazia():
    corpo = G._JS_CORPO
    assert "function morrer(" in corpo
    assert "nao desenhou" in corpo
    # o guarda de dados ausentes tem de CHAMAR `morrer`, nao so voltar
    trecho = corpo[corpo.index("if(!window.DADOS"):][:220]
    assert "morrer(" in trecho


# ------------------------------------------- a fronteira: o JS nao calcula

# Assinaturas das contas que moram no Python. Nao e prova formal — e um alarme
# barato contra a segunda implementacao voltar por descuido, que e o que o
# `compor.js` precisou de portao inteiro para controlar.
_PROIBIDO_NO_JS = [
    r"function\s+rsi\s*\(", r"function\s+ema\s*\(",
    r"function\s+sma\s*\(", r"function\s+macd\s*\(",
    r"function\s+bollinger\s*\(", r"function\s+donchian\s*\(",
    r"function\s+gauss\w*\s*\(", r"pstdev", r"trueRange",
]


def test_js_nao_recalcula_indicador():
    corpo = G._JS_CORPO
    achados = [p for p in _PROIBIDO_NO_JS if re.search(p, corpo)]
    assert not achados, (
        "o JS voltou a calcular indicador: " + ", ".join(achados)
        + ". Os numeros nascem em macro/indicadores.py.")


# ------------------------------------------------------- guarda de idade

def test_serie_parada_e_declarada_com_as_horas():
    barras = _barras(100)
    agora = dt.datetime.fromtimestamp(barras[-1].ts, dt.UTC) + dt.timedelta(
        hours=G.IDADE_MAXIMA_H + 1)
    idade = G._idade(barras, agora)
    assert idade["velha"] is True
    html = G.bloco("MU", {"1h": G._serie_de(barras)}, idade)
    assert "serie esta parada" in html or "série está parada" in html


def test_serie_fresca_nao_leva_aviso():
    barras = _barras(100)
    agora = dt.datetime.fromtimestamp(barras[-1].ts, dt.UTC) + dt.timedelta(
        hours=2)
    assert G._idade(barras, agora)["velha"] is False


# ------------------------------------------------------------- timeframes

def test_os_seis_timeframes_saem_e_o_diario_tem_um_bloco_por_dia():
    barras = _barras(70)                    # 10 dias de 7 barras
    s = J.series_de(barras, janela_1h=None)
    assert set(s) == {"1h", "4h", "1d", "3d", "5d", "1sem"}
    assert len(s["1d"]["t"]) == 10
    assert len(s["3d"]["t"]) == 4           # 3+3+3+1
    assert len(s["5d"]["t"]) == 2


def test_o_diario_nao_junta_dois_pregoes_num_candle():
    """`por=99` com quebra no dia: um dia com 8 barras continua sendo UM
    candle, e dois dias nunca caem no mesmo."""
    barras = _barras(70)
    diarias = agregar(barras, por=J.POR_DIA, quebrar_no_dia=True)
    for b in diarias:
        assert b.barras == 7
    dias = [dt.datetime.fromtimestamp(b.ts, dt.UTC).date() for b in diarias]
    assert len(dias) == len(set(dias))


def test_semana_de_calendario_e_cinco_pregoes_nao_sao_a_mesma_coisa():
    """Na semana curta o bloco de calendario fecha com menos barras; o de
    contagem atravessa e junta cinco. Publicar um com o nome do outro seria
    rotular errado."""
    # 4 dias na primeira semana (segunda a quinta) e 5 na seguinte
    inicio = int(dt.datetime(2026, 1, 5, 14, 30, tzinfo=dt.UTC).timestamp())
    diarias = []
    for i in range(9):
        dia = i if i < 4 else i + 3        # pula sexta/sab/dom da 1a semana
        diarias.append(Barra(ts=inicio + dia * 86400, abertura=1.0, maxima=1.0,
                             minima=1.0, fechamento=1.0, volume=1))
    por_semana = agrupar_por(diarias, J._semana)
    por_cinco = agregar(diarias, por=5, quebrar_no_dia=False)
    assert [b.barras for b in por_semana] == [4, 5]
    assert [b.barras for b in por_cinco] == [5, 4]
    assert por_semana[0].barras != por_cinco[0].barras


def test_agrupar_por_de_lista_vazia_devolve_vazio():
    assert agrupar_por([], J._semana) == []


# ------------------------------------------------------- a pagina inteira

def test_a_pagina_publica_as_ressalvas_que_ela_nao_pode_omitir():
    html = J.montar("MU", _barras(600), css="",
                    agora=dt.datetime(2026, 9, 22, tzinfo=dt.UTC))
    for frase in ["preço da BOLSA", "1,42%", "POR_QUE_NAO",
                  "número da barra", "página de composição"]:
        assert frase in html, frase


def test_a_pagina_carrega_json_valido():
    html = J.montar("MU", _barras(300), css="")
    bruto = html[html.index("window.DADOS = ") + len("window.DADOS = "):]
    dados = json.loads(bruto[:bruto.index(";\n")])
    assert dados["simbolo"] == "MU"
    assert set(dados["series"]) == {"1h", "4h", "1d", "3d", "5d", "1sem"}


def test_casas_decimais_seguem_o_nivel_do_preco():
    assert G._casas_de([958.0, 960.0]) == 3        # acao em centenas
    assert G._casas_de([0.5, 0.52]) == 6           # token em centavos
    assert G._casas_de([]) == 4                    # sem dado, o default


def test_barras_do_banco_ignora_fechamento_nulo(tmp_path):
    bd = tmp_path / "t.db"
    c = sqlite3.connect(bd)
    c.execute("create table acoes_ohlc(simbolo text, intervalo text, ts int,"
              " abertura real, maxima real, minima real, fechamento real,"
              " volume int)")
    c.executemany("insert into acoes_ohlc values (?,?,?,?,?,?,?,?)", [
        ("MU", "1h", 100, 1.0, 1.0, 1.0, 1.0, 5),
        ("MU", "1h", 200, 1.0, 1.0, 1.0, None, 5),
        ("MU", "1h", 300, 1.0, 1.0, 1.0, 2.0, 5),
    ])
    c.commit()
    c.close()
    b = J.barras_do_banco(str(bd), "MU")
    assert [x.ts for x in b] == [100, 300]


# ------------------------------------------------------ onde cada um mora

def test_o_primeiro_simbolo_mora_na_raiz_e_os_outros_em_subpasta():
    """O link que se guarda e `/grafico/`, nao `/grafico/mu/`."""
    simbolos = ["MU", "USO", "RDDT"]
    assert J.destino_de("/srv/site/grafico", simbolos, "MU") == Path(
        "/srv/site/grafico")
    assert J.destino_de("/srv/site/grafico", simbolos, "RDDT") == Path(
        "/srv/site/grafico/rddt")


def test_as_abas_apontam_certo_dos_dois_niveis():
    """O primeiro esta um nivel acima dos outros: um caminho fixo quebraria
    metade dos links, e link quebrado nao levanta nada."""
    simbolos = ["MU", "USO"]
    da_raiz = J._abas(simbolos, "MU")
    assert 'href="uso/"' in da_raiz and "<b>MU</b>" in da_raiz
    de_dentro = J._abas(simbolos, "USO")
    assert 'href="../"' in de_dentro and "<b>USO</b>" in de_dentro


def test_simbolo_unico_nao_publica_barra_de_abas():
    assert J._abas(["MU"], "MU") == ""
