"""O job do heatmap: leitura do banco, links e o texto que cabe no tile.

**O teste do link e o que mais importa aqui.** Um href montado por convencao
de nome (`/grafico/<sym>/`) parece certo, passa em revisao e produz 404 para
SPCX e SGOV — tokens que negociam na chain e nao tem acao na bolsa. Link
quebrado nao levanta nada e ninguem reclama; o tile so nao leva a lugar
nenhum.
"""

from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

import pytest

from defime import heatmap as H                                  # noqa: E402
from defime.jobs import heatmap as J                             # noqa: E402
from defime.web import heatmap_render as R                       # noqa: E402

AGORA = 1_760_000_000


def _banco(tmp_path, pools, simbolos=None, acoes=("MU", "NVDA")):
    """Banco minimo com as tres tabelas que o job exige."""
    bd = tmp_path / "t.db"
    c = sqlite3.connect(bd)
    c.executescript("""
      create table pool_hora(pool text, ts int, preco real, tvl_usd real,
                             volume_usd real);
      create table pool_param(pool text, usdg0 int, token0 text, token1 text,
                              spacing int, taxa real, versao text,
                              primeiro_ts int);
      create table token_simbolo(endereco text, simbolo text, decimais int,
                                 visto_em int);
      create table acoes_ohlc(simbolo text, intervalo text, ts int,
                              abertura real, maxima real, minima real,
                              fechamento real, volume int);
    """)
    for s in (simbolos or {}):
        c.execute("insert into token_simbolo values (?,?,?,?)",
                  (s, (simbolos or {})[s], 18, AGORA))
    for p in pools:
        c.execute("insert into pool_param values (?,?,?,?,?,?,?,?)",
                  (p["pool"], 1, "0xusdg", p["token"], 60, 0.003, "v3", 0))
        for ts, preco, tvl, vol in p["pontos"]:
            c.execute("insert into pool_hora values (?,?,?,?,?)",
                      (p["pool"], ts, preco, tvl, vol))
    for a in acoes:
        # dois pregoes de 7 barras: o de ontem fecha em 100, o de hoje em 102
        for d, fech in ((1, 100.0), (0, 102.0)):
            base = AGORA - d * 86400 - 6 * 3600
            for h in range(7):
                c.execute(
                    "insert into acoes_ohlc values (?,?,?,?,?,?,?,?)",
                    (a, "1h", base + h * 3600, fech, fech, fech, fech, 1000))
    c.commit()
    c.close()
    return str(bd)


def _pontos(p0, p1, n=25, tvl=1e6, vol=1000.0, ate=AGORA):
    """n pontos horarios terminando agora, de p0 ate p1."""
    out = []
    for i in range(n):
        f = i / max(1, n - 1)
        out.append((ate - (n - 1 - i) * 3600, p0 + (p1 - p0) * f, tvl, vol))
    return out


# --------------------------------------------------------- leitura do banco

def test_le_preco_tvl_volume_e_variacao_de_24h(tmp_path):
    bd = _banco(tmp_path,
                [{"pool": "0xa", "token": "0xmu",
                  "pontos": _pontos(100.0, 110.0, tvl=2e6, vol=500.0)}],
                simbolos={"0xmu": "MU"})
    d = J._serie_por_pool(bd, AGORA)["0xa"]
    assert d["simbolo"] == "MU"
    assert d["preco"] == pytest.approx(110.0)
    assert d["tvl_usd"] == pytest.approx(2e6)
    assert d["variacao_24h_pct"] == pytest.approx(10.0, rel=1e-6)
    # o volume soma SO as ultimas 24 h, nao as 30 h lidas
    assert d["volume_24h_usd"] == pytest.approx(500.0 * 24)


def test_pool_parada_fica_de_fora(tmp_path):
    """Publicar o preco de ontem ao lado do de agora e a familia numero um
    deste projeto."""
    velho = AGORA - int((J.IDADE_MAXIMA_H + 2) * 3600)
    bd = _banco(tmp_path, [{"pool": "0xa", "token": "0xmu",
                            "pontos": _pontos(100.0, 110.0, ate=velho)}],
                simbolos={"0xmu": "MU"})
    assert J._serie_por_pool(bd, AGORA) == {}


