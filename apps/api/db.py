from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

# Walk up from cwd to find a .env (no-op if none exists, e.g. in containers
# where Railway injects env vars directly). Override=True so an empty shell
# value doesn't shadow the .env value.
_env = Path(__file__).resolve().parent
for _ in range(4):
    candidate = _env / ".env"
    if candidate.exists():
        load_dotenv(candidate, override=True)
        break
    _env = _env.parent

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql+psycopg://postgres:postgres@localhost:55432/recall",
)

if DATABASE_URL.startswith("postgresql://") and "+psycopg" not in DATABASE_URL:
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)

engine = create_engine(DATABASE_URL, future=True, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    pass
