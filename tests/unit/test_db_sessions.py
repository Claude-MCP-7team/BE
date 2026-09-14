"""세션 저장소 통합 테스트 (BE-M1-2).

**실제 PostgreSQL 이 필요하다.** `DATABASE_URL` 이 없으면 통째로 건너뛴다.
CI 는 postgres:16 서비스 컨테이너를 띄우므로 매 PR 에서 실행된다.

가짜 DB 로 대체하지 않는 이유: 여기서 검증하려는 것 대부분이 DB 의 행동이다 —
UUID 생성, `expires_at` 기본값, CHECK 제약, BYTEA 왕복, ON DELETE CASCADE.
가짜로 바꾸면 '가짜가 맞게 동작하는지'만 확인하게 된다.
"""

from __future__ import annotations

import os
from datetime import date
from uuid import UUID, uuid4

import pytest
import pytest_asyncio

from app.core.crypto import ProfileCipher, generate_key, load_keys_from_env
from app.db.pool import Database, DatabaseUnavailable
from app.db.sessions import SessionNotFound, SessionRepository, try_save
from app.schemas.user import Consent, Core, History, UserProfile

DATABASE_URL = os.environ.get("DATABASE_URL")

pytestmark = [
    pytest.mark.skipif(not DATABASE_URL, reason="DATABASE_URL 이 없습니다 (실제 PostgreSQL 필요)"),
    pytest.mark.asyncio,
]


def _profile(**core_kwargs: object) -> UserProfile:
    base: dict[str, object] = {
        "birth_date": date(2001, 3, 14),
        "region_code": "41465",
        "employment_status": "job_seeking",
        "household_income_ratio_median": 120,
    }
    base.update(core_kwargs)
    return UserProfile(
        core=Core(**base),  # type: ignore[arg-type]
        history=History(received_policy_ids=["MOLIT-RENT"]),
        consent=Consent(terms_version="1.0", privacy_agreed_at="2026-09-14T00:00:00+09:00"),
    )


@pytest_asyncio.fixture
async def db():
    database = Database(DATABASE_URL)
    assert await database.connect(), "DB 연결 실패"
    yield database
    await database.close()


@pytest.fixture
def cipher() -> ProfileCipher:
    return ProfileCipher(load_keys_from_env(f"1:{generate_key()}"))


@pytest_asyncio.fixture
async def repo(db, cipher) -> SessionRepository:
    return SessionRepository(db, cipher)


@pytest_asyncio.fixture
async def cleanup(db):
    """테스트가 만든 세션을 지운다."""
    created: list[UUID] = []
    yield created
    if created:
        async with db.acquire() as conn:
            await conn.execute(
                "DELETE FROM user_session WHERE session_id = ANY($1::uuid[])", created
            )


# --- 풀 -------------------------------------------------------------------


async def test_풀이_실제로_질의한다(db) -> None:
    assert await db.healthy()
    assert db.ready and db.configured


async def test_DSN_이_없으면_연결을_시도하지_않는다() -> None:
    """설정이 없는 것과 연결에 실패한 것은 다르다."""
    database = Database(None)
    assert not database.configured
    assert await database.connect() is False
    assert not database.ready


async def test_연결_실패해도_예외를_올리지_않는다() -> None:
    """부팅 시 DB 가 없다고 프로세스를 죽이면 판정까지 멈춘다 (ADR-001)."""
    database = Database("postgresql://nobody:nobody@127.0.0.1:1/nonexistent")
    assert await database.connect() is False
    assert not await database.healthy()


async def test_풀이_없으면_명확한_예외를_던진다() -> None:
    database = Database(None)
    with pytest.raises(DatabaseUnavailable):
        async with database.acquire():
            pass


# --- 생성 · 조회 ----------------------------------------------------------


async def test_빈_세션을_만들_수_있다(repo, cleanup) -> None:
    session_id = await repo.create()
    cleanup.append(session_id)

    stored = await repo.get(session_id)
    assert stored.session_id == session_id
    assert stored.profile is None  # 아직 프로필 없음
    assert stored.expires_at > stored.created_at


async def test_프로필과_함께_만들_수_있다(repo, cleanup) -> None:
    session_id = await repo.create(_profile())
    cleanup.append(session_id)

    stored = await repo.get(session_id)
    assert stored.profile is not None
    assert stored.profile.core.birth_date == date(2001, 3, 14)
    assert stored.profile.core.region_code == "41465"
    assert stored.profile.history.received_policy_ids == ["MOLIT-RENT"]


