"""권장 착수일 역산 (US-05) — "언제부터 준비를 시작해야 마감을 맞추나".

계산 자체는 한 줄이다: `착수일 = 마감일 − 준비 영업일`. 판단이 필요한 곳은
'준비 영업일'을 어떻게 세느냐다.

**서류 발급은 병렬이다 — 더하지 않고 최댓값을 쓴다.**
  등본(즉시)과 소득금액증명(1일)과 가족관계증명서(즉시)를 요구하는 정책에서
  소요일을 더하면 3일이 나오지만, 실제로는 한 번 방문해 셋을 함께 신청하고
  가장 오래 걸리는 하나를 기다린다. 더하기는 사용자를 실제보다 일찍 움직이게
  만든다 — 안전해 보이지만, 모든 정책이 동시에 '급함'으로 뜨면 무엇이 진짜
  급한지 구별이 사라진다. 그래서 max 를 쓰고, 여기에 제출 버퍼를 더한다.

**추정 소요일은 늘리는 쪽으로만 쓴다.**
  서류 마스터에 없는 서류의 소요일은 모른다. 0 으로 두면 "오늘 시작해도 된다"가
  되고, 그 말을 믿은 사용자는 마감 전날 발급에 3일 걸린다는 걸 알게 된다.
  모를 때는 보수적 기본값을 쓰고 `estimated` 로 표시한다.

**마감일이 없으면 날짜를 만들어내지 않는다.**
  상시 모집(ROLLING)과 마감 미상(UNKNOWN)은 다르다. 전자는 역산할 필요가
  없고, 후자는 역산할 수 없다. 둘을 같은 상태로 뭉뚱그리면, 확인이 필요한
  정책이 '여유 있음'으로 보인다.
"""

from __future__ import annotations

from datetime import date

from app.engine.compile import Snapshot
from app.engine.evaluate import Verdicts
from app.planner.businessday import CalendarCoverageError, HolidayCalendar, bundled_calendar
from app.schemas.judgement import DISCLAIMER
from app.schemas.plan import (
    DocumentTask,
    PlanDocument,
    PlanResponse,
    PlanSummary,
    PolicyPlan,
)
from app.schemas.policy import Document, PolicySchema

# 서류를 손에 넣은 뒤 접수까지의 여유 영업일.
# 0 으로 두면 '서류 나오는 날 = 마감일'이 되어, 온라인 접수 오류나 창구 마감시간
# 같은 흔한 사고 하나에 신청이 통째로 무산된다.
SUBMISSION_BUFFER_BUSINESS_DAYS = 1

# 발급 소요일을 모르는 서류의 잠정값. 즉시발급(0)과 우편/심사(5+) 사이에서,
# 온라인 발급이 안 되는 서류가 대체로 하루 이틀인 점을 감안한 보수값이다.
# 서류 마스터(document 테이블)가 채워지면 이 값을 쓸 일은 사라진다.
DEFAULT_UNKNOWN_LEAD_BUSINESS_DAYS = 2

# 착수일까지 남은 영업일이 이 값 이하이면 '지금 움직여야 한다'로 본다.
URGENT_THRESHOLD_BUSINESS_DAYS = 0


def build_plan(
    snapshot: Snapshot,
    verdicts: Verdicts,
    today: date,
    calendar: HolidayCalendar | None = None,
    policy_ids: list[str] | None = None,
) -> PlanResponse:
    """적격 정책들의 신청 일정을 만든다.

    `policy_ids` 를 주면 그 정책들만 계획한다 — 사용자가 조합 추천(S6)에서
    특정 조합을 고른 뒤 그 조합의 일정만 보는 경로다. 주지 않으면 적격 전체.
    """
    cal = calendar or bundled_calendar()

    if policy_ids is None:
        targets = [snapshot.policies[i] for i in range(snapshot.size) if verdicts.eligible[i]]
    else:
        wanted = set(policy_ids)
        targets = [p for p in snapshot.policies if p.policy_id in wanted]

    plans = [_plan_for(policy, today, cal) for policy in targets]
    plans.sort(key=_urgency_key)

    tasks = _document_tasks(targets, plans)

    return PlanResponse(
        snapshot_version=snapshot.version,
        generated_for_date=today.isoformat(),
        summary=_summarize(plans),
        plans=plans,
        documents=tasks,
        total_document_cost_krw=sum(t.cost_krw or 0 for t in tasks),
        calendar_source_ref=cal.source_ref,
        disclaimer=DISCLAIMER,
    )


# --- 정책 1건 ---------------------------------------------------------------


