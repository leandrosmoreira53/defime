"""`jobs/simbolos.py` — o nome do par, lido do token e nao de uma lista.

Nenhum teste toca a rede: o `rpc` entra como duble.
"""
from __future__ import annotations

import io
import sqlite3
import sys
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[1]

from defime.jobs import simbolos as S        # noqa: E402


def _banco() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    S.criar_tabela(c)
    c.execute("""create table pool_param(pool text primary key, usdg0 integer,
        token0 text, token1 text, spacing integer, taxa real, versao text,
        primeiro_ts integer)""")
    return c


class RpcFalso:
    """Devolve o que o teste mandar, e CONTA as chamadas — e a contagem que
    prova que o cache evita trabalho."""

    def __init__(self, respostas: dict):
        self.respostas, self.chamadas = respostas, []

    def eth_call(self, addr, sel, *a, **k):
        self.chamadas.append((addr, sel))
        v = self.respostas.get((addr.lower(), sel))
        if isinstance(v, Exception):
            raise v
        return v


def _string_abi(txt: str) -> str:
    """`symbol()` no formato canonico: offset, tamanho, bytes."""
    b = txt.encode()
    return ("0x" + "20".rjust(64, "0")
            + f"{len(b):064x}" + b.hex().ljust(64, "0"))


def _bytes32(txt: str) -> str:
    return "0x" + txt.encode().hex().ljust(64, "0")


# ------------------------------------------------------------- symbol()

def test_le_symbol_nos_DOIS_formatos_que_a_chain_devolve():
    """O ERC-20 canonico devolve `string` (offset + tamanho + bytes); token
    antigo devolve `bytes32` cru com zeros a direita.

    Ler so a primeira forma devolveria lixo na segunda; ler so a segunda
    devolveria o offset `0x20` como se fosse o nome — um token chamado " "
    entrando no seletor sem nada reclamar."""
    rpc = RpcFalso({("0xaa", S.SEL_SYMBOL): _string_abi("USDG"),
                    ("0xbb", S.SEL_SYMBOL): _bytes32("QUBT")})
    assert S._texto(rpc, "0xaa") == "USDG"
    assert S._texto(rpc, "0xbb") == "QUBT"


def test_token_que_falha_NAO_entra_no_cache():
    """Gravar `simbolo = None` marcaria o token como resolvido e ele nunca
    mais seria tentado: o 429 de um minuto viraria nome ausente para sempre.
    Mesma regra do `faixa_fechada_v3`, que tambem nao cacheia `None`."""
    c = _banco()
    rpc = RpcFalso({("0xaa", S.SEL_SYMBOL): _string_abi("USDG"),
                    ("0xaa", S.SEL_DECIMALS): "0x6",
                    ("0xbb", S.SEL_SYMBOL): RuntimeError("HTTP 429")})
    r = S.resolver(c, rpc, ["0xAA", "0xBB"])
    assert r["novos"] == 1 and r["falhos"] == 1
    assert c.execute("select count(*) from token_simbolo").fetchone()[0] == 1
    # a segunda passada tenta o que falhou DE NOVO
    rpc2 = RpcFalso({("0xbb", S.SEL_SYMBOL): _string_abi("QUBT"),
                     ("0xbb", S.SEL_DECIMALS): "0x12"})
    r2 = S.resolver(c, rpc2, ["0xAA", "0xBB"])
    assert r2["novos"] == 1 and r2["pedidos"] == 1        # o 0xaa nao voltou
    assert ("0xaa", S.SEL_SYMBOL) not in rpc2.chamadas


def test_o_cache_evita_a_chamada_e_quem_so_le_fica_com_zero_rpc():
    """A pagina `/compor/` declara ZERO chamadas de RPC. Isso so se sustenta
    porque simbolo de token NUNCA muda e a segunda passada nao pergunta de
    novo — se perguntasse, o custo voltaria a ser por ciclo."""
    c = _banco()
    rpc = RpcFalso({("0xaa", S.SEL_SYMBOL): _string_abi("USDG"),
                    ("0xaa", S.SEL_DECIMALS): "0x6"})
    S.resolver(c, rpc, ["0xaa"])
    n = len(rpc.chamadas)
    S.resolver(c, rpc, ["0xaa"])
    assert len(rpc.chamadas) == n, "perguntou de novo o que ja estava no cache"


