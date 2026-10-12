"""A sentinela de gas — e os dois portoes que impedem ruido de virar conselho.

Contexto medido em 07/09: o `baseFeePerGas` subiu **vinte vezes** em duas
semanas (0,0201 gwei na w33 contra 0,4018 na w35) e ninguem notou, porque nada
media. E o absoluto continua sendo ruido: rerange a **$0,88** contra ~$120 de
taxa de interface na mesma operacao.

As duas coisas juntas sao o motivo do modulo: "e ruido HOJE" e uma medicao, nao
uma lei, e a diferenca entre as duas e exatamente uma sentinela.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from defime.custo import gas as G


def test_o_custo_medido_hoje_se_reproduz():
    """Ancoragem: a 0,3097 gwei e ETH a $3.000, o que a chain cobrou.

    Medido em recibos reais em 07/09 — swap 203.786 gas a $0,189 e mint
    491.148 a $0,456. Sem esta ancora o modulo seria uma formula que ninguem
    conferiu, que foi como a tabela de fee por sessao saiu 3x baixa em 01/09.
    """
    assert abs(G.custo_usd(0.3097, 203_786, 3000.0) - 0.1893) < 0.002
    assert abs(G.custo_usd(0.3097, 491_148, 3000.0) - 0.4563) < 0.002
    # O rerange inteiro, que e o numero que decide. **$0,905 e nao $0,88**:
    # o primeiro calculo usou um swap de 180.000 gas estimado, e o medido hoje
    # num swap real e 203.786. A diferenca nao muda decisao nenhuma — continua
    # 0,7% da taxa de interface —, mas o numero publicado passa a ser o medido.
    assert abs(G.custo_usd(0.3097, G.GAS_RERANGE, 3000.0) - 0.9052) < 0.01


def test_a_sentinela_esta_QUIETA_no_gas_de_hoje():
    """$0,88 de gas contra $120 de taxa de interface e 0,7% — abaixo de 1%.

    E por isso que a recomendacao "espere, o gas esta alto" seria errada hoje:
    esperar uma hora custa $38,82 de fee para economizar, no maximo, $0,98.
    """
    s = G.sentinela(0.3097, 3000.0, volume_swap_usd=7500.0)
    assert s["alerta"] is False
    assert 0.006 < s["fracao"] < 0.008


def test_a_sentinela_DISPARA_quando_o_gas_passa_a_decidir():
    """Cem vezes o gas de hoje e o rerange sai a $88 — ai ele existe.

    O limiar nao e um gwei escolhido: e o gas contra o custo da MESMA
    operacao. Um limiar em gwei erraria em toda pool de volume diferente.
    """
    s = G.sentinela(31.0, 3000.0, volume_swap_usd=7500.0)
    assert s["alerta"] is True
    assert s["fracao"] > G.FRACAO_QUE_IMPORTA


def test_o_limiar_acompanha_o_TAMANHO_da_operacao():
    """A mesma condicao de rede decide num rerange pequeno e nao num grande.

    Com $500 de swap a taxa de interface e $8, e o mesmo gas de $0,88 e 11%
    dela. Com $7.500 e 0,7%. Um limiar em gwei nao distinguiria os dois — e e
    o pequeno que o Test Lab mede.
    """
    grande = G.sentinela(0.3097, 3000.0, volume_swap_usd=7500.0)
    pequeno = G.sentinela(0.3097, 3000.0, volume_swap_usd=500.0)
    assert grande["alerta"] is False
    assert pequeno["alerta"] is True
    assert pequeno["fracao"] > 10 * grande["fracao"]


def test_sem_preco_do_ETH_o_veredito_e_NONE_e_nao_zero():
    """Ausencia se declara. O gwei continua publicado porque foi medido; o
    dolar sai `None` com motivo, em vez de um preco suposto."""
    s = G.sentinela(0.3097, None, volume_swap_usd=7500.0)
    assert s["custo_usd"] is None
    assert s["alerta"] is None
    assert s["base_gwei"] == 0.3097
    assert "ETH" in s["motivo"]


def test_o_portao_de_horario_REPROVA_o_que_foi_medido_em_07_09():
    """Quatro faixas de 6h, 14 amostras cada, medianas de 0,266 a 0,291 gwei.

    Razao 1,09x. Se o portao aprovasse isso, a pagina publicaria "melhor hora"
    para uma diferenca de **$0,08** — e treinaria quem le a esperar por ela.
    """
    por_faixa = {0: [0.2894] * 14, 1: [0.2768] * 14,
                 2: [0.2912] * 14, 3: [0.2661] * 14}
    r = G.pode_recomendar_horario(por_faixa)
    assert r["pode"] is False
    assert r["razao"] < 1.10
    # Os DOIS motivos saem, porque os dois criterios falharam.
    assert "amostra" in r["motivo"] and "razão" in r["motivo"]


def test_amostra_grande_com_razao_pequena_continua_reprovando():
    """n nao compra padrao. Mil amostras de uma diferenca de 9% continuam
    descrevendo 9% — e o custo de esperar nao caiu por haver mais dado."""
    por_faixa = {0: [0.29] * 500, 1: [0.266] * 500}
    r = G.pode_recomendar_horario(por_faixa)
    assert r["pode"] is False
    assert r["n_min"] == 500
    assert "razão" in r["motivo"]


def test_razao_grande_com_amostra_pequena_tambem_reprova():
    """O caso da LULU: 6,7 horas de historia no topo da tabela. Diferenca
    enorme e n=5 nao e padrao, e a razao entre uma faixa medida e uma faixa
    chutada nao e razao."""
    por_faixa = {0: [0.10] * 5, 1: [0.90] * 5}
    r = G.pode_recomendar_horario(por_faixa)
    assert r["pode"] is False
    assert r["razao"] > 5
    assert "amostra" in r["motivo"]


def test_o_portao_APROVA_quando_os_dois_criterios_fecham():
    por_faixa = {0: [0.10] * 40, 1: [0.30] * 40}
    r = G.pode_recomendar_horario(por_faixa)
    assert r["pode"] is True
    assert r["motivo"] is None
    assert r["mais_barata"] == 0 and r["mais_cara"] == 1


def test_uma_faixa_so_nao_produz_razao():
    r = G.pode_recomendar_horario({0: [0.1] * 100})
    assert r["pode"] is False
    assert r["razao"] is None


def test_o_percentil_e_um_valor_que_ACONTECEU():
    """Sem interpolacao, pela mesma razao do `median_high` do `/compor/`: um
    limiar nao pode ser um valor que nunca foi observado."""
    v = [1.0, 2.0, 3.0, 4.0, 100.0]
    r = G.resumo(v)
    assert r["p90"] in v and r["p10"] in v
    assert r["max"] == 100.0 and r["n"] == 5


def test_amostra_curta_nao_vira_distribuicao():
    assert G.resumo([1.0, 2.0]) is None
    assert G.resumo([]) is None


def test_a_taxa_de_interface_e_a_MEDIDA():
    """1,6000% em dez de dez swaps da migracao de 04/09. Se alguem mexer nisto
    sem uma medicao nova, o teste cai — o numero e a ancora da sentinela
    inteira."""
    assert G.TAXA_INTERFACE == 0.016
