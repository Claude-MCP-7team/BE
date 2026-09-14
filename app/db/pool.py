"""asyncpg 연결 풀 (BE-M1-2).

**DB 는 판정 경로에 없다** (ADR-001). 여기를 쓰는 곳은 세션 저장·조회와 배치뿐이고,
`app/engine`·`app/solver`·`app/planner` 는 이 모듈을 import 하지 않는다 (린트로 강제).
그래서 풀이 죽어도 판정은 계속 된다 — 저장만 안 될 뿐이다. 그 성질을 코드로
지키는 것이 이 모듈의 요점이다.

**무료 티어를 전제로 한 설정**
  Neon Free 는 scale-to-zero 라서 첫 연결에 콜드스타트가 붙고, 동시 연결 수도
  넉넉하지 않다. 그래서 풀은 작게 잡고(min 0), 타임아웃은 짧게 둔다.
  min_size=0 이 중요하다 — 유휴 연결을 붙들고 있으면 compute 가 잠들지 못해
  월 100 CU-h 예산을 앉아서 태운다.

**풀이 없어도 부팅한다.**
  DATABASE_URL 이 없거나 DB 가 죽어 있어도 API 는 뜬다. 스냅샷만 있으면 판정은
  되기 때문이다. 저장이 필요한 엔드포인트만 503 을 낸다.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import asyncpg

log = logging.getLogger("ypc.db")

# 무료 티어 기준값.
#   min 0  : 유휴 연결을 붙들지 않는다 → scale-to-zero 가 동작한다
#   max 5  : 단일 컨테이너가 쓸 만큼만. 늘려도 Neon 쪽 상한에 먼저 막힌다
DEFAULT_MIN_SIZE = 0
DEFAULT_MAX_SIZE = 5

# 연결 획득 타임아웃. 세션 저장 때문에 요청이 오래 매달리면 안 된다 —
# 판정은 이미 끝났고 저장만 남은 상황이라, 기다리느니 저장을 포기하는 게 낫다.
DEFAULT_ACQUIRE_TIMEOUT = 5.0
DEFAULT_COMMAND_TIMEOUT = 10.0

# 콜드스타트 중인 Neon 은 첫 연결을 거절하기도 한다. 몇 번은 다시 시도한다.
CONNECT_RETRIES = 3
CONNECT_BACKOFF_SECONDS = 1.5


class DatabaseUnavailable(RuntimeError):
    """DB 를 쓸 수 없다. 판정은 계속되어야 하고, 저장만 실패해야 한다."""


class Database:
    """풀 한 개를 감싼다. 프로세스당 하나만 만든다."""

    __slots__ = ("_dsn", "_pool")

    def __init__(self, dsn: str | None) -> None:
        self._dsn = dsn
        self._pool: asyncpg.Pool | None = None

    @property
    def configured(self) -> bool:
        """DSN 이 주어졌는가. 설정 자체가 없으면 연결을 시도하지도 않는다."""
        return bool(self._dsn)

    @property
    def ready(self) -> bool:
        return self._pool is not None

    async def connect(self) -> bool:
        """풀을 연다. 실패해도 예외를 올리지 않고 False 를 돌려준다.

        부팅 시 DB 가 없다고 프로세스를 죽이면, 스냅샷만으로 할 수 있는 판정까지
        멈춘다. 그래서 여기서는 실패를 기록만 하고 넘어간다.
        """
        if not self._dsn:
            log.warning("DATABASE_URL 이 없습니다 — 세션 저장 없이 기동합니다")
            return False
        if self._pool is not None:
            return True

        import asyncio

        last: Exception | None = None
        for attempt in range(CONNECT_RETRIES):
            pool = None
            try:
                pool = await asyncpg.create_pool(
                    self._dsn,
                    min_size=int(os.environ.get("DB_POOL_MIN", DEFAULT_MIN_SIZE)),
                    max_size=int(os.environ.get("DB_POOL_MAX", DEFAULT_MAX_SIZE)),
                    timeout=DEFAULT_ACQUIRE_TIMEOUT,
                    command_timeout=DEFAULT_COMMAND_TIMEOUT,
                    # 서버가 준비한 구문 캐시를 신뢰할 수 없는 환경(PgBouncer 등)을
                    # 대비해 캐시를 끈다. Neon 의 풀러 뒤에서 흔히 깨지는 지점이다.
                    statement_cache_size=0,
                )
                # min_size=0 이면 create_pool 은 연결을 한 번도 맺지 않는다.
                # 여기서 확인하지 않으면 DB 가 꺼져 있어도 "연결 완료" 로그가 찍히고,
                # 실패는 한참 뒤 첫 저장 요청에서야 드러난다.
                async with pool.acquire() as conn:
                    await conn.fetchval("SELECT 1")
                self._pool = pool
                log.info("DB 풀 연결 완료")
                return True
            except (OSError, asyncpg.PostgresError, TimeoutError) as e:
                last = e
                if pool is not None:
                    await pool.close()
                if attempt < CONNECT_RETRIES - 1:
                    await asyncio.sleep(CONNECT_BACKOFF_SECONDS * (2**attempt))

        log.warning("DB 풀 연결 실패 — 세션 저장 없이 기동합니다: %s", last)
        return False

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[asyncpg.Connection]:
        """연결 하나를 빌린다. 풀이 없으면 DatabaseUnavailable."""
        if self._pool is None:
            raise DatabaseUnavailable("DB 풀이 준비되지 않았습니다")
        async with self._pool.acquire() as conn:
            yield conn

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[asyncpg.Connection]:
        """트랜잭션 안에서 연결 하나를 빌린다."""
        async with self.acquire() as conn, conn.transaction():
            yield conn

    async def healthy(self) -> bool:
        """실제로 질의가 되는가. 풀 객체의 존재만으로는 알 수 없다."""
        if self._pool is None:
            return False
        try:
            async with self.acquire() as conn:
                return await conn.fetchval("SELECT 1") == 1
        except (OSError, asyncpg.PostgresError, DatabaseUnavailable):
            return False

    @property
    def info(self) -> dict[str, Any]:
        return {
            "configured": self.configured,
            "ready": self.ready,
            "size": self._pool.get_size() if self._pool else 0,
            "idle": self._pool.get_idle_size() if self._pool else 0,
        }


def dsn_from_env() -> str | None:
    return os.environ.get("DATABASE_URL") or None


# 프로세스 전역 풀.
#
# ⚠️ 스냅샷 보관소와 같은 규칙: `from app.db.pool import db` 대신
#    `from app.db import pool as db_pool` 후 `db_pool.db` 로 쓴다.
#    전자는 모듈마다 별도 바인딩을 만들어서, 테스트가 풀을 갈아끼워도
#    일부 모듈만 새것을 보는 상태가 된다.
db = Database(dsn_from_env())
