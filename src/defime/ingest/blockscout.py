"""Coletor alternativo pelo Blockscout REST, para quando o RPC engasga.

**Por que ele existe.** O 429 do RPC primario e por FREQUENCIA, nao por
tamanho de batch (medido em 2026-09-03: batches de 5, 15, 25 e 50 passam
todos). O limite e do Cloudflare por IP, e os cinco coletores permanentes mais
os oito timers ja o consomem quase todo — qualquer backfill novo disputa com
eles e trava. Um backfill da RBLX repetiu o mesmo erro por 40 minutos.

**E nao ha segundo RPC publico para esta chain.** `robinhood.drpc.org`
responde `eth_chainId` de cache e bloqueia todo o resto (0 de 31 chamadas);
Ankr exige chave; thirdweb diz "Invalid chain". A ideia de rodizio entre tres
RPCs nao se sustenta.

**O que existe e o Blockscout, e ele resolve o gargalo pela raiz.** O que
estoura o RPC no backfill nao e o `eth_getLogs` — e o batch de
`block_timestamps` que vem DEPOIS dele, um item por bloco tocado. O Blockscout
devolve `block_timestamp` embutido em cada log: a metade cara da conta some.

**Ele exigia so um `User-Agent`.** Sem ele: 403. O projeto registrava ha meses
que "a API bloqueia"; ela recusa cliente sem UA. Medido com UA: 200, e 15
requisicoes em 10 segundos sem um erro.

---

**As tres limitacoes, e elas decidem quando usar isto:**

1. **Nao aceita filtro de bloco.** `from_block`/`to_block` devolvem vazio.
   So da para paginar do topo para tras, 50 logs por pagina, seguindo o
   cursor. Serve para pool nova ou para alcancar o topo; nao serve para
   preencher um buraco no meio da historia sem varrer tudo ate ele.
2. **Nao aceita filtro de topico.** `topic0` devolve vazio. Todos os eventos
   do endereco vem, e a filtragem e nossa.
3. **Nao expoe `transactionIndex`.** Grava-se `-1`, que e sentinela e nao
   valor: `log_index` ja ordena dentro do bloco, e nada no projeto usa
   `tx_index` para calculo — so o `npm_link` e o `ownership`, que continuam
   vindo do RPC.

**Fonte secundaria, e o projeto ja tem regra para isso:** o que vier daqui
carrega a procedencia. Nao substitui o RPC como verdade; substitui como
CAMINHO quando a verdade esta inacessivel por limite de taxa.
"""

from __future__ import annotations

import datetime as dt
import json
import time
import urllib.parse
import urllib.request

from sqlalchemy.exc import OperationalError

from ..chain.abi import decode_log
from ..db.models import PoolEvent
from ..db.session import session_scope
from ..logging import get_logger
from .indexer import _upsert_ignore

log = get_logger(__name__)

BASE = "https://robinhoodchain.blockscout.com/api/v2"
# Sem isto e 403. Nao e disfarce: e o que a API exige de qualquer cliente.
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
POR_PAGINA = 50
PAUSA_S = 0.35          # ~3/s, folgado dentro dos 15/10s medidos
TX_INDEX_AUSENTE = -1


def _pedir(url: str, tentativas: int = 4) -> dict:
    ultimo = None
    for i in range(tentativas):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as exc:                              # noqa: BLE001
            ultimo = exc
            time.sleep(1.5 * (i + 1))
    raise RuntimeError("blockscout: %s" % str(ultimo)[:160])


def _ts(iso: str) -> int:
    """"2026-09-03T02:16:36.000000Z" -> epoch.

    O `Z` final e ISO valido mas `fromisoformat` so o aceita a partir do
    Python 3.11; trocar por `+00:00` funciona nos dois e nao custa nada.
    """
    return int(dt.datetime.fromisoformat(
        iso.replace("Z", "+00:00")).timestamp())


