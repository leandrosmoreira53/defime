"""Estado para o MCP: escrita atomica, fora do site, e falha nunca derruba o job."""

import json
import os

from defime import estado


def test_grava_com_carimbo_e_permissao_restrita(tmp_path, monkeypatch):
    monkeypatch.setenv("DEFIME_ESTADO_DIR", str(tmp_path))
    p = estado.gravar_estado("carteira", {"roi": 1.5, "obj": object()}, agora=1000)
    d = json.loads(p.read_text(encoding="utf-8"))
    assert d["gerado_em"] == 1000 and d["dados"]["roi"] == 1.5
    assert isinstance(d["dados"]["obj"], str)              # nao-serializavel vira texto, nao excecao
    if os.name == "posix":
        assert oct(p.stat().st_mode & 0o777) == "0o640"    # nao legivel por "outros"
    assert not list(tmp_path.glob(".*.tmp"))               # temporario foi renomeado, nao sobrou


def test_sem_diretorio_nao_grava_e_nao_levanta(tmp_path, monkeypatch):
    monkeypatch.setenv("DEFIME_ESTADO_DIR", str(tmp_path / "nao-existe"))
    assert estado.gravar_estado("carteira", {"a": 1}) is None


def test_falha_de_escrita_nao_levanta(tmp_path, monkeypatch):
    monkeypatch.setenv("DEFIME_ESTADO_DIR", str(tmp_path))

    def explode(*a, **k):
        raise OSError("disco cheio")

    monkeypatch.setattr(estado.os, "replace", explode)
    assert estado.gravar_estado("carteira", {"a": 1}) is None
