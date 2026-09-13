"""스키마 밸리데이터 — 게이트 기준을 문서가 아니라 코드가 강제한다.

배치가 PolicySchema 를 DB에 넣기 직전에 이 검사를 통과해야 한다.
DB의 CHECK 제약이 최후 방어선이라면, 여기는 "왜 거부됐는지"를 알려주는 1차 방어선이다.
DB는 거부만 하고 이유를 필드 단위로 설명하지 못하므로 둘 다 필요하다.

강제하는 규칙 (마일스톤 G1 / PRD §7.1, §7.6):
  1. source_quote 100% — 근거 없는 룰은 거부 (타협 대상 아님)
  2. 룰의 field 는 KNOWN_FIELDS 안에 있어야 함 — 알 수 없는 필드는 조용히 통과시키지 않음
  3. op 와 value 의 형태가 맞아야 함 — between 은 2원소, in 은 배열
  4. askable 이면 question_template 필수
  5. time_satisfiable 은 시간으로 충족 가능한 필드에만
  6. ESTIMATED/NEEDS_REVIEW 판정에는 담당부서 연락처 필수
"""

from __future__ import annotations

import msgspec

from app.schemas.enums import KNOWN_FIELDS, TIME_SATISFIABLE_FIELDS
from app.schemas.judgement import JudgementResult
from app.schemas.policy import PolicySchema, Rule

# op 별로 value 가 만족해야 하는 형태
_SCALAR_OPS = frozenset({"==", "!=", ">", ">=", "<", "<="})
_ARRAY_OPS = frozenset({"in", "not_in"})


class SchemaViolation(msgspec.Struct, kw_only=True):
    """위반 1건. 필드 경로와 이유를 함께 담아 관리자 큐로 보낼 수 있게 한다."""

    path: str
    code: str
    message: str

    def __str__(self) -> str:  # pragma: no cover - 디버깅 편의
        return f"{self.path}: [{self.code}] {self.message}"


class SchemaValidationError(Exception):
    def __init__(self, violations: list[SchemaViolation]) -> None:
        self.violations = violations
        super().__init__(
            f"{len(violations)}건의 스키마 위반: "
            + "; ".join(str(v) for v in violations[:5])
        )


def validate_policy(policy: PolicySchema) -> list[SchemaViolation]:
    """PolicySchema 를 검사하고 위반 목록을 돌려준다. 빈 리스트면 통과."""
    out: list[SchemaViolation] = []

    for i, rule in enumerate(policy.eligibility):
        out += _validate_rule(rule, f"eligibility[{i}]")
    for i, rule in enumerate(policy.exclusions):
        out += _validate_rule(rule, f"exclusions[{i}]")

    for i, c in enumerate(policy.conflicts):
        if not c.source_quote.strip():
            out.append(
                SchemaViolation(
                    path=f"conflicts[{i}].source_quote",
                    code="MISSING_SOURCE_QUOTE",
                    message="상충 관계에도 공고문 근거가 필요합니다",
                )
            )
        if c.type == "explicit_policy" and not (
            c.target_policy_ids or c.target_policy_name
        ):
            out.append(
                SchemaViolation(
                    path=f"conflicts[{i}]",
                    code="CONFLICT_TARGET_MISSING",
                    message="explicit_policy 는 대상 정책 ID 또는 정책명이 필요합니다",
                )
            )
        if c.type == "category_overlap" and not c.target_category:
            out.append(
                SchemaViolation(
                    path=f"conflicts[{i}]",
                    code="CONFLICT_TARGET_MISSING",
                    message="category_overlap 은 target_category 가 필요합니다",
                )
            )
        if c.type == "same_authority" and not c.target_authority:
            out.append(
                SchemaViolation(
                    path=f"conflicts[{i}]",
                    code="CONFLICT_TARGET_MISSING",
                    message="same_authority 는 target_authority 가 필요합니다",
                )
            )

    # 게시하려면 판정에 필요한 최소 정보가 있어야 한다
    if policy.status == "published":
        if not policy.eligibility:
            out.append(
                SchemaViolation(
                    path="eligibility",
                    code="NO_ELIGIBILITY_RULE",
                    message="자격요건이 하나도 없는 정책은 게시할 수 없습니다",
                )
            )
        if not policy.meta.region_code:
            out.append(
                SchemaViolation(
                    path="meta.region_code",
                    code="NO_REGION",
                    message="지역 코드가 없으면 후보 축소가 불가능합니다",
                )
            )
        if not (policy.source.origin_url or policy.source.announcement_url):
            out.append(
                SchemaViolation(
                    path="source",
                    code="NO_ORIGIN_URL",
                    message="원문 링크는 모든 경우에 제공되어야 합니다 (PRD §7.6)",
                )
            )

    return out


