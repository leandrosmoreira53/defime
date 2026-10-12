"""O que um job JA calculou, gravado como JSON para um leitor externo (ex.: o mcp-defime).

Por que existe: refazer as contas no leitor seria uma SEGUNDA implementacao de cada numero e
custaria RPC de novo. Aqui o job grava o dicionario que acabou de montar para a pagina, e o leitor so le.

  - desligado por padrao: so grava se `DEFIME_ESTADO_DIR` apontar para um diretorio existente;
  - escrita ATOMICA (arquivo temporario + rename): o leitor nunca le meio arquivo;
  - carimbo `gerado_em` junto, para o leitor saber a idade e recusar dado velho;
  - falha aqui NUNCA derruba o job: a pagina e o produto, o JSON e acessorio.
    A falha vai para o log com o motivo.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

DIRETORIO_PADRAO = ""  # vazio = não grava (DEFIME_ESTADO_DIR liga)


def diretorio() -> Path | None:
    v = os.environ.get("DEFIME_ESTADO_DIR", DIRETORIO_PADRAO)
    if not v:
        return None
    d = Path(v)
    return d if d.is_dir() else None


def gravar_estado(nome: str, dados: Any, agora: float | None = None) -> Path | None:
    """Grava `<dir>/<nome>.json`. Devolve o caminho, ou None se nao gravou (e diz por que no log)."""
    d = diretorio()
    if d is None:
        log.info("estado %s nao gravado: diretorio %s nao existe", nome,
                 os.environ.get("DEFIME_ESTADO_DIR", DIRETORIO_PADRAO))
        return None
    destino = d / f"{nome}.json"
    try:
        corpo = json.dumps({"gerado_em": int(agora if agora is not None else time.time()),
                            "dados": dados}, default=str, ensure_ascii=False)
        fd, tmp = tempfile.mkstemp(dir=d, prefix=f".{nome}.", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(corpo)
        os.chmod(tmp, 0o640)
        os.replace(tmp, destino)
        return destino
    except Exception as exc:                                  # noqa: BLE001
        log.warning("estado %s nao gravado: %s: %s", nome, type(exc).__name__, exc)
        return None
