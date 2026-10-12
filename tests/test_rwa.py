"""Coleta do universo RWA: leitura só-leitura de um indexador opcional e as recusas de número inventado."""

import sqlite3
import time

import pytest

pytest.importorskip("defime_mcp")
from defime.jobs import rwa as P

AGORA = int(time.time())


def _banco(tmp_path):
    db = tmp_path / "indexador.db"
    c = sqlite3.connect(db)
    c.executescript("""
        create table pool_hora(pool text, ts integer, bloco_de integer, bloco_ate integer, tick integer,
            preco real, liquidez_ativa text, tvl_usd real, tvl_fonte text, book_usd real, swaps integer,
            volume_usd real, fee_usd real, ratio_stable real, primary key (pool, ts));
        create table pool_param(pool text primary key, usdg0 integer, token0 text, token1 text,
            spacing integer, taxa real, versao text, primeiro_ts integer);
        create table token_simbolo(endereco text primary key, simbolo text, decimais integer, visto_em integer);
    """)
    c.executemany("insert into pool_hora(pool, ts, tvl_usd, tvl_fonte, book_usd) values (?,?,?,?,?)", [
        ("0xaaa", AGORA - 7200, 900_000, "balanceOf", 100_000),
        ("0xaaa", AGORA - 3600, 1_000_000, "balanceOf", 120_000),   # a mais nova vence
        ("0xbbb", AGORA - 40 * 3600, 5_000, "balanceOf", 1_000),      # velha demais: fora
    ])
    c.execute("insert into pool_param values ('0xaaa', 1, '0xusdg', '0xmu', 60, 0.003, 'v3', 0)")
    c.executemany("insert into token_simbolo values (?,?,18,0)", [("0xusdg", "USDG"), ("0xmu", "MU")])
    c.commit()
    c.close()
    return str(db)


def test_indexador_le_a_ultima_e_ignora_velha(tmp_path):
    idx = P.indexador(_banco(tmp_path), AGORA)
    assert set(idx) == {"0xaaa"}
    assert idx["0xaaa"]["tvl"] == 1_000_000 and idx["0xaaa"]["nome"] == "MU/USDG"   # ativo primeiro


def test_indexador_abre_so_leitura(tmp_path):
    db = _banco(tmp_path)
    P.indexador(db, AGORA)
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    with pytest.raises(sqlite3.OperationalError):
        con.execute("insert into pool_param values ('x',0,'','',0,0,'',0)")


def test_sem_banco_nao_quebra():
    assert P.indexador(None, AGORA) == {} and P.indexador("/nao/existe.db", AGORA) == {}


def test_apr_e_divergencia():
    assert P._apr(1000, 0.25, 100_000, 1) == pytest.approx(1000 * 0.0025 / 100_000 * 365 * 100)
    assert P._apr(1000, None, 100_000, 1) is None          # taxa ilegível não vira 0%
    assert P._div(1_000_000, 1_300_000) == pytest.approx(30.0)
    assert P._div(None, 5) is None


def _linha(**kw):
    base = dict(nome="MU / USDG", ativo="Micron Technology – Robinhood Token", pool_id="0x" + "1" * 40,
                versao="v3", fee=0.3, lp=0.25, marca="", motivo=None, tvl=1_000_000, tvl_idx=700_000,
                fonte_idx="balanceOf", apr_1h=10.0, apr_24h=20.0, apr_7d=30.0, apr_30d=40.0, dias=30,
                erro_hist=None)
    base.update(kw)
    return base






# --- Fase 2 do PLAN_HEAT_POOLS: série de vol_1h e rwa.json ---------------------------------------

import json
import os


def _r(pid, v1, v24=1000.0, tvl=50_000.0):
    return {"pool_id": pid, "vol_1h": v1, "vol_24h": v24, "tvl": tvl}


def test_serie_grava_le_e_nao_duplica(tmp_path):
    db = tmp_path / "s.db"
    assert P.serie_gravar(db, AGORA - 3600, [_r("0xa", 10.0), _r("0xb", 5.0)]) == 2
    P.serie_gravar(db, AGORA, [_r("0xa", 20.0)])
    P.serie_gravar(db, AGORA, [_r("0xa", 99.0)])                 # mesma rodada de novo: não duplica
    s = P.serie_ler(db, AGORA)
    assert s["0xa"] == [[AGORA - 3600, 10.0], [AGORA, 20.0]] and s["0xb"] == [[AGORA - 3600, 5.0]]


def test_vol_1h_ausente_nao_vira_ponto_zero(tmp_path):
    db = tmp_path / "s.db"
    assert P.serie_gravar(db, AGORA, [_r("0xa", None), _r("0xb", 0.0)]) == 1
    assert P.serie_ler(db, AGORA) == {"0xb": [[AGORA, 0.0]]}     # zero medido fica; ausente não entra


def test_serie_poda_e_janela(tmp_path):
    db = tmp_path / "s.db"
    P.serie_gravar(db, AGORA - 8 * 86400, [_r("0xa", 1.0)], agora=AGORA - 8 * 86400)
    P.serie_gravar(db, AGORA - 2 * 86400, [_r("0xa", 2.0)], agora=AGORA - 2 * 86400)
    P.serie_gravar(db, AGORA, [_r("0xa", 3.0)])                  # poda o de 8 dias
    assert P.serie_ler(db, AGORA, janela_s=10 * 86400)["0xa"] == [[AGORA - 2 * 86400, 2.0], [AGORA, 3.0]]
    assert P.serie_ler(db, AGORA)["0xa"] == [[AGORA, 3.0]]      # entrega só as ~26 h


def test_serie_sem_banco_e_vazia(tmp_path):
    assert P.serie_ler(tmp_path / "nao.db", AGORA) == {}


def test_estado_leva_a_serie_de_cada_pool_sem_conta_nova():
    d = {"ts": AGORA, "n_universo": 83, "rwa": [_linha(pool_id="0xa"), _linha(pool_id="0xb")], "conf": []}
    e = P.estado_dados(d, {"0xa": [[AGORA, 7.0]]})
    assert e["rwa"][0]["serie_vol_1h"] == [[AGORA, 7.0]] and e["rwa"][1]["serie_vol_1h"] == []
    assert not any(k in e["rwa"][0] for k in ("concentracao", "giro", "aquecendo"))   # a conta mora em defime.heat_pools