def _validate_rule(rule: Rule, path: str) -> list[SchemaViolation]:
    out: list[SchemaViolation] = []

    # 1. source_quote 100% — 이 검사는 완화되지 않는다
    if not rule.source_quote.strip():
        out.append(
            SchemaViolation(
                path=f"{path}.source_quote",
                code="MISSING_SOURCE_QUOTE",
                message="근거 구절 없는 룰은 저장할 수 없습니다 (G1, 타협 대상 아님)",
            )
        )

    # 2. 알 수 없는 필드를 조용히 통과시키지 않는다
    if rule.field not in KNOWN_FIELDS:
        out.append(
            SchemaViolation(
                path=f"{path}.field",
                code="UNKNOWN_FIELD",
                message=(
                    f"'{rule.field}' 는 룰 엔진이 평가할 수 없는 필드입니다. "
                    "KNOWN_FIELDS 에 추가하거나 룰을 제거하세요"
                ),
            )
        )

    # 3. op ↔ value 형태 일치
    out += _validate_op_value(rule, path)

    # 4. 역질문 대상이면 질문 템플릿 필수
    if rule.askable and not rule.question_template:
        out.append(
            SchemaViolation(
                path=f"{path}.question_template",
                code="ASKABLE_WITHOUT_QUESTION",
                message="askable=true 인 룰에는 question_template 이 필요합니다",
            )
        )

    # 5. 시간으로 충족 불가능한 필드에 time_satisfiable 을 달면 오답이 나온다
    if rule.time_satisfiable and rule.field not in TIME_SATISFIABLE_FIELDS:
        out.append(
            SchemaViolation(
                path=f"{path}.time_satisfiable",
                code="NOT_TIME_SATISFIABLE",
                message=(
                    f"'{rule.field}' 는 시간 경과만으로 충족되지 않습니다. "
                    "잘못된 충족 예상일이 사용자에게 표시됩니다"
                ),
            )
        )

    return out


def _validate_op_value(rule: Rule, path: str) -> list[SchemaViolation]:
    op, value = rule.op, rule.value
    p = f"{path}.value"

    if op == "between":
        if not isinstance(value, list) or len(value) != 2:
            return [
                SchemaViolation(
                    path=p,
                    code="BAD_VALUE_SHAPE",
                    message="between 은 [하한, 상한] 2원소 배열이어야 합니다",
                )
            ]
        lo, hi = value
        if isinstance(lo, (int, float)) and isinstance(hi, (int, float)) and lo > hi:
            return [
                SchemaViolation(
                    path=p,
                    code="RANGE_INVERTED",
                    message=f"하한({lo})이 상한({hi})보다 큽니다",
                )
            ]
        return []

    if op in _ARRAY_OPS:
        if not isinstance(value, list):
            return [
                SchemaViolation(
                    path=p,
                    code="BAD_VALUE_SHAPE",
                    message=f"{op} 는 배열 value 가 필요합니다",
                )
            ]
        if not value:
            return [
                SchemaViolation(
                    path=p,
                    code="EMPTY_VALUE_LIST",
                    message=f"{op} 의 배열이 비어 있으면 룰이 무의미합니다",
                )
            ]
        return []

    if op in _SCALAR_OPS and isinstance(value, list):
        return [
            SchemaViolation(
                path=p,
                code="BAD_VALUE_SHAPE",
                message=f"{op} 는 스칼라 value 가 필요합니다",
            )
        ]

    # 부등호 비교는 숫자에만 의미가 있다
    if op in {">", ">=", "<", "<="} and not isinstance(value, (int, float)):
        return [
            SchemaViolation(
                path=p,
                code="BAD_VALUE_TYPE",
                message=f"{op} 는 숫자 value 가 필요합니다 (받은 값: {type(value).__name__})",
            )
        ]

    return []


def validate_judgement(result: JudgementResult) -> list[SchemaViolation]:
    """판정 결과가 신뢰도·근거 고지 규칙을 지키는지 검사한다 (US-06)."""
    out: list[SchemaViolation] = []

    # ESTIMATED / NEEDS_REVIEW 는 담당부서 연락처 없이 사용자에게 보여선 안 된다
    if result.needs_dept_contact and not result.dept_tel:
        out.append(
            SchemaViolation(
                path="dept_tel",
                code="MISSING_DEPT_CONTACT",
                message=(
                    f"confidence={result.confidence} 판정에는 "
                    "담당부서 연락처가 병기되어야 합니다 (PRD §7.6)"
                ),
            )
        )

    # 원문 링크는 모든 경우에 제공한다 (예외 없음)
    if not result.origin_url:
        out.append(
            SchemaViolation(
                path="origin_url",
                code="MISSING_ORIGIN_URL",
                message="판정 근거 원문 링크는 예외 없이 제공되어야 합니다",
            )
        )

    for i, u in enumerate(result.unmatched):
        if not u.source_quote.strip():
            out.append(
                SchemaViolation(
                    path=f"unmatched[{i}].source_quote",
                    code="MISSING_SOURCE_QUOTE",
                    message="미충족 사유에는 공고문 원문 구절이 필요합니다",
                )
            )
        # 충족 예상일을 약속해놓고 날짜를 못 주면 US-02 수용기준 미달
        if u.time_satisfiable and not (
            u.satisfiable_from or u.permanently_unsatisfiable
        ):
            out.append(
                SchemaViolation(
                    path=f"unmatched[{i}].satisfiable_from",
                    code="MISSING_SATISFIABLE_DATE",
                    message="시간 경과로 충족 가능한 조건은 충족 예상일이 필요합니다",
                )
            )

    # NEEDS_INFO 인데 물어볼 게 없으면 사용자가 빠져나갈 길이 없다
    if result.verdict == "NEEDS_INFO" and not result.unknown:
        out.append(
            SchemaViolation(
                path="unknown",
                code="NEEDS_INFO_WITHOUT_QUESTION",
                message="NEEDS_INFO 판정에는 미확인 항목이 최소 1개 있어야 합니다",
            )
        )

    return out


def assert_valid_policy(policy: PolicySchema) -> None:
    """위반이 있으면 예외를 던진다. 배치의 DB 저장 직전 훅에서 사용."""
    if violations := validate_policy(policy):
        raise SchemaValidationError(violations)