def paginar(pool: str, ate_bloco: int | None = None,
            max_paginas: int = 2000):
    """Gera paginas de logs, do mais novo para o mais antigo.

    Para ao chegar em `ate_bloco` — que e o piso, nao o teto: a paginacao so
    anda para tras. Sem ele varre a pool inteira.
    """
    url = f"{BASE}/addresses/{pool}/logs"
    cursor: dict | None = None
    for pagina in range(max_paginas):
        u = url + ("?" + urllib.parse.urlencode(cursor) if cursor else "")
        d = _pedir(u)
        itens = d.get("items") or []
        if not itens:
            return
        yield itens
        menor = min(int(i["block_number"]) for i in itens)
        if ate_bloco is not None and menor <= ate_bloco:
            return
        cursor = d.get("next_page_params")
        if not cursor:
            return
        time.sleep(PAUSA_S)


def _gravar_com_espera(linhas: list[dict], tentativas: int = 6) -> int:
    """Grava um lote, esperando quem estiver com o banco na mao.

    **Uma escrita travada nao pode matar a coleta inteira.** Medido em
    2026-09-03: a LLY paginou 20 minutos e perdeu tudo num
    `database is locked` — quatorze processos escrevem neste SQLite e o
    `busy_timeout` de 30s do engine nao cobre uma escrita longa dos
    permanentes. O lote e idempotente (`_upsert_ignore`), entao repetir e
    seguro; desistir e que nao e.

    Falha que persiste depois da espera continua subindo: banco realmente
    indisponivel e resultado, e o chamador precisa saber.
    """
    for i in range(tentativas):
        try:
            with session_scope() as sess:
                return _upsert_ignore(sess, PoolEvent, linhas)
        except OperationalError as exc:
            if "locked" not in str(exc).lower() or i == tentativas - 1:
                raise
            espera = 5 * (i + 1)
            log.warning("blockscout_banco_travado", extra={
                "tentativa": i + 1, "espera_s": espera})
            time.sleep(espera)
    raise RuntimeError("inalcancavel")


def coletar(pool: str, ate_bloco: int | None = None,
            max_paginas: int = 2000) -> dict:
    """Baixa e grava os eventos da pool. Devolve o que aconteceu.

    Idempotente: `_upsert_ignore` ignora o que ja existe, entao rodar duas
    vezes nao duplica nem apaga — e o mesmo contrato do backfill por RPC.

    **Nao grava marca de cobertura.** `BackfillRange` significa "esta faixa
    foi varrida por inteiro", e paginar do topo para tras nao garante isso: a
    pagina pode acabar no meio de um bloco. Gravar a marca aqui reintroduziria
    o bug de 01/09, em que faixa marcada como completa escondia Mint que a
    chain tinha e o banco nao.
    """
    t0 = time.time()
    total = gravados = paginas = 0
    menor = maior = None
    for itens in paginar(pool, ate_bloco, max_paginas):
        paginas += 1
        linhas = []
        for it in itens:
            total += 1
            bn = int(it["block_number"])
            menor = bn if menor is None else min(menor, bn)
            maior = bn if maior is None else max(maior, bn)
            d = decode_log({"topics": it.get("topics") or [],
                            "data": it.get("data") or "0x"})
            if not d:
                continue                  # evento que nao e nosso: ignora
            nome, args = d
            linhas.append({
                "pool_address": pool.lower(),
                "block_number": bn,
                "log_index": int(it["index"]),
                "tx_hash": it["transaction_hash"],
                "tx_index": TX_INDEX_AUSENTE,
                "block_hash": it["block_hash"],
                "ts": _ts(it["block_timestamp"]),
                "event": nome,
                "args": args,
            })
        if linhas:
            gravados += _gravar_com_espera(linhas)
        if paginas % 25 == 0:
            log.info("blockscout_progresso", extra={
                "pool": pool[:10], "paginas": paginas, "logs": total,
                "bloco": menor})
    return {"pool": pool, "paginas": paginas, "logs_lidos": total,
            "eventos_gravados": gravados, "bloco_min": menor,
            "bloco_max": maior, "segundos": time.time() - t0,
            "fonte": "blockscout (secundaria)"}


__all__ = ["coletar", "paginar", "BASE", "UA", "TX_INDEX_AUSENTE"]
