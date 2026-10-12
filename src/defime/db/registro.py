"""Gravadores das tres tabelas de serie: posicao, decisao e batimento.

Ficam juntos porque compartilham a mesma regra: **medicao datada nunca
sobrescreve.** Todas usam `on_conflict_do_nothing` — rodar duas vezes no mesmo
segundo nao duplica nem apaga, e um coletor que reinicia nao corrompe a serie.

E todas seguem a regra do `base.py` do macro: **ausencia se declara, nunca vira
zero.** `fee_pendente_usd=None` significa "nao medi"; `0.0` significaria "medi e
deu zero", que sao coisas diferentes e ja custaram caro neste projeto.
"""

from __future__ import annotations

import time

from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from .models import ColetorHeartbeat, Decisao, PosicaoSnapshot
from .session import session_scope


def gravar_batimento(coletor: str, *, ok: bool = True, itens: int = 0,
                     duracao_s: float = 0.0, erro: str | None = None,
                     nota: str | None = None, ts: int | None = None) -> None:
    """Um batimento por ciclo. NUNCA levanta — coletor nao pode morrer por log.

    `ok=False` com `erro` tambem e batimento: ciclo que tentou e falhou e
    informacao diferente de ciclo que nao aconteceu. Essa distincao e a razao
    da tabela existir — em 2026-08-27/29 os coletores caíram tres vezes e as
    tres foram silenciosas.
    """
    try:
        with session_scope() as sess:
            sess.execute(
                sqlite_insert(ColetorHeartbeat).on_conflict_do_nothing(),
                [{"coletor": coletor[:32], "ts": ts or int(time.time()),
                  "ok": bool(ok), "itens": int(itens),
                  "duracao_s": float(duracao_s),
                  "erro": (str(erro)[:300] if erro else None),
                  "nota": (str(nota)[:200] if nota else None)}])
    except Exception:  # noqa: BLE001
        pass          # registrar batimento nunca pode derrubar o coletor


_ULTIMO_BAT: dict[str, float] = {}


def batimento_periodico(coletor: str, *, minimo_s: float = 900.0,
                        **kw) -> bool:
    """Batimento para quem roda em laco APERTADO (feed a cada 15s, tail a 5s).

    Estrangula EM PROCESSO, sem consultar o banco: 5s de cadencia dariam 17.280
    linhas por dia e a tabela viraria log de debug. A 900s dao ~96, que e o
    suficiente para o `check-1` medir buraco de cobertura com resolucao de 15min.

    O estrangulamento e por processo, de proposito: reiniciar o coletor
    **sempre** grava o primeiro batimento. E exatamente o evento que interessa —
    processo que voltou — e um throttle persistido o esconderia.

    Devolve True se gravou.
    """
    import time as _t
    agora = _t.monotonic()
    ultimo = _ULTIMO_BAT.get(coletor)
    if ultimo is not None and (agora - ultimo) < minimo_s:
        return False
    _ULTIMO_BAT[coletor] = agora
    gravar_batimento(coletor, **kw)
    return True


def gravar_posicao(linhas: list[dict]) -> int:
    """Snapshots de posicao. `fee_pendente_usd` ausente fica None, nao 0."""
    linhas = [x for x in linhas if x.get("pool_address") and x.get("token_id")]
    if not linhas:
        return 0
    with session_scope() as sess:
        sess.execute(sqlite_insert(PosicaoSnapshot).on_conflict_do_nothing(),
                     linhas)
    return len(linhas)


def gravar_decisao(rel: dict, *, pool: str, ts: int | None = None) -> bool:
    """Extrai a decisao do relatorio e grava. Devolve False se nao havia uma.

    Relatorio sem decisao acontece — gate bloqueante, posicao nao encontrada,
    coletor atrasado. Gravar `acao=None` seria inventar; devolver False deixa o
    chamador dizer que nao houve.

    **Os nomes das chaves foram lidos dos 39 relatorios em `relatorios/`, nao
    supostos.** A primeira versao procurava `decisao["vantagem"]["mediana"]` e
    `decisao["detalhes"]["alvo_de_vantagem_usd"]`; o motor publica
    `vantagem_usd` e `alvo_de_vantagem_usd`, os dois na raiz de `decisao`. Nada
    teria falhado: as colunas ficariam NULL, que se le como "nao medido" quando
    na verdade foi medido. E a mesma familia do dado velho entregue em silencio.
    """
    dec = rel.get("decisao")
    if not dec or not dec.get("acao"):
        return False
    pos = rel.get("nossa_posicao") or {}
    cal = rel.get("calibracao") or {}
    van = dec.get("vantagem_usd") or {}
    linha = {
        "pool_address": pool,
        "ts": ts or int((rel.get("instante") or {}).get("ts") or time.time()),
        "token_id": pos.get("token_id"),
        "acao": str(dec["acao"])[:16],
        "motivo": str(dec.get("motivo") or "")[:400],
        "gates_bloqueantes": dec.get("gates_bloqueantes") or [],
        "gates": dec.get("gates") or [],
        "vantagem_mediana_usd": van.get("mediana"),
        "vantagem_p25_usd": van.get("p25"),
        "alvo_usd": dec.get("alvo_de_vantagem_usd"),
        "custo_usd": dec.get("custo_usd"),
        "simulacoes": int(dec.get("simulacoes_executadas") or 0),
        "preco_usd": (rel.get("instante") or {}).get("preco_usd"),
        "sigma_pct": cal.get("sigma_anualizado_pct"),
        "fator_macro": (rel.get("macro") or {}).get("fator_sigma"),
        # O vetor de features do inviolavel 7: o que o motor VIU ao decidir.
        # `hold` e `candidato` entram inteiros porque sao a saida do Monte
        # Carlo que produziu a vantagem — sem eles o replay nao reconstitui a
        # comparacao, so o veredito dela.
        "features": {
            "faixa_usd": pos.get("faixa_usd"),
            "em_range": pos.get("em_range"),
            "capital_usd": pos.get("capital_usd"),
            "fracao_usdg": pos.get("fracao_usdg"),
            "largura_ticks": pos.get("largura_ticks"),
            "largura_em_sigmas": pos.get("largura_em_sigmas"),
            "assimetria": pos.get("assimetria"),
            "idade_dias": pos.get("idade_dias"),
            "unidade_da_grade_usd": cal.get("unidade_da_grade_usd"),
            "sigma_ic95_pct": cal.get("sigma_ic95_pct"),
            "volume_usd_por_dia": cal.get("volume_usd_por_dia"),
            "basis_pct": (rel.get("referencia") or {}).get("basis_pct"),
            "consenso_usd": (rel.get("consenso") or {}).get("faixa_usd"),
            "vantagem_usd": van or None,
            "melhor_candidato": dec.get("melhor_candidato"),
            "candidatos_avaliados": dec.get("candidatos_avaliados"),
            "candidatos_viaveis_p25": dec.get("candidatos_viaveis_p25"),
            "fator_de_exigencia": dec.get("fator_de_exigencia"),
            "horizonte_h": dec.get("horizonte_h"),
            "n_trajetorias": dec.get("n_trajetorias"),
            "seed": dec.get("seed"),
            "hold": dec.get("hold"),
            "candidato": dec.get("candidato"),
            "avisos": rel.get("avisos") or [],
        },
    }
    with session_scope() as sess:
        sess.execute(sqlite_insert(Decisao).on_conflict_do_nothing(), [linha])
    return True


__all__ = ["gravar_batimento", "batimento_periodico",
           "gravar_posicao", "gravar_decisao"]