def test_um_unico_ponto_nao_vira_variacao_zero(tmp_path):
    bd = _banco(tmp_path, [{"pool": "0xa", "token": "0xmu",
                            "pontos": [(AGORA - 600, 100.0, 1e6, 10.0)]}],
                simbolos={"0xmu": "MU"})
    d = J._serie_por_pool(bd, AGORA)["0xa"]
    assert d["variacao_24h_pct"] is None
    assert d["motivo"]


def test_falta_de_tabela_diz_qual_e_como_preencher(tmp_path):
    bd = tmp_path / "vazio.db"
    sqlite3.connect(bd).close()
    with pytest.raises(RuntimeError, match="token_simbolo"):
        J._serie_por_pool(str(bd), AGORA)


# ----------------------------------------------------------------- links

def test_so_vira_link_o_simbolo_com_pagina_no_disco(tmp_path):
    bd = _banco(tmp_path, [], simbolos={}, acoes=("MU", "NVDA", "SPY"))
    g = tmp_path / "grafico"
    (g / "nvda").mkdir(parents=True)
    (g / "nvda" / "index.html").write_text(
        "<title>NVDA — grafico</title>", encoding="utf-8")
    links = J.links_de_grafico(bd, g, ["MU", "NVDA", "SPY", "SPCX"])
    assert links == {"NVDA": "/grafico/nvda/"}


def test_o_simbolo_da_RAIZ_e_descoberto_pelo_title(tmp_path):
    """O `jobs/grafico.py` poe o PRIMEIRO simbolo na raiz; adivinhar qual foi
    daria link errado para o vizinho."""
    bd = _banco(tmp_path, [], acoes=("MU", "NVDA"))
    g = tmp_path / "grafico"
    g.mkdir()
    (g / "index.html").write_text(
        "<!doctype html><title>MU — grafico</title>", encoding="utf-8")
    links = J.links_de_grafico(bd, g, ["MU", "NVDA"])
    assert links == {"MU": "/grafico/"}


def test_simbolo_sem_serie_de_bolsa_nunca_vira_link(tmp_path):
    bd = _banco(tmp_path, [], acoes=("MU",))
    g = tmp_path / "grafico"
    (g / "spcx").mkdir(parents=True)
    (g / "spcx" / "index.html").write_text(
        "<title>SPCX — grafico</title>", encoding="utf-8")
    assert J.links_de_grafico(bd, g, ["SPCX"]) == {}


# ------------------------------------------------------- a pagina inteira

def _bd_completo(tmp_path):
    pools = [
        {"pool": "0xnv", "token": "0xnvda",
         "pontos": _pontos(100.0, 100.4, tvl=5.7e6, vol=2e5)},
        {"pool": "0xsp1", "token": "0xspy",
         "pontos": _pontos(600.0, 601.0, tvl=4.3e6, vol=1e4)},
        {"pool": "0xsp2", "token": "0xspy",
         "pontos": _pontos(600.0, 599.0, tvl=1.2e6, vol=3e4)},
        {"pool": "0xuso", "token": "0xusot",
         "pontos": _pontos(150.0, 145.6, tvl=1.6e6, vol=9e4)},
        {"pool": "0xmu", "token": "0xmut",
         "pontos": _pontos(100.0, 103.9, tvl=9.6e5, vol=1.4e4)},
    ]
    return _banco(tmp_path, pools,
                  simbolos={"0xnvda": "NVDA", "0xspy": "SPY",
                            "0xusot": "USO", "0xmut": "MU"},
                  acoes=("NVDA", "SPY", "USO", "MU"))


def test_a_pagina_sai_com_os_tiles_e_as_ressalvas(tmp_path):
    html, resumo = J.montar(_bd_completo(tmp_path), top=10, agora=AGORA)
    assert resumo["tokens"] == 4
    achatado = " ".join(html.split())
    for frase in ["Heatmap dos tokens", "pool dominante", "a terceira é a",
                  "1,42%", "NVDA", "SPY", "623×"]:
        assert frase in achatado, frase


def test_as_duas_visoes_tem_LAYOUTS_diferentes(tmp_path):
    """`H.montar` grava o retangulo dentro do Token: reusar a mesma lista
    faria a segunda visao sobrescrever a primeira e os dois SVG sairiam
    identicos, com o rotulo dizendo que sao diferentes."""
    html, _ = J.montar(_bd_completo(tmp_path), top=10, agora=AGORA)
    a = html[html.index('id="mapa-tvl"'):html.index('id="mapa-volume"')]
    b = html[html.index('id="mapa-volume"'):html.index('id="mapa-bolsa"')]
    assert a.count("<rect") == b.count("<rect") > 0
    assert a != b