async def test_세션_ID_는_매번_다르다(repo, cleanup) -> None:
    """세션 ID 가 곧 인증이다 — 추측 가능하면 남의 프로필을 읽을 수 있다."""
    ids = []
    for _ in range(5):
        sid = await repo.create()
        cleanup.append(sid)
        ids.append(sid)
    assert len(set(ids)) == 5


async def test_없는_세션은_404_취급(repo) -> None:
    with pytest.raises(SessionNotFound):
        await repo.get(uuid4())


# --- 저장은 암호화된다 ----------------------------------------------------


async def test_DB_에_평문이_들어가지_않는다(repo, db, cleanup) -> None:
    """이 테스트가 이 계층의 존재 이유다."""
    session_id = await repo.create(_profile())
    cleanup.append(session_id)

    async with db.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT profile_ct, profile_nonce, enc_key_version, "
            "       birth_year, region_prefix, employment_status "
            "  FROM user_session WHERE session_id = $1",
            session_id,
        )

    blob = bytes(row["profile_ct"])
    assert b"41465" not in blob
    assert b"2001-03-14" not in blob
    assert b"MOLIT-RENT" not in blob
    assert len(bytes(row["profile_nonce"])) == 12
    assert row["enc_key_version"] == 1

    # 통계 컬럼은 평문이지만 개인을 특정하지 못하는 수준까지만
    assert row["birth_year"] == 2001  # 생년월일이 아니라 생년
    assert row["region_prefix"] == "41"  # 5자리가 아니라 시도 2자리
    assert row["employment_status"] == "job_seeking"


async def test_다른_키로는_읽히지_않는다(db, cipher, cleanup) -> None:
    repo = SessionRepository(db, cipher)
    session_id = await repo.create(_profile())
    cleanup.append(session_id)

    from app.core.crypto import DecryptionFailed

    stranger = SessionRepository(db, ProfileCipher(load_keys_from_env(f"1:{generate_key()}")))
    with pytest.raises(DecryptionFailed):
        await stranger.get(session_id)


async def test_암호문을_다른_세션_행에_옮기면_읽히지_않는다(repo, db, cleanup) -> None:
    """AAD 로 세션 ID 를 묶었기 때문에 행 바꿔치기가 막힌다."""
    victim = await repo.create(_profile())
    attacker = await repo.create()
    cleanup.extend([victim, attacker])

    async with db.acquire() as conn:
        await conn.execute(
            """
            UPDATE user_session SET
                profile_ct = v.profile_ct,
                profile_nonce = v.profile_nonce,
                enc_key_version = v.enc_key_version
            FROM (SELECT profile_ct, profile_nonce, enc_key_version
                    FROM user_session WHERE session_id = $1) AS v
            WHERE session_id = $2
            """,
            victim,
            attacker,
        )

    from app.core.crypto import DecryptionFailed

    with pytest.raises(DecryptionFailed):
        await repo.get(attacker)


# --- 수정 -----------------------------------------------------------------


async def test_프로필을_덮어쓸_수_있다(repo, cleanup) -> None:
    session_id = await repo.create(_profile())
    cleanup.append(session_id)

    await repo.save_profile(session_id, _profile(region_code="11680", household_size=3))
    stored = await repo.get(session_id)
    assert stored.profile is not None
    assert stored.profile.core.region_code == "11680"
    assert stored.profile.core.household_size == 3


async def test_덮어쓰면_통계_컬럼도_따라간다(repo, db, cleanup) -> None:
    session_id = await repo.create(_profile())
    cleanup.append(session_id)

    await repo.save_profile(session_id, _profile(region_code="11680", employment_status="employed"))
    async with db.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT region_prefix, employment_status FROM user_session WHERE session_id = $1",
            session_id,
        )
    assert row["region_prefix"] == "11"
    assert row["employment_status"] == "employed"


async def test_덮어쓸_때마다_nonce_가_바뀐다(repo, db, cleanup) -> None:
    """같은 키로 nonce 를 재사용하면 GCM 인증이 무너진다."""
    session_id = await repo.create(_profile())
    cleanup.append(session_id)

    nonces = set()
    for _ in range(5):
        await repo.save_profile(session_id, _profile())
        async with db.acquire() as conn:
            nonces.add(
                bytes(
                    await conn.fetchval(
                        "SELECT profile_nonce FROM user_session WHERE session_id = $1",
                        session_id,
                    )
                )
            )
    assert len(nonces) == 5


async def test_없는_세션에는_저장할_수_없다(repo) -> None:
    with pytest.raises(SessionNotFound):
        await repo.save_profile(uuid4(), _profile())


async def test_마지막_접근_시각이_갱신된다(repo, cleanup) -> None:
    session_id = await repo.create()
    cleanup.append(session_id)
    before = (await repo.get(session_id)).last_seen_at

    await repo.touch(session_id)
    assert (await repo.get(session_id)).last_seen_at >= before


