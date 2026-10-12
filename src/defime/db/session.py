"""Engine e sessao. Agnostico de banco: SQLite local, Postgres em producao."""

from __future__ import annotations

from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from ..config import settings
from .models import Base

_engine = None
_Session = None


def get_engine():
    global _engine, _Session
    if _engine is not None:
        return _engine

    cfg = settings()
    kwargs: dict = {"future": True, "pool_pre_ping": True}
    if cfg.is_sqlite:
        # check_same_thread=False: o tail roda em thread separada do backfill
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
        kwargs.pop("pool_pre_ping")

    _engine = create_engine(cfg.database_url, **kwargs)

    if cfg.is_sqlite:
        @event.listens_for(_engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _):
            cur = dbapi_conn.cursor()
            # WAL: leitura concorrente enquanto o indexer escreve
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

    _Session = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _engine


def get_sessionmaker():
    get_engine()
    return _Session


@contextmanager
def session_scope() -> Session:
    factory = get_sessionmaker()
    sess = factory()
    try:
        yield sess
        sess.commit()
    except Exception:
        sess.rollback()
        raise
    finally:
        sess.close()


def create_all():
    """Cria o schema. Alembic assume daqui em diante para evolucao."""
    Base.metadata.create_all(get_engine())


def reset_engine():
    """So para testes: forca releitura da config."""
    global _engine, _Session
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _Session = None


__all__ = ["get_engine", "get_sessionmaker", "session_scope", "create_all",
           "reset_engine"]