def test_o_corte_do_top_e_dito_na_pagina(tmp_path):
    html, resumo = J.montar(_bd_completo(tmp_path), top=2, agora=AGORA)
    assert resumo["tokens"] == 2
    assert sorted(resumo["fora"]) == ["MU", "USO"]
    assert "Ficaram de fora" in html and "MU" in html


def test_o_layout_usa_o_viewBox_do_render(tmp_path):
    """Calcular o treemap num retangulo e desenhar em outro distorceria as
    razoes de aspecto que o squarified existe para otimizar."""
    html, _ = J.montar(_bd_completo(tmp_path), top=10, agora=AGORA)
    assert f'viewBox="0 0 {R.VB_L:g} {R.VB_A:g}"' in html


def test_tile_sem_link_nao_vira_ancora(tmp_path):
    """Sem pagina de grafico o tile continua no mapa, sem href."""
    bd = _banco(tmp_path,
                [{"pool": "0xa", "token": "0xspcx",
                  "pontos": _pontos(10.0, 10.1, tvl=2e6)}],
                simbolos={"0xspcx": "SPCX"}, acoes=("MU",))
    html, _ = J.montar(bd, top=10, agora=AGORA)
    assert "SPCX" in html
    assert 'href="/grafico/spcx/"' not in html
    assert "sem gráfico" in html


# --------------------------------------------------- o texto dentro do tile

def _tok(simbolo, x, y, w, h, var=1.0, preco=100.0):
    t = H.Token(simbolo=simbolo, tvl_usd=1.0, volume_24h_usd=1.0, preco=preco,
                variacao_24h_pct=var, pool_dominante="0xa", pools=1,
                horas_cobertas=24.0)
    t.x, t.y, t.largura, t.altura = x, y, w, h
    return t


def _linhas_y(svg: str):
    import re
    return [float(m) for m in re.findall(r'<text x="[\d.]+" y="([\d.]+)"', svg)]


@pytest.mark.parametrize("w,h", [(40, 30), (20, 14), (12, 9), (8, 5),
                                 (30, 4), (4, 20)])
def test_o_texto_nunca_sai_do_tile(w, h):
    """O preco do USO saiu POR BAIXO da borda na primeira versao: o tile
    passava no limiar de altura e a terceira linha caia fora mesmo assim."""
    t = _tok("GOOGL", 10.0, 20.0, w, h)
    svg = R._texto(t, {}, 3.0)
    for y in _linhas_y(svg):
        assert t.y <= y <= t.y + h, f"linha em y={y} fora de [{t.y}, {t.y+h}]"


def test_tile_minusculo_nao_desenha_texto_transbordando():
    assert R._texto(_tok("GOOGL", 0, 0, 1.5, 1.0), {}, 3.0) == ""


def test_o_texto_encolhe_por_ORDEM_conforme_o_tile_diminui():
    """Sai o preco primeiro, depois a variacao; o simbolo e o ultimo a cair,
    porque um tile so com o nome ainda informa tamanho e identidade."""
    # tile grande: simbolo + variacao + preco
    assert len(_linhas_y(R._texto(_tok("MU", 0, 0, 40, 30), {}, 3.0))) == 3
    # achatado: a terceira linha nao cabe na ALTURA
    assert len(_linhas_y(R._texto(_tok("MU", 0, 0, 14, 7.0), {}, 3.0))) == 2
    # estreito e alto: a variacao nao cabe na LARGURA e so o simbolo fica
    estreito = R._texto(_tok("MU", 0, 0, 3.8, 18.0, var=-10.0), {}, 3.0)
    assert len(_linhas_y(estreito)) == 1
    # e o corte TRUNCA: o preco, mais curto, nao pode sobreviver a variacao
    assert "$" not in estreito


def test_variacao_ausente_pinta_neutro_e_nao_verde_fraco():
    """"Nao sei" na mesma escala de "subiu pouco" e a confusao do zero no
    lugar do ausente."""
    assert R.cor_do_tile(None, 3.0) != R.cor_do_tile(0.0, 3.0)
    assert R.cor_do_tile(None, 3.0) == "#1c2730"


def test_a_cor_satura_no_teto_em_vez_de_estourar():
    assert R.cor_do_tile(5.0, 3.0) == R.cor_do_tile(50.0, 3.0)
    assert R.cor_do_tile(-5.0, 3.0) == R.cor_do_tile(-50.0, 3.0)


