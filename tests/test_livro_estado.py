"""O livro lido do estado: a caminhada, a ancora e as tres conferencias.

O defeito que estes testes existem para travar foi cometido: na primeira
versao (um spike) a caminhada para BAIXO atribuia a cada faixa a liquidez da
faixa seguinte, e a "conferencia" comparava a liquidez ativa com a faixa que a
propria ancora tinha acabado de escrever — tautologia que passa sempre.
"""

from __future__ import annotations

import pytest

from defime.chain.livro_estado import (caminhar, gross_e_net, livro,
                                       palavras_do_bitmap, ticks_inicializados)

SPACING = 60
POOL = "0x0f4227d27082b3bca6818381b9ea6460275e49f4"


class RpcFalso:
    """Responde `aggregate3` a partir de um bitmap e de um mapa de ticks."""

    def __init__(self, bitmaps: dict[int, int], ticks: dict[int, tuple[int, int]]):
        self.bitmaps, self.ticks, self.chamadas = bitmaps, ticks, 0

    def eth_call(self, to, data, bloco=None):
        from eth_abi import decode, encode
        self.chamadas += 1
        (itens,) = decode(["(address,bool,bytes)[]"], bytes.fromhex(data[10:]))
        saida = []
        for _alvo, _ok, dados in itens:
            sel, arg = "0x" + dados[:4].hex(), int.from_bytes(dados[4:36], "big")
            if sel == "0x5339c296":
                w = arg - (1 << 256) if arg >= 1 << 255 else arg
                saida.append((True, self.bitmaps.get(w, 0).to_bytes(32, "big")))
            else:
                t = arg - (1 << 256) if arg >= 1 << 255 else arg
                gross, net = self.ticks.get(t, (0, 0))
                saida.append((True, gross.to_bytes(32, "big")
                              + (net & ((1 << 256) - 1)).to_bytes(32, "big")))
        return "0x" + encode(["(bool,bytes)[]"], [saida]).hex()


def _rpc(ticks: dict[int, tuple[int, int]]):
    bitmaps: dict[int, int] = {}
    for t in ticks:
        c = t // SPACING
        bitmaps[c >> 8] = bitmaps.get(c >> 8, 0) | (1 << (c & 255))
    return RpcFalso(bitmaps, ticks)


# livro simetrico: 1000 entra em -120, mais 500 em 0, sai tudo em 120 e 240
TICKS = {-120: (1000, 1000), 0: (500, 500), 120: (500, -500), 240: (1000, -1000)}


def test_a_faixa_do_tick_corrente_vale_a_ancora_dos_dois_lados():
    """O bug da primeira versao: a faixa logo ABAIXO do tick recebia a
    liquidez da faixa de baixo, e o livro saia deslocado por uma borda."""
    segs = caminhar(tick=60, liquidez=1500, net={t: n for t, (_g, n) in TICKS.items()})
    por_faixa = {(a, b): L for a, b, L in segs}
    assert por_faixa[(0, 60)] == 1500, "abaixo do tick, ainda na mesma faixa"
    assert por_faixa[(60, 120)] == 1500, "acima do tick, ainda na mesma faixa"


def test_a_caminhada_aplica_o_net_na_direcao_certa():
    segs = {(a, b): L for a, b, L in
            caminhar(60, 1500, {t: n for t, (_g, n) in TICKS.items()})}
    assert segs[(120, 240)] == 1000      # sai 500 ao cruzar 120 para cima
    assert segs[(-120, 0)] == 1000       # 500 entraram em 0
    assert all(L >= 0 for L in segs.values())


def test_ancora_errada_produz_faixa_negativa_e_isso_reprova():
    r = _rpc(TICKS)
    # a liquidez ativa de verdade e 1500; com 100 a janela nao fecha
    fora = livro(r, POOL, spacing=SPACING, tick=60, liquidez=100,
                 alcance_ticks=600, bloco=1)
    assert not fora["confere"]
    assert any("negativa" in p for p in fora["problemas"])
    certo = livro(r, POOL, spacing=SPACING, tick=60, liquidez=1500,
                  alcance_ticks=600, bloco=1)
    assert certo["confere"] and certo["problemas"] == []
    assert certo["ancora"] == 1500 and certo["ticks"] == 4


