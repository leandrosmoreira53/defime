"""Fase 6 — provider abstrato e degradacao graciosa.

O PLAN.md define a escada: `ManualCSV` -> `Delayed` -> `IBKR`. A ordem nao e
sobre qualidade de dado, e sobre **dependencia**: o sistema tem que decidir bem
com zero feed macro ligado, e melhor com eles. Um motor que para de funcionar
porque uma API caiu nao e robusto, e um que muda de opiniao porque uma API caiu
e pior ainda.

Por isso a regra desta camada: **toda fonte pode estar ausente, e a ausencia e
declarada, nunca suprida por chute.** `Indisponivel` nao e erro — e um estado.

O que esta implementado e testado offline:
  - o calendario da EIA (quarta 10:30 ET) — deterministico, sem fonte externa
  - o vencimento dos contratos CL — regra publica da CME, deterministica
  - leitura de CSV manual para curva de futuros e estoques

O que NAO foi testado aqui, e por que:
  - GDELT, CME e api.eia.gov sao inalcancaveis do sandbox do agente (medido:
    GDELT e CME sem resposta, EIA devolve 403 sem chave). Os providers estao
    escritos e marcados como **nao testaveis aqui** — nao como funcionando.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path

from ..config import ROOT

DADOS = ROOT / "dados_macro"


@dataclass
class Leitura:
    """Resultado de uma fonte. `disponivel=False` e estado, nao excecao."""

    fonte: str
    disponivel: bool
    dados: dict = field(default_factory=dict)
    motivo: str | None = None
    ao_vivo: bool = False        # False => veio de CSV manual ou cache

    def resumo(self) -> dict:
        return {"fonte": self.fonte, "disponivel": self.disponivel,
                "ao_vivo": self.ao_vivo, "motivo": self.motivo}


class Provider:
    """Interface. Implementacao NUNCA levanta: devolve Leitura indisponivel."""

    nome = "abstrato"

    def ler(self, **kw) -> Leitura:
        raise NotImplementedError

    def _falha(self, motivo: str) -> Leitura:
        return Leitura(self.nome, False, motivo=motivo)


class ManualCSV(Provider):
    """Le um CSV que o usuario coloca em `dados_macro/`.

    E o degrau de baixo da escada e o unico que funciona sem rede. Formato
    minimo, uma linha de cabecalho; colunas extras sao preservadas.
    """

    nome = "manual_csv"

    def __init__(self, arquivo: str, colunas_exigidas: tuple[str, ...] = ()):
        self.arquivo = arquivo
        self.colunas = colunas_exigidas

    @property
    def caminho(self) -> Path:
        return DADOS / self.arquivo

    def ler(self, **kw) -> Leitura:
        if not self.caminho.exists():
            return self._falha(f"{self.caminho} nao existe")
        try:
            with self.caminho.open(encoding="utf-8", newline="") as fh:
                linhas = list(csv.DictReader(fh))
        except Exception as exc:  # noqa: BLE001
            return self._falha(f"{type(exc).__name__}: {exc}")
        if not linhas:
            return self._falha("csv vazio")
        faltando = [c for c in self.colunas if c not in linhas[0]]
        if faltando:
            return self._falha(f"colunas ausentes: {faltando}")
        return Leitura(self.nome, True, {"linhas": linhas, "n": len(linhas)},
                       ao_vivo=False)


class Encadeado(Provider):
    """Tenta os providers em ordem e devolve o primeiro disponivel.

    A degradacao e a funcionalidade principal, nao o caso de erro: com a lista
    inteira indisponivel devolve `Leitura` indisponivel com o motivo de cada
    degrau, para o relatorio poder dizer o que faltou.
    """

    def __init__(self, *providers: Provider, nome: str = "encadeado"):
        self.providers = providers
        self.nome = nome

    def ler(self, **kw) -> Leitura:
        motivos = []
        for p in self.providers:
            try:
                r = p.ler(**kw)
            except Exception as exc:  # noqa: BLE001
                motivos.append(f"{p.nome}: {type(exc).__name__}: {exc}")
                continue
            if r.disponivel:
                return r
            motivos.append(f"{p.nome}: {r.motivo}")
        return Leitura(self.nome, False, motivo="; ".join(motivos) or "sem providers")


__all__ = ["Provider", "ManualCSV", "Encadeado", "Leitura", "DADOS"]