# ---------------------------------------------------- a visao da BOLSA

def test_a_bolsa_mede_fechamento_contra_fechamento():
    """Janela de relogio na bolsa ou mede o nada (fim de semana) ou
    atravessa dois pregoes. O numero que se le num heatmap de acao e a
    variacao do pregao."""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        bd = _banco(Path(d), [], acoes=("MU",))
        b = J.serie_da_bolsa(bd, AGORA)["MU"]
    assert b["fonte"] == "bolsa"
    assert b["janela"] == "pregão"
    assert b["preco"] == pytest.approx(102.0)
    assert b["variacao_24h_pct"] == pytest.approx(2.0)      # 100 -> 102


def test_a_area_da_bolsa_e_o_volume_FINANCEIRO(tmp_path):
    """Ordenar por numero de acoes poria uma acao de $17 na frente de uma de
    $270 com o mesmo giro em dolares — o preco unitario nao e tamanho."""
    bd = _banco(tmp_path, [], acoes=("MU",))
    b = J.serie_da_bolsa(bd, AGORA)["MU"]
    assert b["volume_24h_usd"] == pytest.approx(7 * 1000 * 102.0)


def test_bolsa_parada_declara_em_vez_de_publicar_variacao(tmp_path):
    bd = _banco(tmp_path, [], acoes=("MU",))
    futuro = AGORA + int((J.IDADE_BOLSA_H + 5) * 3600)
    b = J.serie_da_bolsa(bd, futuro)["MU"]
    assert b["variacao_24h_pct"] is None
    assert "último fechamento" in b["motivo"]


def _pool_serie(simbolo, tvl, var, horas):
    return {"simbolo": simbolo, "tvl_usd": tvl, "volume_24h_usd": 1000.0,
            "preco": 10.0, "variacao_24h_pct": var, "horas_cobertas": horas}


def test_a_guarda_de_18h_nao_se_aplica_a_bolsa():
    """Um pregao tem 6h30. Aplicar o limiar da pool ali descartaria TODA
    acao, medindo o calendario do mercado em vez da cobertura da serie."""
    t = H.agregar({"MU": {"simbolo": "MU", "tvl_usd": 0.0,
                          "volume_24h_usd": 1e6, "preco": 102.0,
                          "variacao_24h_pct": 2.0, "horas_cobertas": 6.5,
                          "fonte": "bolsa", "janela": "pregão"}})
    assert t[0].variacao_24h_pct == pytest.approx(2.0)
    assert t[0].fonte == "bolsa"
    # e continua valendo para a POOL, que negocia 24/7
    p = H.agregar({"0xa": _pool_serie("MU", 1e6, 2.0, 6.5)})
    assert p[0].variacao_24h_pct is None


def test_a_pagina_traz_as_TRES_visoes_com_legendas_proprias(tmp_path):
    html, resumo = J.montar(_bd_completo(tmp_path), top=10, agora=AGORA)
    for ident in ("mapa-tvl", "mapa-volume", "mapa-bolsa"):
        assert f'id="{ident}"' in html, ident
    assert resumo["bolsa"] >= 1
    # cada aba carrega o SEU teto e a SUA janela: a escala da pool desenhada
    # sobre o mapa da bolsa diria que a escala e uma quando e outra
    assert 'data-janela="pregão"' in html
    assert 'data-janela="24 h"' in html


def test_o_tile_da_bolsa_declara_a_fonte_no_title(tmp_path):
    """"+2,6%" sem dizer se e da pool ou da bolsa e um numero sem a pergunta
    que ele responde — o basis entre as duas chega a 3%."""
    html, _ = J.montar(_bd_completo(tmp_path), top=10, agora=AGORA)
    bolsa = html[html.index('id="mapa-bolsa"'):]
    assert "não é o preço do token na chain" in bolsa


# ------------------------------------------- o preco vem do CONTRATO

# A palavra 1 do `slot0()` e o tick, CONFERIDO contra a chain em 24/09: a
# pool 0xd4eb2120 devolveu 222154 e o `pool_hora` de 59 min antes tinha
# 222151. Escrever o layout de cabeca ja custou "mediana 100,000%" com p10 de
# −2,3 trilhoes por cento num spike de 09/09.
def _slot0(tick: int, sqrt: int = 5_280_320_546_960_489_407_305_380_194_799_470):
    """Resposta de `slot0()` como o no a devolve: uma palavra por campo, com
    o int24 negativo estendido ate 256 bits (e assim que o ABI o serializa)."""
    t = tick & ((1 << 256) - 1)
    return "0x" + f"{sqrt:064x}" + f"{t:064x}" + "00" * 32 * 3


