"""Cliente JSON-RPC — a unica porta de saida para a chain.

Trata os tres fatos descobertos na Fase 0:
  1. o resolvedor DNS local filtra robinhood.com  -> contorno por DoH
  2. o RPC publico devolve 429                    -> backoff + intervalo minimo
  3. o RPC nao e archive                          -> chamadas de estado historico
     falham com "metadata is not found"; e erro esperado, nao bug

READ-ONLY: sem conta, sem assinatura. Metodos de escrita nao existem aqui.
"""

from __future__ import annotations

import json
import random
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from ..config import settings
from ..logging import get_logger

log = get_logger(__name__)

_dns_lock = threading.Lock()
_dns_installed = False


class RpcError(RuntimeError):
    def __init__(self, method: str, payload):
        self.method = method
        self.payload = payload
        super().__init__(f"{method}: {payload}")

    @property
    def is_missing_state(self) -> bool:
        """No nao-archive respondendo sobre bloco antigo. Esperado, nao bug."""
        return "metadata is not found" in str(self.payload)


def _doh_lookup(hostname: str, timeout: int = 15) -> list[str]:
    url = f"https://dns.google/resolve?name={hostname}&type=A"
    req = urllib.request.Request(
        url, headers={"Accept": "application/dns-json", "User-Agent": "uso-lp/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode())
    return [a["data"] for a in data.get("Answer", []) if a.get("type") == 1]


def install_dns_bypass(hostnames: list[str]) -> dict:
    """Mapeia hostname->IP so dentro deste processo.

    Hostname, SNI e validacao de certificado seguem intactos: apenas a fonte da
    resolucao muda. Nao ha downgrade de seguranca.
    """
    global _dns_installed
    with _dns_lock:
        if _dns_installed:
            return {}
        mapping = {}
        for host in hostnames:
            try:
                socket.gethostbyname(host)
                continue                      # resolve normalmente, nao mexer
            except OSError:
                pass
            try:
                ips = _doh_lookup(host)
                if ips:
                    mapping[host] = ips[0]
            except Exception as exc:  # noqa: BLE001
                log.warning("doh_falhou", host=host, erro=str(exc))
        if not mapping:
            return {}

        original = socket.getaddrinfo

        def patched(host, port, *args, **kwargs):
            if host in mapping:
                return original(mapping[host], port, *args, **kwargs)
            return original(host, port, *args, **kwargs)

        socket.getaddrinfo = patched
        _dns_installed = True
        log.info("dns_bypass_ativo", mapping=mapping)
        return mapping


class RpcClient:
    def __init__(self, url: str | None = None):
        cfg = settings()
        self.url = url or cfg.rpc_url
        self.timeout = cfg.request_timeout
        self.max_retries = cfg.max_retries
        self.min_interval = cfg.min_request_interval_s
        self.max_items_per_s = cfg.max_items_per_second
        self._last_call = 0.0
        self._lock = threading.Lock()
        self.calls = 0
        self.retries = 0

        if cfg.dns_bypass:
            host = urllib.parse.urlparse(self.url).hostname
            if host:
                install_dns_bypass([host])

    def _throttle(self, items: int = 1):
        """Budget por ITEM: o limitador do RPC conta sub-requisicoes do batch,
        nao requests HTTP. Ver nota em config.max_items_per_second."""
        needed = max(self.min_interval, items / self.max_items_per_s)
        gap = time.time() - self._last_call
        if gap < needed:
            time.sleep(needed - gap)

    def call(self, method: str, params: list | None = None):
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                           "params": params or []}).encode()
        req = urllib.request.Request(
            self.url, data=body,
            headers={"Content-Type": "application/json", "User-Agent": "uso-lp/0.1"})

        last = None
        for attempt in range(self.max_retries):
            with self._lock:
                self._throttle()
                try:
                    with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                        payload = json.loads(resp.read().decode())
                    self.calls += 1
                    self._last_call = time.time()
                except urllib.error.HTTPError as exc:
                    self._last_call = time.time()
                    last = f"HTTP {exc.code}"
                    if exc.code in (429, 502, 503, 504):
                        payload = None
                    else:
                        raise RpcError(method, last) from exc
                except Exception as exc:  # noqa: BLE001
                    self._last_call = time.time()
                    last = f"{type(exc).__name__}: {exc}"
                    payload = None

            if payload is not None:
                if "error" in payload:
                    raise RpcError(method, payload["error"])
                return payload.get("result")

            # backoff exponencial com jitter: o RPC publico rejeita rajada
            self.retries += 1
            delay = min(30.0, (2 ** attempt) * 0.5) + random.uniform(0, 0.4)
            log.warning("rpc_retry", method=method, tentativa=attempt + 1,
                        erro=last, aguardando_s=round(delay, 2))
            time.sleep(delay)

        raise RpcError(method, f"esgotou {self.max_retries} tentativas; ultimo: {last}")

    def call_batch(self, requests: list[tuple[str, list]]) -> list:
        """Batch JSON-RPC. Sem isto, buscar timestamp de N blocos custa N
        round-trips — inviavel numa chain de 0,1s por bloco.

        Devolve resultados na ORDEM PEDIDA (o servidor pode responder fora de
        ordem; reordenamos por id). Elemento None = aquele item falhou.
        """
        if not requests:
            return []
        body = json.dumps([
            {"jsonrpc": "2.0", "id": i, "method": m, "params": p or []}
            for i, (m, p) in enumerate(requests)
        ]).encode()
        req = urllib.request.Request(
            self.url, data=body,
            headers={"Content-Type": "application/json", "User-Agent": "uso-lp/0.1"})

        last = None
        for attempt in range(self.max_retries):
            with self._lock:
                self._throttle(items=len(requests))
                try:
                    with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                        payload = json.loads(resp.read().decode())
                    self.calls += 1
                    self._last_call = time.time()
                except urllib.error.HTTPError as exc:
                    self._last_call = time.time()
                    last = f"HTTP {exc.code}"
                    if exc.code not in (429, 502, 503, 504):
                        raise RpcError("batch", last) from exc
                    payload = None
                except Exception as exc:  # noqa: BLE001
                    self._last_call = time.time()
                    last = f"{type(exc).__name__}: {exc}"
                    payload = None

            if payload is not None:
                if isinstance(payload, dict):          # servidor recusou o batch
                    raise RpcError("batch", payload.get("error", payload))
                out: list = [None] * len(requests)
                for item in payload:
                    idx = item.get("id")
                    if isinstance(idx, int) and 0 <= idx < len(out):
                        out[idx] = None if "error" in item else item.get("result")
                return out

            self.retries += 1
            delay = min(30.0, (2 ** attempt) * 0.5) + random.uniform(0, 0.4)
            log.warning("rpc_batch_retry", tentativa=attempt + 1, erro=last,
                        itens=len(requests), aguardando_s=round(delay, 2))
            time.sleep(delay)

        raise RpcError("batch", f"esgotou {self.max_retries} tentativas; ultimo: {last}")

    def block_timestamps(self, numbers: list[int], batch_size: int | None = None) -> dict[int, dict]:
        """{numero: {ts, hash, parent_hash}} para os blocos pedidos."""
        out: dict[int, dict] = {}
        batch_size = batch_size or settings().batch_size
        uniq = sorted(set(numbers))
        for i in range(0, len(uniq), batch_size):
            slice_ = uniq[i:i + batch_size]
            results = self.call_batch(
                [("eth_getBlockByNumber", [hex(n), False]) for n in slice_])
            for n, res in zip(slice_, results):
                if res:
                    out[n] = {"ts": int(res["timestamp"], 16),
                              "hash": res["hash"],
                              "parent_hash": res["parentHash"]}
        return out

    # ---- atalhos usados pelo indexer ---------------------------------

    def block_number(self) -> int:
        return int(self.call("eth_blockNumber"), 16)

    def get_block(self, number: int, full: bool = False) -> dict:
        return self.call("eth_getBlockByNumber", [hex(number), full])

    def get_logs(self, address: str, from_block: int, to_block: int,
                 topics: list | None = None) -> list:
        params = {"address": address, "fromBlock": hex(from_block),
                  "toBlock": hex(to_block)}
        if topics:
            params["topics"] = topics
        return self.call("eth_getLogs", [params]) or []

    def eth_call(self, to: str, data: str, block: str | int = "latest"):
        blk = block if isinstance(block, str) else hex(block)
        return self.call("eth_call", [{"to": to, "data": data}, blk])


__all__ = ["RpcClient", "RpcError", "install_dns_bypass"]
