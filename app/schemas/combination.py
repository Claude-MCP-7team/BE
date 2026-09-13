"""조합 추천 결과 스키마 (US-04, S6 화면)."""

from __future__ import annotations

import msgspec


class CombinationMember(msgspec.Struct, kw_only=True, forbid_unknown_fields=True):
    policy_id: str
    title: str
    estimated_total_krw: int
    # 수혜액이 공고에 없어 추정치를 쓴 경우. 화면에서 구분 표시해야 한다.
    amount_estimated: bool = False


class ExcludedPolicy(msgspec.Struct, kw_only=True, forbid_unknown_fields=True):
    """조합에서 빠진 정책과 그 이유. '왜 빠졌는지'가 없으면 추천을 신뢰할 수 없다."""

    policy_id: str
    title: str
    estimated_total_krw: int
    conflicts_with: str  # 대신 선택된 정책
    conflicts_with_title: str
    confidence: str  # CONFIRMED | ESTIMATED
    conflict_type: str
    source_quote: str
    source_url: str | None = None
    dept_name: str | None = None
    dept_tel: str | None = None


class Combination(msgspec.Struct, kw_only=True, forbid_unknown_fields=True):
    rank: int
    total_krw: int
    members: list[CombinationMember]
    excluded: list[ExcludedPolicy] = msgspec.field(default_factory=list)
    # 합계에 추정 금액이 섞였는지
    total_is_estimated: bool = False


class Scenario(msgspec.Struct, kw_only=True, forbid_unknown_fields=True):
    """보수 / 최대 두 가지 해석 (PRD §7.4).

    '유사사업'의 정의가 공고문에 없어서, 추정 상충을 지킬지 말지는 사실상
    담당부서만 답할 수 있다. 그래서 한쪽을 고르지 않고 둘 다 보여준다.
    """

    kind: str  # conservative | maximal
    label: str
    description: str
    combinations: list[Combination]
    # 근사해로 계산했는지 (정점이 너무 많을 때)
    approximate: bool = False


class CombinationResponse(msgspec.Struct, kw_only=True, forbid_unknown_fields=True):
    snapshot_version: str
    eligible_count: int
    scenarios: list[Scenario]
    disclaimer: str
