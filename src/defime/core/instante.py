"""Guarda contra a familia de bug mais frequente deste projeto.

**Cinco vezes o mesmo erro:** parear dados colhidos em INSTANTES diferentes.

1. `check-4`: soma das posicoes contra `slot0` errando 0,065% — reconstrucao
   ate um bloco, `slot0` de outro.
2. `check-history-1`: 0,170419% — dentro do MESMO bloco, um `Mint` no
   `log_index` 50 depois do ultimo `Swap` no 19.
3. `check-2`: reconstrucao de AGORA comparada com golden congelado no bloco
   37.318.521, acusando 100% de divergencia num bug que nao existia.
4. `check-3c`: gates avaliados na posicao de OUTRO LP, porque o "agora" da
   selecao e o "agora" da avaliacao divergiam.
5. **G1/G4** em 2026-08-23: preco de pool de agora contra fechamento de sexta,
   com o G4 declarando "basis anomalo de +5,83%" — veredito certo, motivo
   errado.

Cinco vezes nao e azar, e ausencia de estrutura. Nos cinco casos a regra estava
escrita em prosa e o codigo nao a cumpria, porque nada obrigava.

O que este modulo oferece nao e um tipo novo espalhado pelo projeto — seria
refatoracao grande e arriscada, e o `int` de tick/amount ja mostrou que
invariante bom e o que se verifica, nao o que se promete. E uma GUARDA
explicita, barata de chamar, que transforma "eu conferi" em "o codigo confere":

    exigir_compativel(
        Instante(ts=ts_pool, bloco=bloco, rotulo="preco da pool"),
        Instante(ts=ts_quote, rotulo="cotacao de referencia"),
        tolerancia_s=900)

A tolerancia e obrigatoria e nao tem default. Dois instantes "proximos o
bastante" depende inteiramente do que se esta pareando — 15 minutos serve para
basis, e seria absurdo para reconciliar liquidez, onde o certo e ZERO e a
comparacao e por `(bloco, log_index)`.
"""

from __future__ import annotations

from dataclasses import dataclass


class InstantesIncompativeis(ValueError):
    """Levantada quando se tenta parear dados de momentos diferentes."""


@dataclass(frozen=True)
class Instante:
    """Quando um dado foi colhido. `bloco` e `log_index` sao opcionais.

    `ts` sozinho nao basta na chain: com block time de 0,1s, varios blocos
    dividem o mesmo segundo, e dentro de um bloco a ORDEM importa — foi o erro
    de 0,170419% do `check-history-1`.
    """

    ts: int
    bloco: int | None = None
    log_index: int | None = None
    rotulo: str = ""

    @property
    def chave_de_ordem(self) -> tuple[int, int]:
        """Ordena dentro da chain. Sem bloco, cai no `ts`."""
        return (self.bloco if self.bloco is not None else self.ts,
                self.log_index if self.log_index is not None else 0)

    def resumo(self) -> dict:
        return {"rotulo": self.rotulo, "ts": self.ts, "bloco": self.bloco,
                "log_index": self.log_index}


def distancia_s(a: Instante, b: Instante) -> int:
    return abs(a.ts - b.ts)


def compativel(a: Instante, b: Instante, *, tolerancia_s: int) -> bool:
    return distancia_s(a, b) <= tolerancia_s


def exigir_compativel(*instantes: Instante, tolerancia_s: int) -> None:
    """Levanta se QUALQUER par estiver alem da tolerancia.

    Compara todos os pares, nao so consecutivos: tres fontes onde A~B e B~C mas
    A e C estao a 30 minutos passariam num teste de cadeia e falhariam no que
    importa.
    """
    if tolerancia_s < 0:
        raise ValueError("tolerancia_s negativa")
    for i, a in enumerate(instantes):
        for b in instantes[i + 1:]:
            if not compativel(a, b, tolerancia_s=tolerancia_s):
                raise InstantesIncompativeis(
                    f"{a.rotulo or 'fonte A'} (ts={a.ts}) e "
                    f"{b.rotulo or 'fonte B'} (ts={b.ts}) estao a "
                    f"{distancia_s(a, b)}s — tolerancia {tolerancia_s}s. "
                    "Parear os dois compararia momentos diferentes.")


def exigir_mesmo_ponto_da_chain(*instantes: Instante) -> None:
    """Para reconciliacao de liquidez: mesmo bloco E mesmo log_index.

    Aqui tolerancia em segundos nao serve. O `check-history-1` errou 0,170419%
    com tudo no MESMO bloco, porque um `Mint` estava depois do ultimo `Swap`.
    """
    faltando = [i for i in instantes if i.bloco is None]
    if faltando:
        raise InstantesIncompativeis(
            "reconciliacao de chain exige bloco: sem ele em "
            + ", ".join(i.rotulo or "sem rotulo" for i in faltando))
    chaves = {i.chave_de_ordem for i in instantes}
    if len(chaves) > 1:
        detalhe = ", ".join(f"{i.rotulo or '?'}@{i.bloco}:{i.log_index}"
                            for i in instantes)
        raise InstantesIncompativeis(
            f"pontos diferentes da chain: {detalhe}. Dentro do mesmo bloco a "
            "ORDEM importa — ver `lifecycle.bloco_de_corte`.")


def mais_velho(*instantes: Instante) -> Instante:
    """O gargalo de frescor de um conjunto de fontes."""
    return min(instantes, key=lambda i: i.ts)


def idade_do_conjunto_s(agora_ts: int, *instantes: Instante) -> int:
    """Idade do dado MAIS VELHO. E ela que limita o que se pode afirmar."""
    return agora_ts - mais_velho(*instantes).ts


__all__ = ["Instante", "InstantesIncompativeis", "compativel",
           "exigir_compativel", "exigir_mesmo_ponto_da_chain", "distancia_s",
           "mais_velho", "idade_do_conjunto_s"]
