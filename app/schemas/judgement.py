"""JudgementResult — 판정 출력. BE ↔ FE 계약면 (C2, 10/07 확정).

설계 규칙 (PRD §7.6, 타협 없음):
  - 모든 판정에 confidence 가 붙는다
  - 미충족/미확인 항목에는 source_quote 와 원문 링크가 반드시 동반된다
  - ESTIMATED / NEEDS_REVIEW 에는 담당부서 연락처가 병기된다
이 규칙들은 app/schemas/validate.py 가 런타임에 강제한다.
"""

from __future__ import annotations

import msgspec

from app.schemas.enums import Confidence, Verdict

# 룰이 비교하는 값의 형태. object 로 두면 계약서(JSON Schema)를 만들 수 없고,
# FE 가 어떤 타입이 올지 알 수 없다.
Scalar = bool | int | float | str
JsonValue = Scalar | list[Scalar] | None

DISCLAIMER = (
    "본 서비스의 판정 결과는 공고문 분석에 기반한 참고 정보이며 법적 효력이 없습니다. "
    "최종 자격 여부는 각 정책 담당부서의 확인을 받으시기 바랍니다."
)


class MatchedRule(msgspec.Struct, kw_only=True, forbid_unknown_fields=True):
    """충족한 조건."""

    rule_id: str
    field: str
    user_value: JsonValue
    source_quote: str
    source_url: str | None = None


class UnmatchedRule(msgspec.Struct, kw_only=True, forbid_unknown_fields=True):
    """미충족 조건 — '왜 안 되는지'와 '언제부터 되는지'를 담는다 (US-02)."""

    rule_id: str
    field: str
    user_value: JsonValue
    required: JsonValue
    source_quote: str

    unit: str | None = None
    source_url: str | None = None
    time_satisfiable: bool = False
    # 시간 경과로 충족 가능한 조건의 충족 예상일 (YYYY-MM-DD)
    satisfiable_from: str | None = None
    # 상한 초과 등으로 영구히 충족 불가 (예: 연령 상한)
    permanently_unsatisfiable: bool = False


class UnknownRule(msgspec.Struct, kw_only=True, forbid_unknown_fields=True):
    """판정에 필요한데 사용자 입력에 없는 항목 → 역질문 대상 (US-03)."""

    rule_id: str
    field: str
    source_quote: str
    question_template: str | None = None
    source_url: str | None = None


class JudgementResult(msgspec.Struct, kw_only=True, forbid_unknown_fields=True):
    policy_id: str
    verdict: Verdict
    confidence: Confidence

    matched: list[MatchedRule] = msgspec.field(default_factory=list)
    unmatched: list[UnmatchedRule] = msgspec.field(default_factory=list)
    unknown: list[UnknownRule] = msgspec.field(default_factory=list)

    # 룰로 옮기지 못해 엔진이 평가하지 않은 조건의 필드명.
    # 비어 있지 않으면 이 판정은 공고문 전체가 아니라 '옮길 수 있었던 부분'에
    # 대한 것이다. 화면은 무엇을 확인 못 했는지 사용자에게 알려야 한다 —
    # confidence 만 낮추면 '왜 확인이 필요한지'를 답할 수 없다.
    needs_review_fields: list[str] = msgspec.field(default_factory=list)

    # C2 에이전트가 생성한 자연어 설명. 배치 경로가 아니라 요청 시 생성된다.
    explanation: str | None = None

    # ESTIMATED / NEEDS_REVIEW 일 때 필수 (validate.py 가 검사)
    dept_name: str | None = None
    dept_tel: str | None = None
    origin_url: str | None = None

    disclaimer_required: bool = True

    @property
    def needs_dept_contact(self) -> bool:
        return self.confidence in ("ESTIMATED", "NEEDS_REVIEW")


class JudgementSummary(msgspec.Struct, kw_only=True, forbid_unknown_fields=True):
    """S4 대시보드 상단 요약 (US-01: 분류별 건수)."""

    eligible: int = 0
    ineligible: int = 0
    needs_info: int = 0

    @property
    def total(self) -> int:
        return self.eligible + self.ineligible + self.needs_info


class JudgementResponse(msgspec.Struct, kw_only=True, forbid_unknown_fields=True):
    """POST /v1/judge 응답."""

    session_id: str
    snapshot_version: str
    summary: JudgementSummary
    results: list[JudgementResult]
    latency_ms: int = 0
    disclaimer: str = DISCLAIMER
