"""Sessao de mercado a partir de um timestamp UTC. Camada 2 — sem I/O.

O plano do projeto pedia "dentro do pregao, volatilidade cheia; fora do pregao,
volatilidade reduzida porem nao-zero". A medicao desta pool NAO sustenta essa
premissa (ver `decision/simulate.py`, secao de calibracao): os fatores de
sessao ficam todos perto de 1,0 e os intervalos de confianca se sobrepoem. O
mecanismo existe aqui porque e barato e correto te-lo; os NUMEROS vem dos
dados, nao da intuicao, e hoje eles dizem "quase nao ha efeito de calendario".

Fronteiras em horario de Nova York, nao em UTC fixo: o periodo indexado ate
agora e todo EDT, mas o codigo sobrevive a mudanca de horario de verao.

LIMITACAO CONHECIDA: feriados da NYSE nao sao tratados. Nao morde hoje (nao ha
nenhum na vida da pool: 2026-07-17 a 2026-08-15), mas morde no Labor Day.
`feriados` existe para quando isso importar.
"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")

PREGAO = "pregao"                # 09:30-16:00 ET, dia util
PRE = "pre"                      # 04:00-09:30 ET — sessao de Londres, dia util
POS = "pos"                      # 16:00-20:00 ET, dia util
MADRUGADA = "madrugada"          # 20:00-04:00 ET, dia util
FIM_DE_SEMANA = "fim_de_semana"  # sabado e domingo, hora de NY

SESSOES = (PREGAO, PRE, POS, MADRUGADA, FIM_DE_SEMANA)


def sessao_de(ts: int, feriados: frozenset[str] = frozenset()) -> str:
    """Classifica um timestamp UTC (segundos) numa das cinco sessoes."""
    d = datetime.fromtimestamp(ts, timezone.utc).astimezone(NY)
    if d.weekday() >= 5 or d.date().isoformat() in feriados:
        return FIM_DE_SEMANA
    minutos = d.hour * 60 + d.minute
    if 9 * 60 + 30 <= minutos < 16 * 60:
        return PREGAO
    if 4 * 60 <= minutos < 9 * 60 + 30:
        return PRE
    if 16 * 60 <= minutos < 20 * 60:
        return POS
    return MADRUGADA


def abertura_seguinte(ts: int, feriados: frozenset[str] = frozenset()) -> int:
    """Proximo instante de abertura do pregao (09:30 ET) a partir de `ts`."""
    passo = 60
    t = ts - ts % passo + passo
    atual = sessao_de(t, feriados)
    limite = t + 8 * 86400          # so para nao girar para sempre
    while t < limite:
        prox = sessao_de(t + passo, feriados)
        if prox == PREGAO and atual != PREGAO:
            return t + passo
        atual = prox
        t += passo
    return limite



# Fator de VOLUME por sessao, medido nesta chain (ver documento de regras do projeto, secao "Fim de
# semana: sigma igual, volume pela metade"). Sao os numeros que fazem uma hora
# de pregao valer 2,9 horas de fim de semana em fee arrecadada.
FATOR_VOLUME = {
    PRE: 1.825,
    PREGAO: 1.320,
    POS: 0.800,
    MADRUGADA: 0.721,
    FIM_DE_SEMANA: 0.462,
}

# Normalizador: a media dos fatores ao longo de uma SEMANA inteira, ponderada
# pela duracao de cada sessao. Sem ele `dias_equivalentes` de um mes daria
# ~0,95 mes e todo numero maduro ja publicado mudaria de valor — a correcao
# tem que ser neutra no limite longo e so morder no pedaco de dia.
_HORAS_NA_SEMANA = {
    PRE: 5 * 5.5, PREGAO: 5 * 6.5, POS: 5 * 4.0,
    MADRUGADA: 5 * 8.0, FIM_DE_SEMANA: 2 * 24.0,
}
_MEDIA_SEMANAL = (
    sum(FATOR_VOLUME[s] * h for s, h in _HORAS_NA_SEMANA.items())
    / sum(_HORAS_NA_SEMANA.values()))

PASSO_S = 300


def fator_de_volume(ts: int, feriados: frozenset[str] = frozenset()) -> float:
    """Quanto volume aquele instante entrega, com 1,0 = a media da semana."""
    return FATOR_VOLUME[sessao_de(ts, feriados)] / _MEDIA_SEMANAL


def dias_equivalentes(t0: float, t1: float,
                      feriados: frozenset[str] = frozenset()) -> float:
    """Quantos DIAS MEDIOS de mercado cabem na janela [t0, t1].

    **Existe para que a taxa diaria nunca precise dizer "medindo".** Dividir a
    fee pelo tempo de relogio supoe que toda hora rende igual, e esta pool
    mediu o contrario: pre 1,825x, pregao 1,320x, pos 0,800x, madrugada 0,721x
    e fim de semana 0,462x. Foi essa suposicao que fez a POS_D publicar
    $305/dia — 770% ao ano — a partir de 7,6 horas que eram inteiras de
    pregao, o pedaco mais caro do dia.

    Aqui o denominador deixa de ser o relogio e passa a ser o volume: 7,6h de
    pregao valem 0,44 dia medio, nao 0,32, e a taxa sai 27% menor sem que
    ninguem escolha um fator a mao. **A resposta certa nao era esconder o
    numero, era corrigir o denominador.**

    No limite longo a correcao some por construcao (`_MEDIA_SEMANAL`), entao
    posicao madura devolve o mesmo que `(t1 - t0) / 86400`. Ela so morde onde
    a amostra e um pedaco de dia — que e exatamente onde ela precisa morder.
    """
    if t1 <= t0:
        return 0.0
    soma, t = 0.0, float(t0)
    while t < t1:
        passo = min(PASSO_S, t1 - t)
        soma += fator_de_volume(int(t), feriados) * passo
        t += passo
    return soma / 86400.0


__all__ = ["sessao_de", "abertura_seguinte", "SESSOES", "NY",
           "PREGAO", "PRE", "POS", "MADRUGADA", "FIM_DE_SEMANA",
           "FATOR_VOLUME", "fator_de_volume", "dias_equivalentes"]
