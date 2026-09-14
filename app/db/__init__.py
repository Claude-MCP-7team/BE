"""DB 계층 — asyncpg 풀과 리포지토리.

⚠️ `app/engine`·`app/solver`·`app/planner` 는 이 패키지를 import 하지 않는다.
   판정 핫 경로는 스냅샷만 읽는다 (ADR-001). 린트 규칙이 이를 강제한다.
"""

from app.db.pool import Database, DatabaseUnavailable, db, dsn_from_env
from app.db.sessions import (
    SessionNotFound,
    SessionRepository,
    StoredSession,
    try_save,
)

__all__ = [
    "Database",
    "DatabaseUnavailable",
    "SessionNotFound",
    "SessionRepository",
    "StoredSession",
    "db",
    "dsn_from_env",
    "try_save",
]
