"""세션 리포지토리 — 익명 프로필 저장·조회 (BE-M1-2).

`user_session` 에는 프로필 평문 컬럼이 없다. 여기서 암호화해 넣고 여기서 풀어서
꺼낸다. 저장 직전에 암호화하는 게 아니라, **이 계층 밖으로 평문이 나가는 경로가
아예 없게** 만드는 것이 요점이다.

**통계 컬럼은 평문이지만 개인을 특정하지 못하는 것만 둔다.**
  생년(연도만) · 지역 시도 2자리 · 취업상태. 생년월일이 아니라 생년이고,
  법정동 코드 5자리가 아니라 2자리다. 이 셋으로는 사람을 특정할 수 없지만
  "20대 경기도 구직자가 몇 명 왔나"는 셀 수 있다. 그 질문에 답하려고 프로필을
  복호화해야 한다면 아무도 통계를 안 내거나, 다들 복호화 습관을 들이게 된다.

**저장 실패가 판정 실패가 되면 안 된다.**
  판정은 DB 를 쓰지 않는다(ADR-001). 사용자는 이미 답을 받았고 저장은 편의 기능이다.
  그래서 저장 함수는 예외를 삼키지 않되, 호출자가 "실패해도 계속"을 선택할 수
  있도록 `try_save` 를 따로 둔다.

**만료는 DB 가 아니라 조회가 강제한다.**
  `expires_at` 이 지난 행은 삭제 잡이 돌기 전에도 조회되지 않아야 한다.
  잡이 하루 밀리면 90일 보관 약속이 하루씩 깨지는데, 그건 약속이 아니다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

import asyncpg
import msgspec

from app.core.crypto import DecryptionFailed, ProfileCipher, SealedBox, session_aad
from app.db.pool import Database, DatabaseUnavailable
from app.schemas.user import UserProfile

log = logging.getLogger("ypc.db.sessions")


class SessionNotFound(LookupError):
    """없는 세션이거나 만료됐다. 둘을 구분하지 않는다 — 구분하면 세션 ID 를
    대입해 '존재는 한다'를 알아낼 수 있다."""


@dataclass(slots=True)
class StoredSession:
    session_id: UUID
    profile: UserProfile | None
    created_at: datetime
    last_seen_at: datetime
    expires_at: datetime
    key_version: int


class SessionRepository:
    """user_session 테이블에 대한 유일한 출입구."""

    __slots__ = ("_cipher", "_db")

    def __init__(self, db: Database, cipher: ProfileCipher) -> None:
        self._db = db
        self._cipher = cipher

    # --- 쓰기 -----------------------------------------------------------

    async def create(self, profile: UserProfile | None = None) -> UUID:
        """새 익명 세션. 프로필이 있으면 함께 암호화해 넣는다."""
        async with self._db.acquire() as conn:
            session_id: UUID = await conn.fetchval(
                "INSERT INTO user_session (terms_version, privacy_agreed_at) "
                "VALUES ($1, $2) RETURNING session_id",
                profile.consent.terms_version if profile else None,
                _parse_ts(profile.consent.privacy_agreed_at) if profile else None,
            )
        if profile is not None:
            await self.save_profile(session_id, profile)
        return session_id

    async def save_profile(self, session_id: UUID, profile: UserProfile) -> None:
        """프로필을 암호화해 덮어쓴다. 평문은 이 함수 밖으로 나가지 않는다."""
        box = self._cipher.seal(
            msgspec.json.encode(profile), aad=session_aad(str(session_id), "profile")
        )
        stats = _stats_of(profile)

        async with self._db.acquire() as conn:
            updated = await conn.execute(
                """
                UPDATE user_session SET
                    profile_ct        = $2,
                    profile_nonce     = $3,
                    enc_key_version   = $4,
                    birth_year        = $5,
                    region_prefix     = $6,
                    employment_status = $7,
                    last_seen_at      = now()
                WHERE session_id = $1 AND expires_at > now()
                """,
                session_id,
                box.ciphertext,
                box.nonce,
                box.key_version,
                *stats,
            )
        if updated == "UPDATE 0":
            raise SessionNotFound(f"세션이 없거나 만료됐습니다: {session_id}")

    async def touch(self, session_id: UUID) -> None:
        """마지막 접근 시각만 갱신한다. 만료 연장은 하지 않는다 —
        90일은 '마지막 사용 후 90일'이 아니라 '생성 후 90일'이다."""
        async with self._db.acquire() as conn:
            await conn.execute(
                "UPDATE user_session SET last_seen_at = now() "
                "WHERE session_id = $1 AND expires_at > now()",
                session_id,
            )

    async def delete(self, session_id: UUID) -> bool:
        """사용자 요청에 의한 즉시 삭제 (개인정보 파기 요구)."""
        async with self._db.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM user_session WHERE session_id = $1", session_id
            )
        return bool(result != "DELETE 0")

    # --- 읽기 -----------------------------------------------------------

    async def get(self, session_id: UUID) -> StoredSession:
        """세션을 꺼낸다. 만료된 행은 삭제 잡을 기다리지 않고 없는 것으로 취급한다."""
        async with self._db.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT session_id, profile_ct, profile_nonce, enc_key_version,
                       created_at, last_seen_at, expires_at
                  FROM user_session
                 WHERE session_id = $1 AND expires_at > now()
                """,
                session_id,
            )
        if row is None:
            raise SessionNotFound(f"세션이 없거나 만료됐습니다: {session_id}")

        return StoredSession(
            session_id=row["session_id"],
            profile=self._open_profile(row),
            created_at=row["created_at"],
            last_seen_at=row["last_seen_at"],
            expires_at=row["expires_at"],
            key_version=row["enc_key_version"],
        )

    def _open_profile(self, row: asyncpg.Record) -> UserProfile | None:
        if row["profile_ct"] is None:
            return None
        box = SealedBox(
            ciphertext=bytes(row["profile_ct"]),
            nonce=bytes(row["profile_nonce"]),
            key_version=row["enc_key_version"],
        )
        raw = self._cipher.open(box, aad=session_aad(str(row["session_id"]), "profile"))
        return msgspec.json.decode(raw, type=UserProfile)

    # --- 유지보수 -------------------------------------------------------

    async def purge_expired(self, limit: int = 1000) -> int:
        """만료된 세션을 실제로 지운다 (야간 배치).

        한 번에 다 지우지 않고 나눠 지운다 — 90일치가 한꺼번에 만료되는 날
        긴 트랜잭션이 무료 티어의 연결을 다 잡아먹는 상황을 피한다.
        """
        async with self._db.acquire() as conn:
            deleted: int = await conn.fetchval(
                """
                WITH victims AS (
                    SELECT session_id FROM user_session
                     WHERE expires_at <= now() LIMIT $1
                )
                DELETE FROM user_session u USING victims v
                 WHERE u.session_id = v.session_id
                RETURNING 1
                """,
                limit,
            ) or 0
        return deleted

    async def rotate_key(self, session_id: UUID) -> bool:
        """옛 키로 잠긴 프로필을 최신 키로 다시 잠근다.

        읽을 때 호출하면 키 교체가 서비스 중단 없이 점진적으로 끝난다.
        """
        try:
            stored = await self.get(session_id)
        except (SessionNotFound, DecryptionFailed):
            return False
        if stored.profile is None or stored.key_version == self._cipher.current_version:
            return False
        await self.save_profile(session_id, stored.profile)
        return True