# ------------------------------------------------------------- o nome

def _pool(c, pool, usdg0, taxa, versao="v3", t0="0xaa", t1="0xbb"):
    c.execute("insert into pool_param values (?,?,?,?,60,?,?,0)",
              (pool, int(usdg0), t0, t1, taxa, versao))


def test_o_par_sai_do_lado_que_NAO_e_a_stable_nas_duas_orientacoes():
    """Com a stable em token0 o ativo e o token1, e vice-versa. Ignorar
    `usdg0` publicaria `USDG/QUBT` em 11 das 18 pools desta chain — a mesma
    inversao que fez GME e SLV valerem ZERO, so que no rotulo."""
    c = _banco()
    c.execute("insert into token_simbolo values ('0xaa','USDG',6,0)")
    c.execute("insert into token_simbolo values ('0xbb','QUBT',18,0)")
    _pool(c, "p1", usdg0=True, taxa=0.003)
    _pool(c, "p2", usdg0=False, taxa=0.003, t0="0xbb", t1="0xaa")
    n = S.nomes(c)
    assert n["p1"] == "QUBT/USDG 0,30%"
    assert n["p2"] == "QUBT/USDG 0,30%"


def test_a_taxa_entra_no_nome_porque_o_MESMO_par_tem_varias_pools():
    """GME 0,30% e GME 1%, RBLX 0,30% e RBLX 1%, USO 0,30% e USO 1% — todas
    reais nesta chain. Duas linhas identicas num seletor sao piores que o
    endereco: com o endereco da para escolher, com o nome repetido nao."""
    c = _banco()
    c.execute("insert into token_simbolo values ('0xaa','USDG',6,0)")
    c.execute("insert into token_simbolo values ('0xbb','GME',18,0)")
    _pool(c, "p1", usdg0=True, taxa=0.003)
    _pool(c, "p2", usdg0=True, taxa=0.01)
    _pool(c, "p3", usdg0=True, taxa=0.0005)
    _pool(c, "p4", usdg0=True, taxa=0.003499, versao="v4")
    n = S.nomes(c)
    assert n["p1"] == "GME/USDG 0,30%"
    assert n["p2"] == "GME/USDG 1%"            # inteira sai sem as casas
    assert n["p3"] == "GME/USDG 0,05%"
    assert n["p4"].endswith(" v4")             # a versao separa as homonimas
    assert len(set(n.values())) == 4, "duas pools com o mesmo rotulo"


def test_pool_sem_simbolo_dos_DOIS_lados_nao_recebe_nome():
    """Meio nome (`?/USDG`) e pior que endereco: parece informacao. Sem os
    dois simbolos a pool cai no rotulo antigo, que ao menos e honesto."""
    c = _banco()
    c.execute("insert into token_simbolo values ('0xaa','USDG',6,0)")
    _pool(c, "p1", usdg0=True, taxa=0.003)          # 0xbb desconhecido
    _pool(c, "p2", usdg0=True, taxa=0.003, t0=None, t1=None)
    assert S.nomes(c) == {}


# --------------------------------------------- o Initialize da V4

class RpcLogs:
    def __init__(self, topo, logs):
        self.topo, self.logs, self.pedidos = topo, logs, []

    def call(self, metodo, params=None):
        if metodo == "eth_blockNumber":
            return hex(self.topo)
        p = params[0]
        de, ate = int(p["fromBlock"], 16), int(p["toBlock"], 16)
        self.pedidos.append((de, ate))
        if ate - de + 1 > 1_000_000:
            raise RuntimeError("log query timed out")
        return [lg for lg in self.logs if de <= lg["_bloco"] <= ate]


