"""Configuração única do projeto: toda config passa por aqui (variáveis de ambiente ou `.env`).

Nada de endereço fixo no código de domínio: pool, tokens e NonfungiblePositionManager entram por aqui.
Os defaults apontam para contratos PÚBLICOS da Robinhood Chain, verificados on-chain.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---- chain -------------------------------------------------------
    rpc_url: str = "https://rpc.mainnet.chain.robinhood.com"
    ws_url: str = ""
    chain_id: int = 4663
    blockscout_url: str = "https://robinhoodchain.blockscout.com"

    # Resolve DNS por DoH dentro do processo (alguns resolvedores filtram robinhood.com).
    dns_bypass: bool = True

    # ---- pool analisado (exemplo: USO/USDG 0,30%, contrato público) --
    pool_address: str = "0x02175608f1b5e6b5ed221ccfdc7be197d111d915"
    npm_address: str = "0x73991a25C818Bf1f1128dEAaB1492D45638DE0D3"
    # Carteira cujas posições são acompanhadas. Vazio = nenhuma (só dados de mercado).
    wallet: str = ""
    pool_creation_block: int = 12_021_761

    # ---- indexer -----------------------------------------------------
    # 500k blocos por janela comprovado; usamos metade por margem (a densidade de logs varia).
    chunk_size: int = 250_000
    # Orbit tem finalidade "soft" até o batch ir para a chain pai: 300 blocos a 0,1 s = 30 s.
    confirmations: int = 300
    request_timeout: int = 60
    max_retries: int = 6

    # Medido contra o RPC público: o limitador se comporta como token bucket de balde pequeno —
    # batch=100 falha mesmo espaçado, batch=25 a 0,6 s passa sob carga sustentada.
    max_items_per_second: float = 35.0
    min_request_interval_s: float = 0.12
    batch_size: int = 25

    # ---- banco -------------------------------------------------------
    # SQLite por padrão; Postgres em produção é só trocar a URL (pip install psycopg).
    database_url: str = f"sqlite:///{(ROOT / 'defime.db').as_posix()}"

    # ---- notícias (opcional) -----------------------------------------
    # Chave GRATUITA da alphavantage.co. Vazia = a escada de notícias usa as fontes sem chave.
    alphavantage_api_key: str = ""

    # ---- observabilidade ---------------------------------------------
    log_level: str = "INFO"
    log_json: bool = True

    @field_validator("pool_address", "npm_address", "wallet")
    @classmethod
    def _lower(cls, v: str) -> str:
        return v.lower()

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")


@lru_cache(maxsize=1)
def settings() -> Settings:
    return Settings()


__all__ = ["Settings", "settings", "ROOT"]