# --- 호출자 편의 ---------------------------------------------------------


async def try_save(repo: SessionRepository, session_id: UUID, profile: UserProfile) -> bool:
    """저장을 시도하되 실패해도 요청을 깨지 않는다.

    판정은 이미 끝났고 사용자는 답을 받았다. 저장이 안 됐다고 500 을 돌려주면,
    맞는 판정을 보여주고도 실패한 것처럼 보인다.
    """
    try:
        await repo.save_profile(session_id, profile)
        return True
    except (DatabaseUnavailable, SessionNotFound, asyncpg.PostgresError, OSError) as e:
        log.warning("세션 저장 실패 (판정은 정상 응답): %s", e)
        return False


# --- 내부 ----------------------------------------------------------------


def _stats_of(profile: UserProfile) -> tuple[int | None, str | None, str | None]:
    """평문으로 둘 통계 컬럼. 개인을 특정하지 못하는 수준까지만 깎는다.

    생년월일 → 생년, 법정동 5자리 → 시도 2자리.
    """
    core = profile.core
    return (
        core.birth_date.year,
        core.region_code[:2] if core.region_code else None,
        core.employment_status,
    )


def _parse_ts(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        # 동의 시각이 이상하다고 세션 생성을 막지는 않는다. 값만 버린다.
        return None