# Endereco de verdade tem 20 bytes e o topic 32: o padding e de 24 zeros a
# ESQUERDA. A primeira versao deste duble montou o topic curto e o teste
# reprovou o codigo por um defeito do proprio teste.
END_A = "0x" + "aa" * 20
END_B = "0x" + "bb" * 20


def _topico(end: str) -> str:
    return "0x" + end[2:].rjust(64, "0")


def _log_init(bloco, pid, c0, c1, fee=3000, spacing=30):
    # data: fee, tickSpacing, hooks, sqrtPriceX96, tick
    dados = "0x" + f"{fee:064x}" + f"{spacing:064x}" + "0" * 64 * 3
    return {"_bloco": bloco, "data": dados, "topics": [
        S.TOPIC0["Initialize"], pid, _topico(c0), _topico(c1)]}


def test_o_initialize_preenche_as_moedas_das_pools_V4():
    """Na V4 nao existe contrato de pool para perguntar `token0()`: o
    PoolManager e singleton e o "endereco" e o hash da PoolKey. Sem esta
    varredura os V4 do catalogo ficam com `token0` nulo para SEMPRE, porque
    `pools_hora._parametros` so le `token0()` quando ha contrato."""
    c = _banco()
    pid = "0x" + "cd" * 32
    _pool(c, pid, usdg0=True, taxa=0.003, versao="v4", t0=None, t1=None)
    rpc = RpcLogs(2_500_000, [_log_init(1_500_000, pid, END_A, END_B)])
    r = S.varrer_initialize(c, rpc, passo=2_000_000)
    assert r["pool_param_preenchidos"] == 1
    lin = c.execute("select token0, token1 from pool_param where pool=?",
                    (pid,)).fetchone()
    assert (lin["token0"], lin["token1"]) == (END_A, END_B)


def test_a_janela_ENCOLHE_ate_passar_em_vez_de_aceitar_a_falha():
    """`log query timed out` depende de quantos logs ha dentro, nao do tamanho
    da faixa. A primeira versao deste padrao no `pools_hora` engoliu a excecao
    e treze pools sairam como "nenhum Swap" quando o que tinham era uma
    consulta que nao voltou — 22 pools viraram 31 depois da correcao.

    O duble recusa qualquer janela acima de 1M de blocos: se o codigo aceitasse
    a falha, nao acharia o log nenhum."""
    c = _banco()
    pid = "0x" + "cd" * 32
    _pool(c, pid, usdg0=True, taxa=0.003, versao="v4", t0=None, t1=None)
    rpc = RpcLogs(2_500_000, [_log_init(2_400_000, pid, END_A, END_B)])
    r = S.varrer_initialize(c, rpc, passo=2_000_000)
    assert r["pool_param_preenchidos"] == 1
    assert any(a - d + 1 <= 1_000_000 for d, a in rpc.pedidos)


def test_o_initialize_NAO_sobrescreve_moeda_que_ja_estava_la():
    """`pool_param.token0` da V3 vem do proprio contrato da pool, que e fonte
    mais direta. Sobrescrever com o evento seria trocar uma leitura de estado
    por uma reconstrucao — e reconstruir por cima do que ja esta lido e como
    este projeto ja inflou liquidez em 6,49% uma vez."""
    c = _banco()
    pid = "0x" + "cd" * 32
    _pool(c, pid, usdg0=True, taxa=0.003, versao="v4", t0="0x11", t1="0x22")
    rpc = RpcLogs(500_000, [_log_init(1000, pid, END_A, END_B)])
    r = S.varrer_initialize(c, rpc, passo=2_000_000)
    assert r["pool_param_preenchidos"] == 0
    lin = c.execute("select token0 from pool_param where pool=?", (pid,)).fetchone()
    assert lin["token0"] == "0x11"


# ------------------------------------------------------- inviolavel #1

