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
from app.engine.compile import Snapshot
from app.engine.evaluate import explain, judge_all
from app.engine.questions import build_queue
from app.engine.snapshot import SnapshotNotReady
from app.planner.backplan import build_plan
from app.planner.ics import to_ics
from app.schemas.catalog import PolicyListResponse, PolicySummary
from app.schemas.enums import AuthorityLevel, Category
from app.schemas.judgement import DISCLAIMER, JudgementResponse
from app.schemas.plan import PlanResponse
from app.schemas.policy import PolicySchema
from app.schemas.user import UserProfile, region_chain
from app.solver.combine import recommend

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

    # 부적격도 상세를 조립한다. 충족 예상일이 있는지는 근거를 만들어 봐야
    # 알 수 있고(조건마다 날짜를 계산해야 한다), 그 정보가 없으면 "언제부터
    # 가능한가"가 기본 응답에서 통째로 사라진다. 3,000 정책 전체 상세가 31ms 라
    # 예산(p95 5,000ms) 안이다.
    results = []
    future_eligible = 0
    for i in range(snapshot.size):
        if verdicts.eligible[i]:
            verdict = "ELIGIBLE"
        elif verdicts.needs_info[i]:
            verdict = "NEEDS_INFO"
        else:
            verdict = "INELIGIBLE"

        result = explain(snapshot, profile, today, i, verdict)
        if result.future_eligible_from:
            future_eligible += 1
        # 기본 응답에서 빼는 것은 '영영 안 되는' 부적격뿐이다. 수백 건의 근거를
        # 매번 실어 봐야 대부분 읽히지 않는다. 반면 시간이 지나면 가능한 정책은
        # 사용자가 지금 행동을 정하는 데 쓰는 정보라 빼면 안 된다.
        if include == "default" and verdict == "INELIGIBLE" and not result.future_eligible_from:
            continue
        results.append(result)

    summary = verdicts.summary()
    summary.future_eligible = future_eligible

    payload = JudgementResponse(
        session_id=request.headers.get("X-Session-Id", "anonymous"),
        snapshot_version=snapshot.version,
        summary=summary,
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


@router.get("/policies")
async def policy_list(
    region: Annotated[
        str | None, Query(description="법정동 코드. 상위 지역과 전국 정책도 함께 나온다")
    ] = None,
    category: Annotated[Category | None, Query(description="정책 분야")] = None,
    authority_level: Annotated[AuthorityLevel | None, Query(description="주관 수준")] = None,
    q: Annotated[str | None, Query(description="제목 부분일치 (대소문자 무시)")] = None,
    limit: Annotated[int, Query(ge=1, le=100, description="한 페이지 건수")] = 20,
    offset: Annotated[int, Query(ge=0, description="건너뛸 건수")] = 0,
    if_none_match: Annotated[str | None, Header(alias="If-None-Match")] = None,
) -> Response:
    """정책 목록 (S2 진입점). 판정 없이 카탈로그만 본다.

    **판정과 섞지 않는다.** 판정은 프로필이 있어야 하고 사용자마다 다르지만,
    목록은 누가 보든 같다. 섞으면 공용 캐시를 못 쓰고, 프로필 없이 정책을
    둘러보는 화면이 불가능해진다. 판정 결과가 필요하면 `/v1/judge` 를 쓴다.

    **지역은 접두 체인으로 넓힌다.** `region=41190` 은 부천시 정책만이 아니라
    경기도(41)와 전국(00) 정책도 포함한다. 좁게 매칭하면 사용자는 자기가 받을
    수 있는 전국 정책을 목록에서 보지 못하는데, 이 누락은 화면상 '없음'과
    구별되지 않는다.

    **정렬은 결정론이다.** 마감 임박순(마감 없는 것은 뒤로), 같으면 policy_id.
    스냅샷 순서를 그대로 쓰면 수집 순서가 바뀔 때 페이지 경계에서 정책이
    조용히 건너뛰어진다 — 같은 조건에 같은 결과라는 약속이 깨진다.
    """
    try:
        snapshot = snapshot_store.holder.get()
    except SnapshotNotReady as e:
        raise HTTPException(status_code=503, detail=str(e)) from e

    # 목록은 프로필이 섞이지 않으므로 질의만으로 캐시 키가 성립한다.
    key = f"{region}|{category}|{authority_level}|{q}|{limit}|{offset}"
    etag = f'W/"{snapshot.version}:{hashlib.sha256(key.encode()).hexdigest()[:16]}"'
    if if_none_match == etag:
        return Response(status_code=304, headers={"ETag": etag})

    wanted_regions = set(region_chain(region)) if region else None
    needle = q.casefold().strip() if q else None

    matched = [
        (policy, index)
        for index, policy in enumerate(snapshot.policies)
        if _matches(policy, wanted_regions, category, authority_level, needle)
    ]
    matched.sort(key=lambda pair: (_deadline_key(pair[0]), pair[0].policy_id))

    payload = PolicyListResponse(
        snapshot_version=snapshot.version,
        total=len(matched),
        limit=limit,
        offset=offset,
        items=[
            _summarize(policy, len(snapshot.rules_by_policy[index]))
            for policy, index in matched[offset : offset + limit]
        ],
    )

    return Response(
        content=msgspec.json.encode(payload),
        media_type="application/json",
        headers={
            "ETag": etag,
            # 프로필이 섞이지 않는 응답이라 공용 캐시에 남겨도 된다.
            "Cache-Control": "public, max-age=300",
            "X-Snapshot-Version": snapshot.version,
        },
    )


def _matches(
    policy: PolicySchema,
    wanted_regions: set[str] | None,
    category: str | None,
    authority_level: str | None,
    needle: str | None,
) -> bool:
    if category and policy.meta.category != category:
        return False
    if authority_level and policy.meta.authority_level != authority_level:
        return False
    if needle and needle not in policy.meta.title.casefold():
        return False
    # 지역 표기가 없는 정책은 범위를 알 수 없다. 전국으로 단정하면 남의 지역
    # 정책을 권하게 되고, 빼면 조용히 사라진다 — 후자가 더 나쁘므로 남긴다.
    return not (
        wanted_regions is not None
        and policy.meta.region_code
        and not wanted_regions.intersection(policy.meta.region_code)
    )


# 마감 없는 정책(상시모집 등)을 앞에 두면 마감 임박 정책이 뒤로 밀린다.
_NO_DEADLINE = "9999-12-31"


def _deadline_key(policy: PolicySchema) -> str:
    return policy.period.apply_end or _NO_DEADLINE


def _summarize(policy: PolicySchema, rule_count: int) -> PolicySummary:
    return PolicySummary(
        policy_id=policy.policy_id,
        title=policy.meta.title,
        category=policy.meta.category,
        authority_level=policy.meta.authority_level,
        status=policy.status,
        region_code=list(policy.meta.region_code),
        dept_name=policy.meta.dept.name,
        dept_tel=policy.meta.dept.tel,
        origin_url=policy.source.origin_url or policy.source.announcement_url,
        benefit_type=policy.benefit.type,
        amount_krw=policy.benefit.amount_krw,
        duration_months=policy.benefit.duration_months,
        estimated_total_krw=policy.benefit.estimated_total_krw,
        amount_confidence=policy.benefit.amount_confidence,
        apply_start=policy.period.apply_start,
        apply_end=policy.period.apply_end,
        is_rolling=policy.period.is_rolling,
        rule_count=rule_count,
        document_count=len(policy.documents),
        conflict_count=len(policy.conflicts),
        needs_review_fields=list(policy.quality.needs_review_fields),
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


@router.post("/combinations")
async def combinations(request: Request) -> Response:
    """적격 정책들의 최적 조합을 보수/최대 2안으로 돌려준다 (S6).

    조합 계산은 LLM 이 아니라 솔버가 한다 (PRD §7.4). 정확해이며, 결과가
    완전탐색과 일치하는지는 테스트가 매번 확인한다.
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

    verdicts = judge_all(snapshot, profile, today_kst())
    payload = recommend(snapshot, verdicts)

    return Response(
        content=msgspec.json.encode(payload),
        media_type="application/json",
        headers={"Cache-Control": "private, no-store", "X-Snapshot-Version": snapshot.version},
    )


async def _plan_from_request(request: Request) -> tuple[Snapshot, PlanResponse]:
    """요청 본문 → (스냅샷, 계획). /plan 과 /plan.ics 가 공유한다.

    본문은 UserProfile 이며, 선택적으로 `X-Policy-Ids` 헤더에 쉼표로 구분된
    정책 목록을 주면 그 정책들만 계획한다 (S6 에서 고른 조합의 일정만 보는 경로).
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

    raw_ids = request.headers.get("X-Policy-Ids")
    policy_ids = [s.strip() for s in raw_ids.split(",") if s.strip()] if raw_ids else None

    today = today_kst()
    verdicts = judge_all(snapshot, profile, today)
    plan = build_plan(snapshot, verdicts, today, policy_ids=policy_ids)
    return snapshot, plan


@router.post("/plan")
async def plan(request: Request) -> Response:
    """적격 정책의 신청 일정 — 서류·권장 착수일 (US-05, S7).

    판정과 마찬가지로 GET 이 아니라 POST 다. 조건이 본문으로 들어오기 때문이다.
    세션 저장(BE-M1-2)이 들어오면 `GET /v1/plan/{session_id}` 가 이 위에 얹힌다.
    """
    snapshot, payload = await _plan_from_request(request)

    return Response(
        content=msgspec.json.encode(payload),
        media_type="application/json",
        headers={
            "Cache-Control": "private, no-store",
            "X-Snapshot-Version": snapshot.version,
        },
    )


@router.post("/plan.ics")
async def plan_ics(request: Request) -> Response:
    """같은 계획을 캘린더로. 사용자가 앱을 다시 열지 않아도 마감을 기억하게 한다."""
    _, payload = await _plan_from_request(request)

    return Response(
        content=to_ics(payload),
        media_type="text/calendar; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="ypc-plan.ics"',
            "Cache-Control": "private, no-store",
        },
    )
