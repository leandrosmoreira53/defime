"""Volatilidade IMPLICITA da acao, da cadeia de opcoes. O que o mercado espera.

Todo sigma que este projeto usa e OLHANDO PARA TRAS: desvio de retornos que ja
aconteceram. A IV e a unica medida que olha para a frente, e ela e literalmente
o preco que alguem esta pagando hoje por proteger um movimento futuro.

Isso importa para a faixa porque a pergunta da faixa e sobre o futuro. Medido
em 2026-08-31 na MU:

    sigma historico, 3 anos          33,8%
    sigma do trimestre recente       52,3%
    **IV do mercado, 2 dias          ~51%**

A IV concorda com o regime RECENTE e nao com a media longa — o que e uma
validacao independente de que dimensionar a faixa pela media de tres anos a
deixaria estreita demais.

---

**O endpoint exige cookie e crumb**, e sem eles responde `401`. O fluxo e:
tocar `fc.yahoo.com` para receber o cookie (a requisicao em si FALHA, e isso e
esperado), pedir o crumb em `/v1/test/getcrumb` com esse cookie, e so entao
consultar. Sem o passo do cookie o crumb volta vazio e a consulta da 401 de
novo — parece falta de permissao e e falta de sessao.

**IV ATM, nao a da cadeia inteira.** A IV varia com o strike (o "sorriso"), e
pegar a media de todos os strikes mistura o dinheiro com as pontas, onde ha
premio de cauda e liquidez ruim. O que responde "quanto o mercado espera que
ande" e a IV no dinheiro.

**Contrato sem interesse em aberto e sem volume nao entra.** IV de opcao que
ninguem negocia e um numero calculado sobre um preco que nao existe. Uma vez
por semana isso derrubaria o termo inteiro com um strike fantasma.
"""

from __future__ import annotations

import datetime as dt
import http.cookiejar
import json
import math
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

UA = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                     "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 "
                     "Safari/537.36")}
TIMEOUT_S = 30
OI_MINIMO = 10          # contratos em aberto para o strike valer


@dataclass(frozen=True)
class Vencimento:
    data: dt.date
    dias: float
    iv_atm: float             # fracao, nao porcento
    strike_atm: float
    n_strikes: int

    def movimento_esperado_pct(self) -> float:
        """Um desvio-padrao ate o vencimento, em % do spot.

        `sigma x raiz(T)`, com T em anos de CALENDARIO — nao de pregao. A
        opcao expira numa data, e o fim de semana passa mesmo com a bolsa
        fechada.
        """
        return 100 * self.iv_atm * math.sqrt(max(self.dias, 0.0) / 365.0)


@dataclass
class Cadeia:
    simbolo: str
    spot: float | None = None
    vencimentos: list[Vencimento] = field(default_factory=list)
    disponivel: bool = False
    motivo: str | None = None

    def iv_para(self, dias: float) -> float | None:
        """IV interpolada para um horizonte, em fracao.

        Interpola em VARIANCIA acumulada (`iv^2 x t`), nao em IV: a variancia
        e que soma no tempo. Interpolar a IV direto embute uma curva de termo
        que nao existe e erra sempre na mesma direcao.
        """
        v = [x for x in self.vencimentos if x.dias > 0]
        if not v:
            return None
        if dias <= v[0].dias:
            return v[0].iv_atm
        if dias >= v[-1].dias:
            return v[-1].iv_atm
        for a, b in zip(v, v[1:]):
            if a.dias <= dias <= b.dias:
                va, vb = a.iv_atm ** 2 * a.dias, b.iv_atm ** 2 * b.dias
                peso = (dias - a.dias) / (b.dias - a.dias)
                return math.sqrt((va + peso * (vb - va)) / dias)
        return None

    def resumo(self) -> dict:
        return {"simbolo": self.simbolo, "spot": self.spot,
                "disponivel": self.disponivel, "motivo": self.motivo,
                "n_vencimentos": len(self.vencimentos),
                "termo": [{"data": v.data.isoformat(), "dias": round(v.dias, 2),
                           "iv_pct": round(100 * v.iv_atm, 2),
                           "movimento_1sigma_pct": round(
                               v.movimento_esperado_pct(), 2)}
                          for v in self.vencimentos]}


def _sessao():
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    try:
        # Falha por desenho — o que interessa e o cookie que vem junto.
        op.open(urllib.request.Request("https://fc.yahoo.com", headers=UA),
                timeout=TIMEOUT_S)
    except Exception:                                         # noqa: BLE001, S110
        pass
    crumb = op.open(urllib.request.Request(
        "https://query1.finance.yahoo.com/v1/test/getcrumb", headers=UA),
        timeout=TIMEOUT_S).read().decode().strip()
    return op, crumb


