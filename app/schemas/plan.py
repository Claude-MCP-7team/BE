"""신청 계획 스키마 (US-05, S7 화면) — BE ↔ FE 계약면.

판정이 "받을 수 있는가"라면 계획은 "그래서 언제 뭘 들고 가는가"다.
화면이 답해야 하는 질문은 세 개뿐이다:
  지금 당장 움직여야 하는 게 있나        → urgent / status
  주민센터 한 번에 뭘 떼 오면 되나        → documents (서류 기준으로 묶은 것)
  이 정책은 언제 준비를 시작해야 하나      → recommended_start_date
"""

from __future__ import annotations

import msgspec

# 계획 상태.
#   URGENT       권장 착수일이 이미 지났거나 오늘이다 → 지금 시작해야 마감을 맞춘다
#   ON_TRACK     아직 여유가 있다
#   ROLLING      상시 모집 — 마감이 없어 역산 대상이 아니다
#   UNKNOWN      마감일이 공고에서 확인되지 않았다 (역산 불가, 부서 확인 필요)
#   CLOSED       이미 마감됐다
#   INFEASIBLE   지금 시작해도 서류 발급이 마감을 넘긴다
PlanStatus = str


class PlanDocument(msgspec.Struct, kw_only=True, forbid_unknown_fields=True):
    """한 정책에 필요한 서류 1종."""

    name: str
    doc_code: str | None = None
    issuer: str | None = None
    lead_time_business_days: int = 0
    cost_krw: int | None = None
    # 공고에도 서류 마스터에도 발급 소요일이 없어 추정한 경우.
    # 추정치로 역산한 날짜를 확정처럼 보여주면 사용자가 마감을 놓친다.
    lead_time_estimated: bool = False
    notes: str | None = None
    source_quote: str | None = None


class DocumentTask(msgspec.Struct, kw_only=True, forbid_unknown_fields=True):
    """서류 기준으로 묶은 할 일. 같은 서류를 여러 정책이 요구하면 한 번만 뗀다.

    정책별 목록만 주면 사용자는 주민등록등본을 정책 수만큼 떼러 간다.
    이 뷰의 존재 이유가 그것이다.
    """

    name: str
    doc_code: str | None = None
    issuer: str | None = None
    lead_time_business_days: int = 0
    lead_time_estimated: bool = False
    cost_krw: int | None = None
    # 이 서류를 요구하는 정책들
    required_by: list[str] = msgspec.field(default_factory=list)
    required_by_titles: list[str] = msgspec.field(default_factory=list)
    # 이 서류를 요구하는 정책 중 가장 이른 착수일 — 이 날까지는 손에 있어야 한다
    needed_by_date: str | None = None
    notes: str | None = None


class PolicyPlan(msgspec.Struct, kw_only=True, forbid_unknown_fields=True):
    """정책 1건의 신청 일정."""

    policy_id: str
    title: str
    status: PlanStatus

    apply_start_date: str | None = None
    deadline_date: str | None = None
    # 서류 준비를 시작해야 하는 날 (마감일에서 영업일 역산)
    recommended_start_date: str | None = None
    # 오늘부터 권장 착수일까지 남은 영업일. 음수면 이미 지났다.
    slack_business_days: int | None = None
    # 마감일까지 남은 영업일
    business_days_to_deadline: int | None = None
    # 서류 발급 + 제출에 필요한 총 영업일
    preparation_business_days: int = 0

    documents: list[PlanDocument] = msgspec.field(default_factory=list)

    # 왜 이 상태인지 한 줄로. 화면이 그대로 노출해도 되는 문장.
    reason: str = ""
    # 역산에 추정 소요일이 섞였다
    estimated: bool = False
    # 공휴일표가 덮지 않는 구간을 계산했다 → 날짜를 확정으로 쓰면 안 된다
    outside_calendar_coverage: bool = False

    # 부서 확인이 필요한 항목(UNKNOWN / estimated)에 동반된다
    dept_name: str | None = None
    dept_tel: str | None = None
    origin_url: str | None = None


class PlanSummary(msgspec.Struct, kw_only=True, forbid_unknown_fields=True):
    total: int = 0
    urgent: int = 0
    on_track: int = 0
    rolling: int = 0
    unknown_deadline: int = 0
    closed: int = 0
    infeasible: int = 0


class PlanResponse(msgspec.Struct, kw_only=True, forbid_unknown_fields=True):
    """GET /v1/plan 응답."""

    snapshot_version: str
    generated_for_date: str  # 기준일 (KST)
    summary: PlanSummary
    # 착수가 급한 순서로 정렬된다
    plans: list[PolicyPlan] = msgspec.field(default_factory=list)
    # 서류 기준으로 합친 할 일 목록
    documents: list[DocumentTask] = msgspec.field(default_factory=list)
    total_document_cost_krw: int = 0
    calendar_source_ref: str = ""
    disclaimer: str = ""