async def test_접근해도_만료는_연장되지_않는다(repo, cleanup) -> None:
    """90일은 '마지막 사용 후 90일'이 아니라 '생성 후 90일'이다."""
    session_id = await repo.create()
    cleanup.append(session_id)
    before = (await repo.get(session_id)).expires_at

    await repo.touch(session_id)
    assert (await repo.get(session_id)).expires_at == before


# --- 만료 · 삭제 ----------------------------------------------------------


async def test_만료된_세션은_조회되지_않는다(repo, db, cleanup) -> None:
    """삭제 잡이 하루 밀려도 90일 약속은 지켜져야 한다."""
    session_id = await repo.create(_profile())
    cleanup.append(session_id)

    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE user_session SET expires_at = now() - INTERVAL '1 day' "
            "WHERE session_id = $1",
            session_id,
        )

    with pytest.raises(SessionNotFound):
        await repo.get(session_id)


async def test_만료된_세션에는_저장할_수_없다(repo, db, cleanup) -> None:
    session_id = await repo.create()
    cleanup.append(session_id)
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE user_session SET expires_at = now() - INTERVAL '1 day' "
            "WHERE session_id = $1",
            session_id,
        )
    with pytest.raises(SessionNotFound):
        await repo.save_profile(session_id, _profile())


async def test_즉시_삭제할_수_있다(repo, cleanup) -> None:
    """개인정보 파기 요구에 90일을 기다리라고 할 수 없다."""
    session_id = await repo.create(_profile())
    cleanup.append(session_id)

    assert await repo.delete(session_id) is True
    with pytest.raises(SessionNotFound):
        await repo.get(session_id)
    assert await repo.delete(session_id) is False  # 두 번째는 False


async def test_만료분을_실제로_지운다(repo, db, cleanup) -> None:
    session_id = await repo.create(_profile())
    cleanup.append(session_id)
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE user_session SET expires_at = now() - INTERVAL '1 day' "
            "WHERE session_id = $1",
            session_id,
        )

    assert await repo.purge_expired(limit=1000) >= 1
    async with db.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM user_session WHERE session_id = $1", session_id
            )
            == 0
        )


async def test_만료되지_않은_것은_지우지_않는다(repo, db, cleanup) -> None:
    session_id = await repo.create(_profile())
    cleanup.append(session_id)
    await repo.purge_expired(limit=1000)
    async with db.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM user_session WHERE session_id = $1", session_id
            )
            == 1
        )


# --- 키 로테이션 ----------------------------------------------------------


async def test_옛_키로_저장된_것을_새_키로_다시_잠근다(db, cleanup) -> None:
    """키 교체 때 서비스를 멈추지 않으려면 읽으면서 조금씩 옮겨야 한다."""
    key1, key2 = generate_key(), generate_key()

    old_repo = SessionRepository(db, ProfileCipher(load_keys_from_env(f"1:{key1}")))
    session_id = await old_repo.create(_profile())
    cleanup.append(session_id)

    rotated = SessionRepository(db, ProfileCipher(load_keys_from_env(f"1:{key1},2:{key2}")))
    assert (await rotated.get(session_id)).key_version == 1

    assert await rotated.rotate_key(session_id) is True
    stored = await rotated.get(session_id)
    assert stored.key_version == 2
    assert stored.profile is not None
    assert stored.profile.core.region_code == "41465"

    # 이제 새 키만으로 읽힌다
    new_only = SessionRepository(db, ProfileCipher(load_keys_from_env(f"2:{key2}")))
    assert (await new_only.get(session_id)).profile is not None


async def test_이미_최신_키면_재암호화하지_않는다(repo, cleanup) -> None:
    session_id = await repo.create(_profile())
    cleanup.append(session_id)
    assert await repo.rotate_key(session_id) is False


async def test_없는_세션은_로테이션도_조용히_넘어간다(repo) -> None:
    assert await repo.rotate_key(uuid4()) is False


# --- 저장 실패가 판정을 깨지 않는다 ---------------------------------------


async def test_저장_실패는_예외가_아니라_False(cipher) -> None:
    """판정은 이미 끝났다. 저장 실패로 500 을 내면 맞는 답을 주고도 실패로 보인다."""
    dead = SessionRepository(Database(None), cipher)
    assert await try_save(dead, uuid4(), _profile()) is False


async def test_정상_저장은_True(repo, cleanup) -> None:
    session_id = await repo.create()
    cleanup.append(session_id)
    assert await try_save(repo, session_id, _profile()) is True


async def test_없는_세션_저장도_False(repo) -> None:
    assert await try_save(repo, uuid4(), _profile()) is False
