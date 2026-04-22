from __future__ import annotations

import sys
from pathlib import Path

import pytest
from sqlalchemy import text

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from db import SessionLocal, engine  # noqa: E402
from models import Base  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _verify_tables_exist():
    with engine.connect() as conn:
        existing = {row[0] for row in conn.execute(
            text("select tablename from pg_tables where schemaname = 'public'")
        )}
    missing = {t.name for t in Base.metadata.sorted_tables} - existing
    if missing:
        raise RuntimeError(
            f"missing tables {missing}; run `make db.migrate` first"
        )


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()