def test_o_tick_sai_da_palavra_1_do_slot0():
    assert J._tick_de_slot0(_slot0(222154)) == 222154


def test_tick_NEGATIVO_e_lido_com_sinal():
    """Metade das pools desta chain tem tick negativo — a QUBT entre elas.
    Ler sem sinal devolve ~16,7 milhoes e o preco sai por um fator absurdo,
    que e a familia do `pending_fees._i`."""
    assert J._tick_de_slot0(_slot0(-255_660)) == -255_660
    assert J._i24(0xFFFFFF) == -1
    assert J._i24(0x7FFFFF) == 8_388_607


def test_o_preco_do_contrato_vira_o_AGORA_e_a_fonte_e_dita(tmp_path):
    """`pool_hora` publicava a cor com 52 min de atraso mediano numa chain
    que negocia 24/7."""
    bd = _banco(tmp_path, [{"pool": "0xa", "token": "0xmu",
                            "pontos": _pontos(100.0, 110.0)}],
                simbolos={"0xmu": "MU"})
    d = J._serie_por_pool(bd, AGORA, {"0xa": {"preco": 121.0, "bloco": 7}})
    assert d["0xa"]["preco"] == 121.0
    assert d["0xa"]["preco_fonte"] == "contrato"
    # a variacao usa o preco NOVO contra a base de 24 h atras, nao o do banco
    assert d["0xa"]["variacao_24h_pct"] == pytest.approx(21.0, rel=1e-6)


def test_sem_contrato_cai_para_pool_hora_DIZENDO(tmp_path):
    """Degradar para o dado velho e aceitavel; degradar em silencio nao."""
    bd = _banco(tmp_path, [{"pool": "0xa", "token": "0xmu",
                            "pontos": _pontos(100.0, 110.0)}],
                simbolos={"0xmu": "MU"})
    d = J._serie_por_pool(bd, AGORA, {})          # o contrato nao respondeu
    assert d["0xa"]["preco"] == pytest.approx(110.0)
    assert d["0xa"]["preco_fonte"] == "pool_hora"


def test_a_idade_do_TVL_acompanha_o_tile(tmp_path):
    """O preco passou a ser do bloco corrente e a area continua da serie
    horaria: sao frescores diferentes na MESMA celula, e o `title` diz."""
    bd = _banco(tmp_path, [{"pool": "0xa", "token": "0xmu",
                            "pontos": _pontos(100.0, 110.0)}],
                simbolos={"0xmu": "MU"})
    d = J._serie_por_pool(bd, AGORA, {"0xa": {"preco": 121.0}})
    assert d["0xa"]["tvl_idade_min"] == pytest.approx(0.0, abs=1.0)
    t = H.agregar(d)[0]
    assert t.preco_fonte == "contrato"
    assert R._title(t, "tvl").count("bloco corrente") == 1


def test_o_docstring_nao_promete_mais_zero_RPC():
    """A afirmacao estava no cabecalho e a mudanca a tornou falsa — a familia
    de bug numero dois deste projeto."""
    assert "Zero chamadas de RPC" not in (J.__doc__ or "")
    assert "chamadas de RPC" in (J.__doc__ or "")


def test_preco_do_tick_nas_DUAS_orientacoes():
    """Com a stable em token0 o preco e o inverso da razao nativa."""
    assert J.preco_do_tick(0, False) == pytest.approx(1e12)
    assert J.preco_do_tick(0, True) == pytest.approx(1e12)
    p1 = J.preco_do_tick(276_324, False, 6, 18)
    p0 = J.preco_do_tick(-276_324, True, 6, 18)
    assert p1 == pytest.approx(p0)
    assert J.preco_do_tick(276_324, False, 6, 18) == pytest.approx(1e12 * 1.0001 ** 276_324)


def test_slot_da_pool_V4_e_o_keccak_do_id_com_o_indice_6():
    from eth_utils import keccak
    from defime.chain import abi_v4 as A
    pid = "0x" + "ab" * 32
    esperado = int.from_bytes(keccak(bytes.fromhex("ab" * 32) + (6).to_bytes(32, "big")), "big")
    assert A.slot_da_pool(pid) == esperado
    assert A.SEL_EXTSLOAD == "0x1e2eaeaf"
