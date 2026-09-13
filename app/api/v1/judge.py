"""POST /v1/judge — 조건 1회 입력 → 전 정책 일괄 판정 (US-01).

응답 설계 두 가지:
  1. 기본은 '요약 + 적격/확인필요 상세'만 내려간다.
     부적격이 전체의 70% 가까이 되는데, 사용자가 처음 보는 화면은 요약과
     적격 목록이다. 부적격 수백 건의 근거를 매번 실어 보내면 응답이 10배로
     커지고 대부분 쓰이지 않는다. `include=all` 로 전부 받을 수 있다.
  2. ETag 는 (스냅샷 버전 + 프로필 해시)다. 같은 조건으로 다시 물으면
     304 로 끝나므로, 새로고침이 서버 계산을 유발하지 않는다.
"""

from __future__ import annotations

import hashlib
import os
import time
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Literal

import msgspec
from fastapi import APIRouter, Header, HTTPException, Query, Request, Response

from app.engine import snapshot as snapshot_store
from app.engine.evaluate import explain, judge_all
from app.engine.questions import build_queue
from app.engine.snapshot import SnapshotNotReady
from app.schemas.judgement import DISCLAIMER, JudgementResponse
from app.schemas.user import UserProfile

router = APIRouter(prefix="/v1", tags=["judge"])

Include = Literal["default", "all"]


def today_kst() -> date:
    """판정 기준일. 연령과 마감일은 한국 시간으로 세야 한다.

    UTC 로 세면 매일 09시간 동안 날짜가 하루 어긋나, 생일 당일인 사용자가
    하루 늦게 자격을 얻거나 마감 당일 정책이 하루 일찍 사라진다.
    한국은 서머타임이 없어 고정 +9 로 충분하다.
    """
    from app.core.config import settings

    # 오버라이드는 호출 시점에 읽는다. import 시점에만 읽으면 테스트나 데모에서
    # 기준일을 바꿔도 이미 굳어진 설정이 이겨버린다.
    override = os.environ.get("YPC_FIXED_TODAY") or settings.fixed_today
    if override:
        return date.fromisoformat(override)
    return (datetime.now(UTC) + timedelta(hours=9)).date()


def profile_hash(profile: UserProfile) -> str:
    """같은 조건인지 판별하는 지문. ETag 와 재계산 생략에 쓴다."""
    return hashlib.sha256(msgspec.json.encode(profile)).hexdigest()[:16]


@router.post("/judge")
async def judge(
    request: Request,
    include: Annotated[
        Include, Query(description="default=요약+적격/확인필요만, all=부적격 포함 전체")
    ] = "default",
    if_none_match: Annotated[str | None, Header(alias="If-None-Match")] = None,
) -> Response:
    try:
        snapshot = snapshot_store.holder.get()
    except SnapshotNotReady as e:
        raise HTTPException(status_code=503, detail=str(e)) from e

    body = await request.body()
    try:
        profile = msgspec.json.decode(body, type=UserProfile)
    except msgspec.ValidationError as e:
        raise HTTPException(status_code=422, detail=f"조건 입력이 올바르지 않습니다: {e}") from e

    etag = f'W/"{snapshot.version}:{profile_hash(profile)}:{include}"'
    if if_none_match == etag:
        return Response(status_code=304, headers={"ETag": etag})

    started = time.perf_counter()
    today = today_kst()
    verdicts = judge_all(snapshot, profile, today)

    results = []
    for i in range(snapshot.size):
        if verdicts.eligible[i]:
            verdict = "ELIGIBLE"
        elif verdicts.needs_info[i]:
            verdict = "NEEDS_INFO"
        else:
            verdict = "INELIGIBLE"
        if include == "default" and verdict == "INELIGIBLE":
            continue
        results.append(explain(snapshot, profile, today, i, verdict))

    payload = JudgementResponse(
        session_id=request.headers.get("X-Session-Id", "anonymous"),
        snapshot_version=snapshot.version,
        summary=verdicts.summary(),
        results=results,
        latency_ms=int((time.perf_counter() - started) * 1000),
        disclaimer=DISCLAIMER,
    )

    return Response(
        content=msgspec.json.encode(payload),
        media_type="application/json",
        headers={
            "ETag": etag,
            # 조건값이 섞인 응답이라 공용 캐시에 남으면 안 된다
            "Cache-Control": "private, no-store",
            "X-Snapshot-Version": snapshot.version,
        },
    )


@router.get("/policies/{policy_id}")
async def policy_detail(policy_id: str) -> Response:
    """정책 상세. 판정 없이 공고 내용과 근거만 본다 (S5 진입점)."""
    try:
        snapshot = snapshot_store.holder.get()
    except SnapshotNotReady as e:
        raise HTTPException(status_code=503, detail=str(e)) from e

    try:
        index = snapshot.index_of(policy_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=f"없는 정책입니다: {policy_id}") from e

    return Response(
        content=msgspec.json.encode(snapshot.policies[index]),
        media_type="application/json",
        headers={"Cache-Control": "public, max-age=300"},
    )


@router.get("/meta/snapshot")
async def snapshot_meta() -> dict[str, object]:
    """FE 가 캐시 무효화 시점을 스스로 판단할 수 있게 한다."""
    return snapshot_store.holder.info


@router.post("/questions")
async def questions(request: Request) -> Response:
    """조건을 받아 역질문 큐를 돌려준다 (S3).

    답변을 받는 별도 엔드포인트는 두지 않는다. 답은 UserProfile.answers 에 담아
    /v1/judge 나 이 엔드포인트를 다시 부르면 된다.

    PRD 는 '증분 재판정'을 요구하지만, 전 정책 재판정이 0.5ms 다.
    증분을 하려면 세션별 중간 상태를 서버가 들고 있어야 하는데, 0.5ms 를 아끼려고
    상태와 그 만료·정합성 문제를 떠안는 것은 남는 장사가 아니다. 재판정이
    느려지면 그때 도입한다.
    """
    try:
        snapshot = snapshot_store.holder.get()
    except SnapshotNotReady as e:
        raise HTTPException(status_code=503, detail=str(e)) from e

    body = await request.body()
    try:
        profile = msgspec.json.decode(body, type=UserProfile)
    except msgspec.ValidationError as e:
        raise HTTPException(status_code=422, detail=f"조건 입력이 올바르지 않습니다: {e}") from e

    today = today_kst()
    verdicts = judge_all(snapshot, profile, today)
    queue = build_queue(snapshot, profile, today, verdicts)

    return Response(
        content=msgspec.json.encode(queue),
        media_type="application/json",
        headers={"Cache-Control": "private, no-store", "X-Snapshot-Version": snapshot.version},
    )
