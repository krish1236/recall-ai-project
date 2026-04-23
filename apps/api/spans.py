from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from models import UtteranceSpan


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def mark(session: Session, utterance_id: uuid.UUID, **fields: Any) -> None:
    """Upsert an utterance-span row with the given timestamp fields.

    Existing timestamps are overwritten by later marks on the same key, but
    unset fields are not clobbered (ON CONFLICT DO UPDATE updates only the
    columns we actually pass in).
    """
    if not fields:
        return
    stmt = (
        pg_insert(UtteranceSpan)
        .values(utterance_id=utterance_id, **fields)
        .on_conflict_do_update(index_elements=["utterance_id"], set_=fields)
    )
    session.execute(stmt)


def mark_now(session: Session, utterance_id: uuid.UUID, field: str) -> None:
    mark(session, utterance_id, **{field: _now()})
