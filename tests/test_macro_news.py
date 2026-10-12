"""Vocabulario de choque de noticia: o que pontua e o que so filtra."""

def test_interrupcao_de_producao_pontua():
    """Lacuna medida em 2026-09-01 com o feed ao vivo.

    "Micron's Taiwan unions threaten strike over bonus dispute" pontuava 0,0:
    o vocabulario cobria oferta e resultado, nao interrupcao de producao — a
    categoria que mais move preco de memoria, porque tira capacidade sem
    aviso.
    """
    from defime.macro.news import TERMOS_SEMICONDUTORES as T
    for termo in ("strike over", "walkout", "earthquake", "power outage"):
        assert termo in T, termo
    titulo = "Micron's Taiwan unions threaten strike over bonus dispute"
    assert any(t in titulo.lower() for t in T)


def test_strike_sozinho_nao_entra():
    """"strike price" aparece em toda materia de opcoes e seria ruido diario."""
    from defime.macro.news import TERMOS_SEMICONDUTORES as T
    assert "strike" not in T
    assert "strike price" not in "micron's unions threaten strike over bonus"


# ------------------------------------------------------- perfil de jogos
def test_predator_sozinho_pegava_monitor_da_acer():
    """Medido na primeira leitura ao vivo do perfil de jogos, 2026-09-03.

    `predator` casou DUAS vezes e as duas eram o monitor **Acer Predator**
    ("Acer's Latest Predator Gaming Monitor", "Acer Predator Atlas 7 Gaming
    Handheld") — marca de hardware gamer, dentro do filtro de assunto
    "gaming". Ele sozinho levava o choque a 6,0, que e o limiar. Mesma
    familia do `strike price` e do `opec+`.
    """
    from defime.macro.news import ASSUNTO_JOGOS, TERMOS_JOGOS
    assert "predator" not in TERMOS_JOGOS
    assert "child predator" in TERMOS_JOGOS
    titulos = [
        "Hertz So Good: Acer's Latest Predator Gaming Monitor Joins the Club",
        "Hand-On with Acer Predator Atlas 7 Gaming Handheld at IFA 2026",
    ]
    for t in titulos:
        b = t.lower()
        assert any(a in b for a in ASSUNTO_JOGOS)      # passa pelo assunto
        assert not [k for k in TERMOS_JOGOS if k in b]  # e nao pontua mais


def test_upgrade_sozinho_pegava_upgrade_de_PC():
    """"My years-old gaming PC ... only needed one upgrade".

    Em noticia de jogos `upgrade` e hardware muito mais vezes que revisao de
    recomendacao. **`downgrade` fica como esta de proposito** — ninguem faz
    downgrade de PC por escolha —, porque encurtar os dois por simetria
    trocaria um falso positivo por um falso negativo.
    """
    from defime.macro.news import TERMOS_JOGOS
    assert "upgrade" not in TERMOS_JOGOS
    assert "analyst upgrade" in TERMOS_JOGOS
    assert "downgrade" in TERMOS_JOGOS
    b = ("my years-old gaming pc became the best ai machine in the house, "
         "and it only needed one upgrade")
    assert not [k for k in TERMOS_JOGOS if k in b]


def test_nenhum_termo_de_jogos_e_substring_de_outro():
    """O invariante que impede a proxima reintroducao. `opec` dentro de
    `opec+` somava 6,0 numa manchete rotineira."""
    from defime.macro.news import TERMOS_JOGOS
    ks = list(TERMOS_JOGOS)
    for a in ks:
        for b in ks:
            if a != b:
                assert a not in b, f"{a!r} esta dentro de {b!r}"


def test_termo_de_jogos_e_sempre_minusculo():
    """A comparacao e com `lower()`: maiuscula falharia calada, entregando
    choque MENOR do que o real."""
    from defime.macro.news import ASSUNTO_JOGOS, TERMOS_JOGOS
    assert all(t == t.lower() for t in TERMOS_JOGOS)
    assert all(a == a.lower() for a in ASSUNTO_JOGOS)


def test_nenhum_termo_de_jogos_estoura_o_limiar_sozinho():
    """Uma manchete nao pode disparar o gate por si. Foi o que o `opec+`
    fazia, somando 3,0 + 3,0 = exatamente o CHOQUE_ALTO."""
    from defime.macro.news import TERMOS_JOGOS, limiar_do_perfil
    assert max(TERMOS_JOGOS.values()) < limiar_do_perfil("jogos")


def test_a_consulta_de_jogos_nao_tem_operando_de_duas_palavras():
    """Medido em 2026-09-03: o Google News RSS devolveu HTTP 200, RSS valido
    e ZERO itens para `Micron OR DRAM OR HBM OR semiconductor memory`,
    enquanto `Micron OR DRAM OR HBM` devolvia 100. Um operando de duas
    palavras no fim envenena a consulta inteira, e o sintoma e um feed vazio
    que se le como "nao ha noticia".
    """
    from defime.macro.news import PERFIS
    consulta = PERFIS["jogos"][1]
    for operando in consulta.split(" OR "):
        assert len(operando.split()) == 1, operando
