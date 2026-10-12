"""As tres tabelas de noticia por ativo — gravar e LER.

Uma funcao de leitura so (`ler`), porque o `/heat/`, o `/grafico/` e o
`estado.py` que alimenta o MCP vao perguntar a mesma coisa. Tres copias da
consulta divergiriam no primeiro ajuste — a familia da `_horas_fora`.

    noticia           uma linha por MATERIA (dedupe por link e por titulo)
    noticia_simbolo   N:N — uma materia de chips e de MU, NVDA e SNDK
    noticia_leitura   uma linha por TENTATIVA de fonte x simbolo, com ok/erro

A terceira e a que separa "a fonte ficou muda" de "nao houve noticia". Sem ela
a pagina nao teria como dizer a diferenca.

**Sem corpo do artigo.** Titulo, veiculo, link e horario; o link leva ao texto.
**Medicao datada nao se sobrescreve** (`insert or ignore`), e timestamps sao
epoch UTC.
"""

from __future__ import annotations

import sqlite3

from .noticias_ativos import Lida

ESQUEMA = """
create table if not exists noticia(
    id            text primary key,
    fonte         text not null,
    titulo        text not null,
    titulo_norm   text not null,
    publisher     text,
    link          text not null,
    publicado_em  integer not null,
    coletado_em   integer not null,
    n_tickers     integer,
    eventos       text);
create index if not exists ix_noticia_norm on noticia(titulo_norm);
create index if not exists ix_noticia_pub  on noticia(publicado_em desc);

create table if not exists noticia_simbolo(
    noticia_id text not null,
    simbolo    text not null,
    no_titulo  integer not null default 0,   -- a manchete NOMEIA o ativo?
    primary key (noticia_id, simbolo));
create index if not exists ix_noticia_sim on noticia_simbolo(simbolo);

create table if not exists noticia_leitura(
    fonte   text not null,
    simbolo text not null,
    ts      integer not null,
    ok      integer not null,
    n_itens integer not null,
    erro    text,
    primary key (fonte, simbolo, ts));
"""


def preparar(c: sqlite3.Connection) -> None:
    c.executescript(ESQUEMA)


def gravar(c: sqlite3.Connection, lida: Lida, ts: int) -> int:
    """Grava a tentativa e os itens dela. Devolve quantas materias NOVAS.

    Dedupe em dois niveis: o `id` e o hash do link canonico, e antes dele se
    procura o `titulo_norm` — a mesma materia chega pelo Yahoo e pelo Google
    com links diferentes. Achando, so a ligacao ao simbolo e acrescentada.
    """
    c.execute("insert or ignore into noticia_leitura values (?,?,?,?,?,?)",
              (lida.fonte, lida.simbolo, ts, int(lida.ok), len(lida.itens),
               lida.erro))
    novas = 0
    for it in lida.itens:
        achado = c.execute("select id from noticia where titulo_norm=? limit 1",
                           (it.titulo_norm,)).fetchone()
        nid = achado[0] if achado else it.id
        if not achado:
            cur = c.execute(
                "insert or ignore into noticia values (?,?,?,?,?,?,?,?,?,?)",
                (nid, it.fonte, it.titulo, it.titulo_norm, it.publisher,
                 it.link, it.publicado_em, ts, it.n_tickers,
                 ",".join(it.eventos) or None))
            novas += cur.rowcount
        # a mesma materia por outra fonte pode NOMEAR o que a primeira so citava
        c.executemany(
            "insert into noticia_simbolo values (?,?,?) "
            "on conflict(noticia_id, simbolo) do update set "
            "no_titulo = max(no_titulo, excluded.no_titulo)",
            [(nid, s, int(s in it.no_titulo)) for s in it.simbolos])
    return novas


def ler(c: sqlite3.Connection, *, agora: int, simbolo: str | None = None,
        horas: float = 24.0, limite: int = 20,
        so_eventos: bool = False) -> list[dict]:
    """Manchetes da janela, mais nova primeiro, com os simbolos de cada uma.

    `simbolos` e a atribuicao da FONTE; `nomeia` e o subconjunto que o
    titulo cita pelo nome ou ticker. Quem publica "o que moveu X" deve
    preferir `nomeia`: resumo de mercado entra em `simbolos` de todo mundo.
    """
    corte = int(agora - horas * 3600)
    sql = ("select n.id, n.titulo, n.publisher, n.link, n.publicado_em, "
           "n.fonte, n.eventos, n.n_tickers, "
           "(select group_concat(simbolo) from noticia_simbolo x "
           " where x.noticia_id = n.id), "
           "(select group_concat(simbolo) from noticia_simbolo x "
           " where x.noticia_id = n.id and x.no_titulo = 1) "
           "from noticia n where n.publicado_em >= ? and n.publicado_em <= ?")
    args: list = [corte, agora]
    if simbolo:
        sql += (" and exists (select 1 from noticia_simbolo s "
                "where s.noticia_id = n.id and s.simbolo = ?)")
        args.append(simbolo.upper())
    if so_eventos:
        # Evento so conta para o ativo que a manchete NOMEIA: "General Mills
        # Reaffirms Guidance" veio marcada pelo Yahoo com NVDA.
        sql += (" and n.eventos is not null and exists (select 1 from "
                "noticia_simbolo e where e.noticia_id = n.id and e.no_titulo = 1"
                + (" and e.simbolo = ?)" if simbolo else ")"))
        if simbolo:
            args.append(simbolo.upper())
    sql += " order by n.publicado_em desc limit ?"
    args.append(int(limite))
    return [{"id": r[0], "titulo": r[1], "publisher": r[2], "link": r[3],
             "publicado_em": r[4], "idade_h": round((agora - r[4]) / 3600, 2),
             "fonte": r[5],
             "eventos": r[6].split(",") if r[6] else [],
             "n_tickers": r[7],
             "simbolos": sorted(r[8].split(",")) if r[8] else [],
             "nomeia": sorted(r[9].split(",")) if r[9] else []}
            for r in c.execute(sql, args)]


def ultima_leitura(c: sqlite3.Connection) -> dict[str, dict]:
    """Por simbolo: quando alguma fonte respondeu pela ultima vez, e a ultima
    tentativa. E daqui que sai o "fonte muda desde HH:MM"."""
    saida: dict[str, dict] = {}
    for sim, ts_ok, ts_ult in c.execute(
            "select simbolo, max(case when ok=1 then ts end), max(ts) "
            "from noticia_leitura group by simbolo"):
        saida[sim] = {"ultima_ok": ts_ok, "ultima_tentativa": ts_ult,
                      "sem_leitura": ts_ok is None or ts_ok < ts_ult}
    return saida


__all__ = ["ESQUEMA", "preparar", "gravar", "ler", "ultima_leitura"]