def _plan_for(policy: PolicySchema, today: date, cal: HolidayCalendar) -> PolicyPlan:
    docs = [_plan_document(d) for d in policy.documents]
    prep, prep_estimated = _preparation_days(docs)

    plan = PolicyPlan(
        policy_id=policy.policy_id,
        title=policy.meta.title,
        status="UNKNOWN",
        apply_start_date=policy.period.apply_start,
        deadline_date=policy.period.apply_end,
        preparation_business_days=prep,
        documents=docs,
        estimated=prep_estimated,
        dept_name=policy.meta.dept.name,
        dept_tel=policy.meta.dept.tel,
        origin_url=policy.source.announcement_url or policy.source.origin_url,
    )

    deadline = _parse_date(policy.period.apply_end)

    if deadline is None:
        if policy.period.is_rolling:
            plan.status = "ROLLING"
            plan.reason = (
                f"상시 모집입니다. 서류 준비에 영업일 {prep}일이 필요하니 "
                "신청하려는 시점에서 역산해 움직이면 됩니다."
            )
        else:
            plan.status = "UNKNOWN"
            plan.reason = (
                "공고에서 신청 마감일을 확인하지 못해 착수일을 역산할 수 없습니다. "
                "담당부서에 마감일을 확인해 주세요."
            )
        return plan

    if deadline < today:
        plan.status = "CLOSED"
        plan.reason = f"{deadline.isoformat()} 에 마감되었습니다."
        plan.business_days_to_deadline = cal.business_days_between(today, deadline)
        return plan

    # 여기서부터 달력 산술. 표가 덮지 않는 연도면 계산은 하되 사실로 못 박지 않는다.
    plan.outside_calendar_coverage = not (cal.covers(today) and cal.covers(deadline))

    try:
        plan.business_days_to_deadline = cal.business_days_between(today, deadline)
        start = cal.subtract_business_days(deadline, prep)
        plan.recommended_start_date = start.isoformat()
        plan.slack_business_days = cal.business_days_between(today, start)
    except CalendarCoverageError as e:  # pragma: no cover - 표가 비정상일 때만
        plan.status = "UNKNOWN"
        plan.reason = f"영업일을 계산할 수 없습니다: {e}"
        return plan

    if start < today:
        # 오늘 시작해도 서류가 마감 전에 안 나온다
        plan.status = "INFEASIBLE"
        plan.reason = (
            f"마감({deadline.isoformat()})까지 영업일 {plan.business_days_to_deadline}일 남았는데 "
            f"서류 준비에 영업일 {prep}일이 필요합니다. "
            "즉시발급 가능한 창구나 접수 마감 연장 여부를 담당부서에 확인해 주세요."
        )
    elif (plan.slack_business_days or 0) <= URGENT_THRESHOLD_BUSINESS_DAYS:
        plan.status = "URGENT"
        plan.reason = (
            f"오늘 서류 준비를 시작해야 {deadline.isoformat()} 마감에 맞출 수 있습니다."
        )
    else:
        plan.status = "ON_TRACK"
        plan.reason = (
            f"{start.isoformat()} 까지 서류 준비를 시작하면 됩니다 "
            f"(여유 영업일 {plan.slack_business_days}일)."
        )

    if plan.estimated:
        plan.reason += " 일부 서류의 발급 소요일은 추정치입니다."

    return plan


def _plan_document(doc: Document) -> PlanDocument:
    """공고의 서류 항목을 계획용으로 바꾼다.

    소요일의 권위는 서류 마스터(document 테이블)다 — 공고문은 "등본 1부"까지만
    적고 며칠 걸리는지는 쓰지 않는다. 배치가 마스터를 매핑하며 값을 채워 넣고,
    매핑에 실패하면 여기서 추정값으로 메운다.
    """
    lead = doc.lead_time_business_days
    estimated = lead is None
    return PlanDocument(
        name=doc.name,
        doc_code=doc.doc_code,
        issuer=doc.issuer,
        lead_time_business_days=DEFAULT_UNKNOWN_LEAD_BUSINESS_DAYS if estimated else lead or 0,
        cost_krw=doc.cost_krw,
        lead_time_estimated=estimated,
        notes=doc.notes,
        source_quote=doc.source_quote,
    )