def test_tick_com_gross_zero_ou_net_maior_que_gross_reprova():
    r = _rpc({**TICKS, 300: (0, 0)})
    r.bitmaps[300 // SPACING >> 8] |= 1 << ((300 // SPACING) & 255)
    assert any("nao esta inicializado" in p for p in
               livro(r, POOL, spacing=SPACING, tick=60, liquidez=1500,
                     alcance_ticks=600, bloco=1)["problemas"])
    r2 = _rpc({**TICKS, 300: (10, 999)})
    assert any("|net| > gross" in p for p in
               livro(r2, POOL, spacing=SPACING, tick=60, liquidez=1500,
                     alcance_ticks=600, bloco=1)["problemas"])


def test_tick_negativo_e_lido_com_sinal():
    """Metade das pools desta chain tem tick negativo; ler sem sinal aponta
    para um slot que existe e devolve zero, e a liquidez some calada."""
    negativos = {-240: (1000, 1000), -180: (1000, -1000)}
    r = _rpc(negativos)
    v = livro(r, POOL, spacing=SPACING, tick=-210, liquidez=1000,
              alcance_ticks=600, bloco=1)
    assert v["confere"], v["problemas"]
    assert {(s["tick_de"], s["tick_ate"]): s["liquidez"] for s in v["segmentos"]}[(-240, -210)] == 1000


def test_le_tudo_em_poucas_chamadas_e_no_mesmo_bloco():
    r = _rpc(TICKS)
    livro(r, POOL, spacing=SPACING, tick=60, liquidez=1500, alcance_ticks=6000, bloco=77)
    assert r.chamadas <= 3, "bitmap e ticks tem de ir em lote"


def test_a_janela_vira_palavras_sem_repetir():
    assert palavras_do_bitmap(-120, 120, SPACING) == [-1, 0]
    assert len(palavras_do_bitmap(0, 60 * 300, SPACING)) == 2


def test_sem_tick_inicializado_sai_uma_faixa_com_a_ancora_e_nao_reprova():
    """Pool sem tick inicializado na janela nao e livro furado: e uma faixa so,
    valendo a liquidez ativa — que com a pool vazia e zero."""
    r = _rpc({})
    v = livro(r, POOL, spacing=SPACING, tick=0, liquidez=0, alcance_ticks=600, bloco=1)
    assert v["ticks"] == 0 and v["confere"]
    # duas faixas porque a caminhada parte do tick corrente, uma de cada lado
    assert [(s["tick_de"], s["tick_ate"], s["liquidez"]) for s in v["segmentos"]] ==         [(-600, 0, 0), (0, 660, 0)]


def test_os_ticks_vem_do_bitmap_e_nao_de_todo_multiplo_do_spacing():
    r = _rpc(TICKS)
    assert ticks_inicializados(r, POOL, SPACING, -600, 600, 1) == [-120, 0, 120, 240]
    assert set(gross_e_net(r, POOL, [-120, 0], 1)) == {-120, 0}


@pytest.mark.parametrize("tick", [-121, -120, -119, 0, 59, 60, 239, 240, 241])
def test_a_faixa_que_contem_o_tick_sempre_vale_a_ancora(tick):
    """Com as bordas da janela, a faixa do preco nunca fica aberta — era assim
    que ela sumia do desenho quando nao havia tick inicializado acima."""
    segs = caminhar(tick, 1500, {t: n for t, (_g, n) in TICKS.items()},
                    lo=-6000, hi=6000)
    dentro = [L for a, b, L in segs if a <= tick < b]
    assert dentro and all(L == 1500 for L in dentro)


def test_sem_as_bordas_a_ponta_fica_aberta_e_e_omitida():
    """`caminhar` sozinho nao inventa borda: quem tem a janela e quem chama."""
    segs = caminhar(240, 1500, {t: n for t, (_g, n) in TICKS.items()})
    assert not [s for s in segs if s[0] <= 240 < s[1]]








