"""Database engine and sessions.

SQLite locally so a clone runs with zero setup; Postgres on Render via
DATABASE_URL. There is no `dj-database-url` here -- SQLAlchemy parses the URL
directly, which is the whole reason that dependency does not appear in
requirements.txt.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import event
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.config import settings

log = logging.getLogger(__name__)

# Importing the models registers them on SQLModel.metadata. Alembic's autogenerate
# and create_all() both depend on this import having happened.
from app import models  # noqa: E402,F401  (side-effect import: metadata registration)


def normalize_database_url(url: str) -> str:
    """Fix the two URL shapes that break SQLAlchemy 2 on managed Postgres.

    Render (and Heroku before it) hands out `postgres://`, a scheme SQLAlchemy 2
    removed. `postgresql://` selects psycopg2, which is what requirements.txt
    installs.
    """
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]
    return url


def _engine_kwargs(url: str) -> dict:
    parsed = make_url(url)
    if parsed.drivername.startswith("sqlite"):
        kwargs: dict = {
            # NiceGUI, the MCP transport and the batch closer all touch the DB
            # from different threads in the same process.
            "connect_args": {"check_same_thread": False},
        }
        if parsed.database in (None, "", ":memory:"):
            # In-memory DBs need one shared connection or every session sees an
            # empty schema. Used by the tests.
            kwargs["poolclass"] = StaticPool
        return kwargs
    return {
        "pool_size": settings.db_pool_size,
        "max_overflow": settings.db_max_overflow,
        # Managed Postgres drops idle connections; without this the first
        # request after a quiet period fails.
        "pool_pre_ping": True,
        "pool_recycle": 1800,
    }


DATABASE_URL = normalize_database_url(settings.database_url)

engine: Engine = create_engine(
    DATABASE_URL,
    echo=settings.db_echo,
    **_engine_kwargs(DATABASE_URL),
)


@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_connection, _record) -> None:
    """SQLite defaults are wrong for a ledger: foreign keys are OFF and the
    rollback journal serialises readers against the batch closer."""
    if not DATABASE_URL.startswith("sqlite"):
        return
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.close()


def get_session() -> Iterator[Session]:
    """FastAPI dependency: `db: Session = Depends(get_session)`."""
    with Session(engine) as session:
        yield session


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope for background work (batch close, metering writes).

    Commits on success, rolls back on any exception. Ledger writes must never
    half-apply.
    """
    session = Session(engine)
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def create_all() -> None:
    """Create tables directly from the models.

    Alembic owns the schema in every real deployment (`alembic upgrade head` in
    build.sh). This exists for tests and for the local first run, and is a no-op
    when the tables already exist.
    """
    SQLModel.metadata.create_all(engine)


def describe() -> str:
    """Redacted engine description, safe to log and to show in the dashboard."""
    url = make_url(DATABASE_URL)
    return str(url.render_as_string(hide_password=True))