def _preparation_days(docs: list[PlanDocument]) -> tuple[int, bool]:
    """서류 준비에 필요한 총 영업일과, 추정치가 섞였는지.

    발급은 병렬이므로 최댓값 + 제출 버퍼. (모듈 docstring 참조)
    서류가 아예 없는 정책도 접수 자체에 하루는 남겨 둔다.
    """
    if not docs:
        return SUBMISSION_BUFFER_BUSINESS_DAYS, False
    slowest = max(d.lead_time_business_days for d in docs)
    estimated = any(d.lead_time_estimated for d in docs)
    return slowest + SUBMISSION_BUFFER_BUSINESS_DAYS, estimated


# --- 서류 기준 뷰 -----------------------------------------------------------


def _document_tasks(policies: list[PolicySchema], plans: list[PolicyPlan]) -> list[DocumentTask]:
    """정책별 서류 목록을 서류 기준으로 뒤집어 합친다.

    합치는 키는 `doc_code` 가 우선이다. 같은 서류를 공고마다 다르게 적기
    때문에("주민등록등본" / "주민등록표 등본" / "등본"), 이름으로 합치면
    같은 서류가 세 줄로 남는다. 마스터 매핑에 실패해 코드가 없는 서류만
    이름으로 합친다.
    """
    start_by_policy = {p.policy_id: p.recommended_start_date for p in plans}
    planned = {p.policy_id for p in plans}

    tasks: dict[str, DocumentTask] = {}
    for policy in policies:
        if policy.policy_id not in planned:
            continue
        for raw in policy.documents:
            doc = _plan_document(raw)
            key = f"code:{doc.doc_code}" if doc.doc_code else f"name:{doc.name}"
            task = tasks.get(key)
            if task is None:
                task = DocumentTask(
                    name=doc.name,
                    doc_code=doc.doc_code,
                    issuer=doc.issuer,
                    lead_time_business_days=doc.lead_time_business_days,
                    lead_time_estimated=doc.lead_time_estimated,
                    cost_krw=doc.cost_krw,
                    notes=doc.notes,
                )
                tasks[key] = task
            else:
                # 같은 서류인데 공고마다 값이 다르면 오래 걸리는 쪽을 남긴다.
                # 짧은 쪽을 믿으면 그 서류 때문에 마감을 놓친다.
                if doc.lead_time_business_days > task.lead_time_business_days:
                    task.lead_time_business_days = doc.lead_time_business_days
                    task.lead_time_estimated = doc.lead_time_estimated
                if task.issuer is None:
                    task.issuer = doc.issuer
                if task.cost_krw is None:
                    task.cost_krw = doc.cost_krw

            task.required_by.append(policy.policy_id)
            task.required_by_titles.append(policy.meta.title)

            # 가장 이른 착수일이 이 서류의 기한이다
            start = start_by_policy.get(policy.policy_id)
            if start and (task.needed_by_date is None or start < task.needed_by_date):
                task.needed_by_date = start

    ordered = list(tasks.values())
    # 기한이 이른 것 먼저, 기한 미상은 뒤로. 같으면 여러 정책이 쓰는 서류를 위로.
    ordered.sort(key=lambda t: (t.needed_by_date or "9999-12-31", -len(t.required_by), t.name))
    return ordered


# --- 정렬 · 집계 ------------------------------------------------------------

# 화면 상단에 와야 하는 순서. 사용자가 먼저 봐야 하는 것은 '지금 못 하면 끝나는 것'이다.
_STATUS_ORDER = {
    "INFEASIBLE": 0,
    "URGENT": 1,
    "ON_TRACK": 2,
    "UNKNOWN": 3,
    "ROLLING": 4,
    "CLOSED": 5,
}


def _urgency_key(plan: PolicyPlan) -> tuple[int, int, str]:
    slack = plan.slack_business_days
    return (
        _STATUS_ORDER.get(plan.status, 9),
        slack if slack is not None else 10**6,
        plan.policy_id,
    )


def _summarize(plans: list[PolicyPlan]) -> PlanSummary:
    summary = PlanSummary(total=len(plans))
    for plan in plans:
        if plan.status == "URGENT":
            summary.urgent += 1
        elif plan.status == "ON_TRACK":
            summary.on_track += 1
        elif plan.status == "ROLLING":
            summary.rolling += 1
        elif plan.status == "UNKNOWN":
            summary.unknown_deadline += 1
        elif plan.status == "CLOSED":
            summary.closed += 1
        elif plan.status == "INFEASIBLE":
            summary.infeasible += 1
    return summary


def _parse_date(raw: str | None) -> date | None:
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        # 배치가 정규화하지 못한 날짜 표기. 여기서 추측해 고치면 틀린 마감일로
        # 역산하게 되므로, 마감 미상으로 떨어뜨려 부서 확인 경로로 보낸다.
        return None
