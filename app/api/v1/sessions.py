"""세션 API — 익명 프로필 저장·조회 (BE-M1-2, FE 의 `GET/PUT /api/profile` 대응).

**세션 ID 가 곧 인증이다.** 로그인이 없으므로 세션 ID 를 아는 사람이 그 프로필의
주인이다. 그래서 ID 는 추측 불가능해야 하고(UUIDv4, DB 가 생성), 응답에 단 한 번만
나타나며, 로그에 남기지 않는다.

**없는 세션과 만료된 세션을 구분하지 않는다.** 둘을 나누면 ID 를 대입해가며
"이건 존재는 했다"를 알아낼 수 있다. 둘 다 404 다.

**저장이 안 돼도 판정은 된다.** DB 가 죽어 있으면 이 엔드포인트만 503 을 내고,
`/v1/judge` 는 계속 200 을 낸다 (ADR-001). 그게 스냅샷 인메모리 구조의 이유다.
"""

from __future__ import annotations

import logging
from typing import Annotated
from uuid import UUID

import asyncpg
import msgspec
from fastapi import APIRouter, Path, Request, Response

from app.api.decode import decode_profile
from app.api.schema import profile_body
from app.core.crypto import DecryptionFailed
from app.core.problem import (
    SESSION_NOT_FOUND,
    SESSION_NOT_READABLE,
    SESSION_STORE_UNAVAILABLE,
    Problem,
)
from app.db import DatabaseUnavailable, SessionNotFound
from app.db import pool as db_pool
from app.db.sessions import SessionRepository
from app.schemas.user import UserProfile

log = logging.getLogger("ypc.api.sessions")

router = APIRouter(prefix="/v1", tags=["sessions"])

SessionId = Annotated[UUID, Path(description="세션 ID (UUIDv4)")]


def _repo() -> SessionRepository:
    """리포지토리를 만든다. DB 나 암호화 키가 없으면 503.

    키가 없는 상태로 저장을 허용하면 평문이 들어갈 길이 생긴다. 그럴 바에는
    저장 기능을 통째로 끄는 것이 맞다.
    """
    from app.core.config import settings

    if not db_pool.db.ready:
        raise Problem(
            SESSION_STORE_UNAVAILABLE,
            "세션 저장소를 사용할 수 없습니다 (판정 기능은 정상 동작합니다)",
        )
    cipher = settings.profile_cipher()
    if cipher is None:
        raise Problem(
            SESSION_STORE_UNAVAILABLE,
            "프로필 암호화 키가 설정되지 않아 세션을 저장하지 않습니다"
            " (판정 기능은 정상 동작합니다)",
        )
    return SessionRepository(db_pool.db, cipher)


def _decode_profile(body: bytes) -> UserProfile:
    return decode_profile(body)


@router.post(
    "/sessions",
    status_code=201,
    # 본문 없이 부르면 프로필 없는 익명 세션이 발급된다 — 그래서 required=False.
    openapi_extra=profile_body(
        required=False,
        description="저장할 프로필. 생략하면 빈 세션만 발급한다",
    ),
)
async def create_session(request: Request) -> Response:
    """익명 세션 발급. 본문에 프로필을 담으면 함께 저장한다.

    반환된 `session_id` 는 다시 조회할 유일한 수단이다. FE 가 보관해야 하며
    서버는 그것으로부터 사용자를 역추적할 방법을 갖지 않는다.
    """
    repo = _repo()
    body = await request.body()
    profile = _decode_profile(body) if body.strip() else None

    try:
        session_id = await repo.create(profile)
    except (DatabaseUnavailable, asyncpg.PostgresError, OSError) as e:
        log.warning("세션 생성 실패: %s", e)
        raise Problem(SESSION_STORE_UNAVAILABLE, "세션을 생성하지 못했습니다") from e

    return Response(
        content=msgspec.json.encode({"session_id": str(session_id)}),
        media_type="application/json",
        status_code=201,
        headers={"Cache-Control": "private, no-store"},
    )


@router.get("/sessions/{session_id}")
async def get_session(session_id: SessionId) -> Response:
    """저장된 프로필을 돌려준다 (FE 의 `GET /api/profile`)."""
    repo = _repo()
    try:
        stored = await repo.get(session_id)
    except SessionNotFound as e:
        raise Problem(SESSION_NOT_FOUND) from e
    except DecryptionFailed as e:
        # 복호화 실패는 데이터 문제이지 사용자 잘못이 아니다. 평문을 지어내느니
        # 실패를 알린다 — 틀린 프로필로 판정하는 것이 판정 못 하는 것보다 나쁘다.
        log.error("프로필 복호화 실패: session=%s", session_id)
        raise Problem(SESSION_NOT_READABLE) from e
    except (DatabaseUnavailable, asyncpg.PostgresError, OSError) as e:
        raise Problem(SESSION_STORE_UNAVAILABLE) from e

    payload = {
        "session_id": str(stored.session_id),
        "profile": stored.profile,
        "created_at": stored.created_at.isoformat(),
        "last_seen_at": stored.last_seen_at.isoformat(),
        "expires_at": stored.expires_at.isoformat(),
    }
    return Response(
        content=msgspec.json.encode(payload),
        media_type="application/json",
        headers={"Cache-Control": "private, no-store"},
    )


@router.put("/sessions/{session_id}", openapi_extra=profile_body())
async def put_session(session_id: SessionId, request: Request) -> Response:
    """프로필을 통째로 덮어쓴다 (FE 의 `PUT /api/profile`).

    부분 수정(PATCH)을 두지 않는 이유: 프로필은 필드 10여 개짜리 한 덩어리이고,
    부분 수정을 허용하면 "소득만 지우기"가 `null` 전송인지 필드 생략인지
    모호해진다. 전체 전송이면 그 구분이 필요 없다.
    """
    repo = _repo()
    profile = _decode_profile(await request.body())

    try:
        await repo.save_profile(session_id, profile)
    except SessionNotFound as e:
        raise Problem(SESSION_NOT_FOUND) from e
    except (DatabaseUnavailable, asyncpg.PostgresError, OSError) as e:
        raise Problem(SESSION_STORE_UNAVAILABLE) from e

    return Response(status_code=204, headers={"Cache-Control": "private, no-store"})


@router.delete("/sessions/{session_id}", status_code=204)
async def delete_session(session_id: SessionId) -> Response:
    """즉시 파기 (개인정보 삭제 요구). 90일 만료를 기다리지 않는다."""
    repo = _repo()
    try:
        deleted = await repo.delete(session_id)
    except (DatabaseUnavailable, asyncpg.PostgresError, OSError) as e:
        raise Problem(SESSION_STORE_UNAVAILABLE) from e

    if not deleted:
        raise Problem(SESSION_NOT_FOUND, "세션이 없거나 이미 삭제되었습니다")
    return Response(status_code=204)
