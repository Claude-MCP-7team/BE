"""설정이 틀려도 서버는 뜬다.

ADR-001 에 따라 판정 경로는 DB 를 쓰지 않는다. 그래서 DB 설정이 잘못됐을 때
막혀야 하는 것은 **세션 저장뿐**이고, 판정까지 같이 멈추면 안 된다.

실제로 그렇게 멈춘 적이 있다. `DATABASE_URL` 에 스킴 없는 값이 들어가자 asyncpg 가
`ClientConfigurationError` 를 던졌는데, 그건 `InterfaceError` → `ValueError` 계열이라
`except (OSError, PostgresError, TimeoutError)` 어디에도 안 걸렸다. 예외가 lifespan
으로 올라가 **프로세스가 종료되고 재시작 루프**에 빠졌다 — 환경변수 오타 하나로
서비스 전체가 내려간 것이다.
"""

from __future__ import annotations

import asyncio

import pytest

from app.db.pool import Database

# 운영자가 실제로 저지르는 실수들. 전부 '연결 실패'로 끝나야 하고,
# 어느 하나도 예외를 밖으로 던지면 안 된다.
BAD_DSNS = {
    "빈 문자열": "",
    "공백만": "   ",
    "스킴 없음": "ypc_db_user:pw@host/db",
    "psql 명령을 통째로 붙여넣음": 'psql "postgresql://u:p@h/d"',
    "앞뒤 공백이 섞임": "  postgresql://u:p@nowhere.invalid/d  ",
    "스킴 오타": "postgres-sql://u:p@h/d",
    "URL 이 아닌 문자열": "여기에 DB 주소를 넣으세요",
}


@pytest.mark.parametrize("label", sorted(BAD_DSNS))
def test_잘못된_DSN_이_기동을_막지_않는다(label: str) -> None:
    """connect() 는 실패를 False 로 돌려줄 뿐, 던지지 않는다."""

    async def run() -> bool:
        db = Database(BAD_DSNS[label])
        return await asyncio.wait_for(db.connect(), timeout=30)

    assert asyncio.run(run()) is False, f"{label}: 연결이 성공했다고 보고했다"


def test_연결_실패는_경고로_남는다(caplog: pytest.LogCaptureFixture) -> None:
    """조용히 삼키면 '왜 세션만 안 되지'를 아무도 추적 못 한다.

    예외 종류까지 남긴다 — DSN 오타(ClientConfigurationError)와 DB 다운
    (OSError)은 운영자가 볼 곳이 다르다.
    """
    import logging

    async def run() -> None:
        db = Database("ypc_db_user:pw@host/db")
        await asyncio.wait_for(db.connect(), timeout=30)

    with caplog.at_level(logging.WARNING):
        asyncio.run(run())

    messages = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("세션 저장 없이 기동" in m for m in messages), messages
    assert any("ClientConfigurationError" in m for m in messages), (
        f"예외 종류가 로그에 없다 — 원인 추적이 어려워진다: {messages}"
    )


def test_연결_안_된_풀은_쓰려고_하면_막는다() -> None:
    """기동은 계속하되, 저장은 명확히 거부해야 한다.

    여기서 조용히 통과시키면 저장된 줄 알았는데 안 된 프로필이 생긴다.
    """
    from app.db.pool import DatabaseUnavailable

    async def run() -> None:
        db = Database("")
        await db.connect()
        async with db.acquire():
            pass

    with pytest.raises(DatabaseUnavailable):
        asyncio.run(run())


def test_readyz_가_설정_여부와_연결_여부를_구분한다() -> None:
    """configured=true, ready=false 면 '값은 있는데 못 붙는다'는 뜻이다.

    둘을 한 칸으로 합치면 운영자가 오타인지 DB 다운인지 알 수 없다.
    """

    async def run() -> dict[str, object]:
        db = Database("postgresql://u:p@nowhere.invalid/d")
        await asyncio.wait_for(db.connect(), timeout=30)
        return db.info

    info = asyncio.run(run())
    assert info["configured"] is True
    assert info["ready"] is False

    assert Database("").info["configured"] is False