def _iv_atm(contratos: list[dict], spot: float) -> tuple[float, float, int]:
    """IV do strike mais proximo do dinheiro, com liquidez. (iv, strike, n)."""
    vivos = [c for c in contratos
             if (c.get("openInterest") or 0) >= OI_MINIMO
             and c.get("impliedVolatility")
             and 0.01 < c["impliedVolatility"] < 5.0]
    if not vivos:
        return 0.0, 0.0, 0
    # Media de call e put no mesmo strike quando as duas existem: put-call
    # parity manda serem iguais, e a diferenca e ruido de bid-ask.
    melhor = min(vivos, key=lambda c: abs(c["strike"] - spot))
    perto = [c for c in vivos if abs(c["strike"] - melhor["strike"]) < 1e-9]
    iv = sum(c["impliedVolatility"] for c in perto) / len(perto)
    return iv, float(melhor["strike"]), len(vivos)


def ler(simbolo: str, max_vencimentos: int = 8,
        agora: dt.datetime | None = None) -> Cadeia:
    """Termo de volatilidade implicita. Nunca levanta."""
    agora = agora or dt.datetime.now(dt.UTC)
    try:
        op, crumb = _sessao()
        if not crumb:
            return Cadeia(simbolo, disponivel=False,
                          motivo="crumb vazio: sessao nao estabelecida")
        base = (f"https://query1.finance.yahoo.com/v7/finance/options/"
                f"{simbolo}?crumb={urllib.parse.quote(crumb)}")
        bruto = json.load(op.open(
            urllib.request.Request(base, headers=UA), timeout=TIMEOUT_S))
    except Exception as exc:                                  # noqa: BLE001
        return Cadeia(simbolo, disponivel=False,
                      motivo=f"{type(exc).__name__}: {exc}"[:140])

    res = ((bruto.get("optionChain") or {}).get("result") or [None])[0]
    if not res:
        return Cadeia(simbolo, disponivel=False, motivo="cadeia vazia")
    spot = (res.get("quote") or {}).get("regularMarketPrice")
    if not spot:
        return Cadeia(simbolo, disponivel=False, motivo="sem preco a vista")

    datas = res.get("expirationDates") or []
    vencs: list[Vencimento] = []
    for i, ts in enumerate(datas[:max_vencimentos]):
        try:
            if i == 0:
                pacote = res["options"][0]
            else:
                pacote = json.load(op.open(urllib.request.Request(
                    base + f"&date={ts}", headers=UA), timeout=TIMEOUT_S)
                )["optionChain"]["result"][0]["options"][0]
        except Exception:                                     # noqa: BLE001
            continue
        iv, strike, n = _iv_atm(
            (pacote.get("calls") or []) + (pacote.get("puts") or []), spot)
        if not iv:
            continue
        data = dt.datetime.fromtimestamp(ts, dt.UTC).date()
        dias = (ts - agora.timestamp()) / 86400
        vencs.append(Vencimento(data, dias, iv, strike, n))
    if not vencs:
        return Cadeia(simbolo, spot=spot, disponivel=False,
                      motivo="nenhum vencimento com strike liquido")
    return Cadeia(simbolo, spot=spot, vencimentos=vencs, disponivel=True)


def largura_sugerida(cadeia: Cadeia, dias: float, sigmas: float = 1.0
                     ) -> dict | None:
    """Largura de faixa que cobre `sigmas` desvios ate `dias`, pela IV.

    E a ponte entre a cadeia de opcoes e a decisao de range: a largura TOTAL
    e `2 x sigmas x IV x raiz(T)`, porque a faixa vai para os dois lados.

    Nao substitui a medicao de sobrevivencia — o mercado de opcoes precifica
    a distribuicao inteira, incluindo a cauda que a faixa nao aguenta. Serve
    como segunda opiniao, e discordancia grande entre as duas e sinal, nao
    ruido.
    """
    iv = cadeia.iv_para(dias)
    if iv is None:
        return None
    meia = sigmas * iv * math.sqrt(dias / 365.0)
    return {"dias": dias, "sigmas": sigmas, "iv_pct": round(100 * iv, 2),
            "largura_total_pct": round(200 * meia, 2),
            "piso": round(cadeia.spot * (1 - meia), 2) if cadeia.spot else None,
            "teto": round(cadeia.spot * (1 + meia), 2) if cadeia.spot else None}


__all__ = ["Vencimento", "Cadeia", "ler", "largura_sugerida", "OI_MINIMO"]
