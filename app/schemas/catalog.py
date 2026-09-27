"""정책 목록 응답 스키마 — BE ↔ FE (S2 정책 목록 화면).

`app/schemas/policy.py` 와 분리한 이유: 저쪽은 AI 가 채우고 BE 가 읽는 계약면(C1)
이고, 이쪽은 BE 가 채우고 FE 가 읽는 표현면이다. 한 파일에 두면 C1 Freeze 이후
화면 사정으로 필드를 추가할 때마다 AI 쪽 계약서까지 흔들린다.

목록은 **요약만** 내보낸다. 실스냅샷이 1,555건인데 PolicySchema 전문을 그대로
실으면 룰·근거 인용까지 딸려와 응답이 수 MB 가 된다. 목록 화면이 쓰는 것은
제목·지역·금액·마감뿐이고, 나머지는 `GET /v1/policies/{id}` 가 준다.
"""

from __future__ import annotations

import msgspec

from app.schemas.enums import AuthorityLevel, BenefitType, Category, Confidence, PolicyStatus


class PolicySummary(msgspec.Struct, kw_only=True, forbid_unknown_fields=True):
    """목록 카드 1장이 쓰는 필드."""

    policy_id: str
    title: str
    category: Category
    authority_level: AuthorityLevel
    status: PolicyStatus
    region_code: list[str] = msgspec.field(default_factory=list)

    dept_name: str | None = None
    dept_tel: str | None = None
    origin_url: str | None = None

    benefit_type: BenefitType | None = None
    amount_krw: int | None = None
    duration_months: int | None = None
    estimated_total_krw: int | None = None
    # 금액이 추정치인지. 화면이 확정값처럼 보이게 하면 안 된다.
    amount_confidence: Confidence = "ESTIMATED"

    apply_start: str | None = None
    apply_end: str | None = None
    is_rolling: bool = False

    # 상세를 열기 전에 '이 정책이 무엇을 요구하는지' 가늠할 수 있게 하는 수치.
    rule_count: int = 0
    document_count: int = 0
    conflict_count: int = 0
    # 구조화하지 못해 사람 확인이 필요한 필드. 비어 있지 않으면 화면이 그렇게 알린다.
    needs_review_fields: list[str] = msgspec.field(default_factory=list)


class PolicyListResponse(msgspec.Struct, kw_only=True, forbid_unknown_fields=True):
    """페이지 1장.

    total 은 필터를 적용한 뒤의 전체 건수다. 페이지 길이가 아니라 이 값으로
    페이지네이션을 그려야, 마지막 페이지에서 '더 있음'이 사라지지 않는다.
    """

    snapshot_version: str
    total: int
    limit: int
    offset: int
    items: list[PolicySummary]
