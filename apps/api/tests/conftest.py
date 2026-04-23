from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from sqlalchemy import text

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

os.environ.setdefault("RECALL_WEBHOOK_SECRET", "test-webhook-secret-do-not-use-in-prod")

from db import SessionLocal, engine  # noqa: E402
from models import Base  # noqa: E402

_TABLES = [
    "insight_evidence",
    "insights",
    "action_items",
    "summaries",
    "transcript_utterances",
    "utterance_spans",
    "meeting_events",
    "webhook_deliveries",
    "dead_letter_jobs",
    "llm_cache",
    "meetings",
    "accounts",
]


@pytest.fixture(scope="session", autouse=True)
def _verify_tables_exist():
    with engine.connect() as conn:
        existing = {row[0] for row in conn.execute(
            text("select tablename from pg_tables where schemaname = 'public'")
        )}
    missing = {t.name for t in Base.metadata.sorted_tables} - existing
    if missing:
        raise RuntimeError(f"missing tables {missing}; run `make db.migrate` first")


@pytest.fixture(autouse=True)
def _truncate():
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {', '.join(_TABLES)} RESTART IDENTITY CASCADE"))
    _flush_redis_sync()
    yield


def _flush_redis_sync() -> None:
    import redis as _redis_sync
    url = os.environ.get("REDIS_URL", "redis://localhost:56379")
    r = _redis_sync.from_url(url)
    try:
        r.flushdb()
    finally:
        r.close()


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()