def test_o_resolvedor_nao_assina_nada():
    """Inviolavel #1: nenhuma chave, nenhuma assinatura, nenhuma transacao.
    Ele so faz `eth_call`, que e leitura."""
    src = io.open(RAIZ / "src" / "defime" / "jobs" / "simbolos.py", encoding="utf-8").read().lower()
    for proibido in ("private_key", "mnemonic", ".sign(", "sendtransaction",
                     "send_raw", "account.from_key"):
        assert proibido not in src, f"{proibido} aparece em simbolos.py"


def test_o_rotulo_da_V4_usa_a_taxa_DECLARADA_e_nao_a_medida():
    """Na V4 o campo `fee` do `Swap` e o TOTAL (lpFee + protocolFee sobre a
    entrada): na UPS o contrato declara 3000 e os swaps medem 3499.
    `pool_param.taxa` guarda a medida, que e a certa para o CUSTO — mas o
    rotulo precisa da declarada, que e a que a tela da Uniswap mostra.

    Chamar a pool de "0,35%" quando o app dela diz "0,30%" quebraria
    exatamente o que o nome existe para fazer: identificar."""
    c = _banco()
    c.execute("insert into token_simbolo values (?,'USDG',6,0)", (END_A,))
    c.execute("insert into token_simbolo values (?,'UPS',18,0)", (END_B,))
    pid = "0x" + "cd" * 32
    _pool(c, pid, usdg0=True, taxa=0.003499, versao="v4", t0=END_A, t1=END_B)
    assert S.nomes(c)[pid] == "UPS/USDG 0,35% v4"        # so a medida

    rpc = RpcLogs(500_000, [_log_init(1000, pid, END_A, END_B, fee=3000)])
    S.varrer_initialize(c, rpc, passo=2_000_000)
    assert S.nomes(c)[pid] == "UPS/USDG 0,30% v4"        # a declarada manda


def test_a_chave_da_V4_vai_para_TABELA_PROPRIA_e_nao_em_coluna_nova():
    """`pools_hora._parametros` grava `pool_param` com
    `values (?,?,?,?,?,?,?,?)` — POSICIONAL. Coluna nova entraria no fim para
    o `create table` e no meio para o banco antigo, e o `alter table` do
    SQLite sempre acrescenta no fim: os valores sairiam embaralhados sem erro
    nenhum. O proprio `pools_hora` ja documenta essa armadilha."""
    c = _banco()
    colunas = [r[1] for r in c.execute("pragma table_info(pool_param)")]
    assert colunas == ["pool", "usdg0", "token0", "token1", "spacing",
                       "taxa", "versao", "primeiro_ts"]
    assert c.execute("select count(*) from pool_v4_chave").fetchone()[0] == 0


def test_a_flag_de_taxa_dinamica_NAO_e_lida_como_taxa():
    """`0x800000` no campo `fee` da V4 e a `DYNAMIC_FEE_FLAG` — "quem define a
    taxa e o hook, a cada swap" —, nao um numero de pips. Lida como numero da
    8.388.608 pips = **838,86%**, e foi isso que o primeiro rotulo publicou
    numa pool de SPY.

    O CLAUDE.md ja registra a mesma armadilha na direcao oposta: o agregador leu
    a flag como 0,00% em quatro pools V4. As duas leituras erradas saem do
    mesmo campo, uma arredonda para baixo e a outra para cima, e nenhuma
    levanta erro."""
    c = _banco()
    c.execute("insert into token_simbolo values (?,'USDG',6,0)", (END_A,))
    c.execute("insert into token_simbolo values (?,'SPY',18,0)", (END_B,))
    pid = "0x" + "ef" * 32
    _pool(c, pid, usdg0=True, taxa=0.00025, versao="v4", t0=END_A, t1=END_B)
    rpc = RpcLogs(500_000, [_log_init(1000, pid, END_A, END_B,
                                      fee=S.FLAG_TAXA_DINAMICA)])
    S.varrer_initialize(c, rpc, passo=2_000_000)
    nome = S.nomes(c)[pid]
    assert "838" not in nome, nome
    assert nome == "SPY/USDG dinâmica v4", nome
