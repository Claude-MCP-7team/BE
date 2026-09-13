"""PolicySchema — AI ↔ BE 계약면 (C1, G1 10/02 Freeze).

AI 역할의 A2 에이전트가 채우고, BE의 배치·룰엔진이 읽는다.
Freeze 이후 변경은 변경관리 절차를 경유한다.

msgspec.Struct 를 쓰는 이유: 디코딩이 Pydantic 대비 5~10배 빠르고,
배치가 수천 건을 반복 디코딩하는 경로라 차이가 실제로 드러난다.
API 경계(FastAPI 요청/응답)에서만 Pydantic 을 쓴다.
"""

from __future__ import annotations

import msgspec

from app.schemas.enums import (
    AuthorityLevel,
    BenefitType,
    BudgetRisk,
    Category,
    Confidence,
    ConflictType,
    CrossCheck,
    Operator,
    PolicyStatus,
)

# 룰의 value 가 가질 수 있는 형태. between 은 [하한, 상한], in/not_in 은 배열.
RuleValue = bool | int | float | str | list[bool | int | float | str]


class Source(msgspec.Struct, kw_only=True, forbid_unknown_fields=True):
    """출처 — 모든 판정에 원문 링크를 제공하기 위한 최소 정보 (PRD §7.6)."""

    api: str | None = None
    api_policy_no: str | None = None
    origin_url: str | None = None
    announcement_url: str | None = None
    crawled_at: str | None = None
    content_hash: str | None = None


class Dept(msgspec.Struct, kw_only=True, forbid_unknown_fields=True):
    """담당부서 — ESTIMATED/NEEDS_REVIEW 판정에 반드시 병기된다."""

    name: str | None = None
    tel: str | None = None


class Meta(msgspec.Struct, kw_only=True, forbid_unknown_fields=True):
    title: str
    category: Category
    authority_level: AuthorityLevel
    # 법정동/행정 코드. '00'=전국, '41'=경기도, '41465'=용인 수지구 (접두 체인)
    region_code: list[str] = msgspec.field(default_factory=list)
    dept: Dept = msgspec.field(default_factory=Dept)


class Benefit(msgspec.Struct, kw_only=True, forbid_unknown_fields=True):
    type: BenefitType | None = None
    amount_krw: int | None = None
    duration_months: int | None = None
    # 조합 최적화(MWIS)의 가중치. 미명시 정책 처리는 Q2 결정 사항.
    estimated_total_krw: int | None = None
    amount_confidence: Confidence = "ESTIMATED"


class Period(msgspec.Struct, kw_only=True, forbid_unknown_fields=True):
    apply_start: str | None = None
    apply_end: str | None = None
    is_rolling: bool = False
    budget_exhaust_risk: BudgetRisk | None = None


class Rule(msgspec.Struct, kw_only=True, forbid_unknown_fields=True):
    """자격요건 / 제외대상 1건.

    source_quote 는 필수다. 근거 없는 룰은 저장되지 않는다 (G1 게이트).
    DB 에서도 NOT NULL + 공백금지 CHECK 로 같은 규칙을 강제한다.
    """

    rule_id: str
    field: str
    op: Operator
    value: RuleValue
    source_quote: str

    unit: str | None = None
    basis: str | None = None
    source_offset: int | None = None
    source_url: str | None = None

    # 시간 경과로 충족 가능 → 충족 예상일 계산 대상
    time_satisfiable: bool = False
    # 조항이 모호함 → 애매조항 LLM 재판정 대상
    ambiguous: bool = False
    # 사용자에게 물어서 해소 가능 → 역질문 대상
    askable: bool = False
    # 있으면 C1 LLM 호출을 생략하고 그대로 쓴다 (비용 절감 경로)
    question_template: str | None = None

    confidence: Confidence = "CONFIRMED"


class Conflict(msgspec.Struct, kw_only=True, forbid_unknown_fields=True):
    """중복수혜 제한 — 공고문에서 추출한 원본 서술.

    여기서 간선을 확정하지 않는다. 확정·정규화는 BE 배치가 하고,
    결과는 policy_conflict_edge 에 들어간다 (AI는 관계 추출까지, 계산은 BE).
    """

    type: ConflictType
    source_quote: str

    target_policy_ids: list[str] = msgspec.field(default_factory=list)
    target_policy_name: str | None = None
    target_category: str | None = None
    target_benefit_type: str | None = None
    target_authority: str | None = None
    source_url: str | None = None
    confidence: str = "ESTIMATED"


class Document(msgspec.Struct, kw_only=True, forbid_unknown_fields=True):
    """필요서류. 발급 소요일은 공고문이 아니라 서류 마스터 테이블이 권위다."""

    doc_code: str | None = None  # 마스터 매핑 실패 시 None → 관리자 큐
    name: str
    issuer: str | None = None
    lead_time_business_days: int | None = None
    cost_krw: int | None = None
    notes: str | None = None
    source_quote: str | None = None


class Quality(msgspec.Struct, kw_only=True, forbid_unknown_fields=True):
    parse_confidence: float = 0.0
    cross_check: CrossCheck = "SKIPPED"
    needs_review_fields: list[str] = msgspec.field(default_factory=list)
    last_verified_at: str | None = None


class PolicySchema(msgspec.Struct, kw_only=True, forbid_unknown_fields=True):
    """정책 1건의 표준 스키마. 배치 출력이자 룰 엔진 입력."""

    policy_id: str
    meta: Meta

    status: PolicyStatus = "draft"
    source: Source = msgspec.field(default_factory=Source)
    benefit: Benefit = msgspec.field(default_factory=Benefit)
    period: Period = msgspec.field(default_factory=Period)

    eligibility: list[Rule] = msgspec.field(default_factory=list)
    exclusions: list[Rule] = msgspec.field(default_factory=list)
    conflicts: list[Conflict] = msgspec.field(default_factory=list)
    documents: list[Document] = msgspec.field(default_factory=list)

    quality: Quality = msgspec.field(default_factory=Quality)
