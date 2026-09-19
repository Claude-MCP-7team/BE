"""벡터화 판정 (B 레이어).

그룹 하나당 numpy 연산 한 번으로 전체 정책을 동시에 평가한다.
결과 누적은 ufunc.at 대신 bincount 를 쓴다 — ufunc.at 은 파이썬 루프에 가까워
정책 수가 늘면 벡터화의 이점이 사라진다.

판정 우선순위 (PRD §6.4):
  미충족이 하나라도 있으면  INELIGIBLE  ← '왜 안 되는지' 말해줄 수 있으므로 우선
  아니고 미확인이 있으면     NEEDS_INFO  ← 물어보면 해결된다
  둘 다 없으면               ELIGIBLE
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import numpy as np

from app.engine.compile import (
    BetweenGroup,
    CmpGroup,
    ExistsGroup,
    MemberGroup,
    Snapshot,
)
from app.engine.rules import Outcome, evaluate_rule
from app.engine.timeline import satisfiable_from
from app.schemas.enums import TIME_SATISFIABLE_FIELDS
from app.schemas.judgement import (
    JsonValue,
    JudgementResult,
    JudgementSummary,
    MatchedRule,
    UnknownRule,
    UnmatchedRule,
)
from app.schemas.policy import Rule
from app.schemas.user import UserProfile


@dataclass(slots=True)
class Verdicts:
    """정책별 판정 마스크. 상세 조립 전의 1차 결과."""

    eligible: np.ndarray  # bool[n]
    ineligible: np.ndarray
    needs_info: np.ndarray

    def summary(self) -> JudgementSummary:
        return JudgementSummary(
            eligible=int(self.eligible.sum()),
            ineligible=int(self.ineligible.sum()),
            needs_info=int(self.needs_info.sum()),
        )


def judge_all(snapshot: Snapshot, profile: UserProfile, today: date) -> Verdicts:
    """전 정책을 한 번에 판정한다. DB 접근 없음."""
    n = snapshot.size
    fail = np.zeros(n, dtype=np.int32)
    unknown = np.zeros(n, dtype=np.int32)

    # 필드값은 그룹마다 다시 꺼내지 않고 한 번만 계산한다
    values = {f: profile.resolve(f, today) for f in snapshot.fields_used()}

    for cg in snapshot.cmp_groups:
        _accumulate(fail, unknown, n, cg.policy_idx, _eval_cmp(cg, values[cg.field]))
    for bg in snapshot.between_groups:
        _accumulate(fail, unknown, n, bg.policy_idx, _eval_between(bg, values[bg.field]))
    for mg in snapshot.member_groups:
        _accumulate(fail, unknown, n, mg.policy_idx, _eval_member(mg, values[mg.field]))
    for eg in snapshot.exists_groups:
        _accumulate(fail, unknown, n, eg.policy_idx, _eval_exists(eg, values[eg.field]))

    ineligible = fail > 0
    needs_info = ~ineligible & (unknown > 0)
    return Verdicts(
        eligible=~ineligible & ~needs_info,
        ineligible=ineligible,
        needs_info=needs_info,
    )


def _accumulate(
    fail: np.ndarray,
    unknown: np.ndarray,
    n: int,
    policy_idx: np.ndarray,
    result: tuple[np.ndarray, np.ndarray],
) -> None:
    """그룹 결과를 정책 단위로 합산한다."""
    failed, unresolved = result
    if failed.any():
        fail += np.bincount(policy_idx[failed], minlength=n).astype(np.int32)
    if unresolved.any():
        unknown += np.bincount(policy_idx[unresolved], minlength=n).astype(np.int32)


def _unknown_all(k: int) -> tuple[np.ndarray, np.ndarray]:
    return np.zeros(k, dtype=bool), np.ones(k, dtype=bool)


def _eval_cmp(group: CmpGroup, user_value: object) -> tuple[np.ndarray, np.ndarray]:
    k = group.threshold.size
    if user_value is None:
        return _unknown_all(k)
    v = float(user_value)  # type: ignore[arg-type]
    t = group.threshold
    if group.op == ">":
        passed = v > t
    elif group.op == ">=":
        passed = v >= t
    elif group.op == "<":
        passed = v < t
    else:
        passed = v <= t
    return ~passed, np.zeros(k, dtype=bool)


def _eval_between(group: BetweenGroup, user_value: object) -> tuple[np.ndarray, np.ndarray]:
    k = group.lo.size
    if user_value is None:
        return _unknown_all(k)
    v = float(user_value)  # type: ignore[arg-type]
    passed = (group.lo <= v) & (v <= group.hi)
    return ~passed, np.zeros(k, dtype=bool)


def _eval_member(group: MemberGroup, user_value: object) -> tuple[np.ndarray, np.ndarray]:
    k = group.policy_idx.size
    if user_value is None:
        return _unknown_all(k)

    # 사용자 값이 목록이면 그 원소 전부가 교집합 후보다
    user_values = (
        list(user_value) if isinstance(user_value, (list, tuple, set)) else [user_value]
    )

    hit = np.zeros(k, dtype=bool)
    for value in user_values:
        arr = group.member.get(value)
        if arr is not None:
            hit |= arr

    passed = ~hit if group.negate else hit
    return ~passed, np.zeros(k, dtype=bool)


def _eval_exists(group: ExistsGroup, user_value: object) -> tuple[np.ndarray, np.ndarray]:
    # exists 는 값의 유무 자체를 묻기 때문에 None 이어도 미확인이 아니다
    has = user_value is not None and user_value != ""
    passed = group.want == has
    return ~passed, np.zeros(group.want.size, dtype=bool)


# --- 상세 조립 (설명 · 근거 인용 · 충족 예상일) -------------------------------


def explain(
    snapshot: Snapshot,
    profile: UserProfile,
    today: date,
    policy_index: int,
    verdict: str,
) -> JudgementResult:
    """정책 1건의 판정 근거를 조립한다.

    여기서는 벡터화 경로가 아니라 rules.py 의 기준 구현을 쓴다.
    상세는 한 번에 한 정책만 만들면 되므로 속도가 문제되지 않고,
    사용자에게 보이는 문장은 의미의 원본과 붙어 있는 편이 안전하다.
    """
    policy = snapshot.policies[policy_index]
    origin = policy.source.origin_url or policy.source.announcement_url

    # 엔진이 룰로 옮기지 못한 조건 (무주택·세대주·보증금 등). A2 단계가
    # unrepresentable_conditions 로 받아 여기에 남긴다.
    review_fields = list(policy.quality.needs_review_fields)

    matched: list[MatchedRule] = []
    unmatched: list[UnmatchedRule] = []
    unknown: list[UnknownRule] = []
    worst_confidence = "CONFIRMED"

    for ref_index in snapshot.rules_by_policy[policy_index]:
        rule = snapshot.rule_refs[ref_index].rule
        user_value = profile.resolve(rule.field, today)
        outcome = evaluate_rule(rule, user_value)

        if outcome is Outcome.PASS:
            matched.append(
                MatchedRule(
                    rule_id=rule.rule_id,
                    field=rule.field,
                    user_value=_as_json(user_value),
                    source_quote=rule.source_quote,
                    source_url=rule.source_url or origin,
                )
            )
            continue

        worst_confidence = _worse(worst_confidence, rule.confidence)

        if outcome is Outcome.UNKNOWN:
            unknown.append(
                UnknownRule(
                    rule_id=rule.rule_id,
                    field=rule.field,
                    source_quote=rule.source_quote,
                    question_template=rule.question_template,
                    source_url=rule.source_url or origin,
                )
            )
            continue

        unmatched.append(_unmatched(rule, user_value, profile, today, origin))

    future_from = future_eligible_date(snapshot, profile, policy_index, unmatched, unknown)

    # 평가하지 못한 조건이 남아 있는데 '적격'이라고 확정하면, 사용자는 그 조건
    # 때문에 반려될 수 있다는 걸 모른 채 서류를 준비한다. 근거 없는 조건은 자동
    # 확정하지 않는다는 규칙(마일스톤 §11)이 여기에 걸린다.
    #
    # 단순 부적격에는 적용하지 않는다. 조건은 전부 충족해야 하는 관계라, 확인
    # 못 한 조건이 더 있다고 해서 이미 확인된 미충족이 뒤집히지 않는다. 명확한
    # 탈락에 '확인 필요'를 붙이면 진짜 확인이 필요한 판정과 구별이 사라진다.
    #
    # 다만 충족 예상일을 함께 내보낼 때는 다시 적용한다. '오늘 안 된다'와
    # '그날 된다'는 다른 주장이고, 뒤쪽은 확인 못 한 조건이 얼마든지 뒤집는다.
    if review_fields and (verdict != "INELIGIBLE" or future_from is not None):
        worst_confidence = _worse(worst_confidence, "NEEDS_REVIEW")

    return JudgementResult(
        policy_id=policy.policy_id,
        verdict=verdict,  # type: ignore[arg-type]
        confidence=worst_confidence,  # type: ignore[arg-type]
        future_eligible_from=future_from,
        needs_review_fields=review_fields,
        matched=matched,
        unmatched=unmatched,
        unknown=unknown,
        dept_name=policy.meta.dept.name,
        dept_tel=policy.meta.dept.tel,
        origin_url=origin,
    )


def future_eligible_date(
    snapshot: Snapshot,
    profile: UserProfile,
    policy_index: int,
    unmatched: list[UnmatchedRule],
    unknown: list[UnknownRule],
) -> str | None:
    """미충족이 전부 시간으로 해결될 때, 정책 전체가 충족되는 날 (YYYY-MM-DD).

    가장 늦은 조건이 전체를 결정한다. 19세 하한과 거주 36개월을 함께 요구하면
    둘 다 만족하는 날부터 신청할 수 있고, 이른 쪽 날짜를 주면 그날 신청했다가
    반려된다.

    하나라도 다음에 해당하면 None 이다. 날짜를 주는 것이 '기다리면 된다'는
    약속이기 때문에, 약속할 수 없는 경우를 날짜로 덮으면 안 된다:
      - 시간과 무관한 조건 (소득·가구원수 등)
      - 영구 불가 (연령 상한 초과)
      - 미확인 조건이 남아 있음 — 그날 적격이 될지 알 수 없다

    **그날 다른 조건이 깨지는 경우도 None 이다.** 기다리면 충족되는 조건과
    기다리면 깨지는 조건이 한 정책에 함께 있을 수 있다 — 24세 사용자에게
    거주 36개월을 요구하면서 연령 상한이 25세인 정책은, 거주 요건을 채우는
    날 이미 나이가 넘는다. 미충족 조건만 보면 그 날짜가 나오고, 사용자는
    3년을 기다렸다가 반려된다.

    확인은 날짜 산술을 다시 짜지 않고 **그 날짜로 전 조건을 다시 평가**해서
    한다. 계산식을 복제하면 한쪽만 고쳐질 때 조용히 어긋난다.
    """
    if unknown or not unmatched:
        return None

    dates: list[str] = []
    for unmet in unmatched:
        if unmet.permanently_unsatisfiable or not unmet.time_satisfiable:
            return None
        if not unmet.satisfiable_from:
            return None
        dates.append(unmet.satisfiable_from)

    # ISO 날짜는 사전순 비교가 시간순과 일치한다
    candidate = max(dates)
    when = date.fromisoformat(candidate)

    for ref_index in snapshot.rules_by_policy[policy_index]:
        rule = snapshot.rule_refs[ref_index].rule
        if evaluate_rule(rule, profile.resolve(rule.field, when)) is not Outcome.PASS:
            return None

    return candidate


def _unmatched(
    rule: Rule,
    user_value: object,
    profile: UserProfile,
    today: date,
    origin: str | None,
) -> UnmatchedRule:
    # 충족 예상일 여부는 rule.time_satisfiable 플래그가 아니라 '필드' 로 판단한다.
    # 플래그는 AI 에이전트가 채우는 값이라, 빠뜨리면 차별점인 "언제부터 가능한가"가
    # 조용히 사라진다. 시간으로 충족되는 필드인지는 결정론적으로 알 수 있다.
    time_satisfiable = rule.field in TIME_SATISFIABLE_FIELDS
    lo, hi = _bounds(rule)
    sat = (
        satisfiable_from(rule.field, profile, today, minimum=lo, maximum=hi)
        if time_satisfiable
        else None
    )
    return UnmatchedRule(
        rule_id=rule.rule_id,
        field=rule.field,
        user_value=_as_json(user_value),
        required=_as_json(rule.value),
        unit=rule.unit,
        source_quote=rule.source_quote,
        source_url=rule.source_url or origin,
        time_satisfiable=time_satisfiable,
        satisfiable_from=sat.date.isoformat() if sat and sat.date else None,
        permanently_unsatisfiable=bool(sat and sat.permanently_unsatisfiable),
    )


def _bounds(rule: Rule) -> tuple[int | None, int | None]:
    """룰을 [하한, 상한] 으로 환원한다. 충족 예상일 계산의 입력이 된다."""
    op, value = rule.op, rule.value
    if op == "between" and isinstance(value, list) and len(value) == 2:
        return int(value[0]), int(value[1])
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None, None
    if op == ">=":
        return int(value), None
    if op == ">":
        return int(value) + 1, None
    if op == "<=":
        return None, int(value)
    if op == "<":
        return None, int(value) - 1
    if op == "==":
        return int(value), int(value)
    return None, None


_CONFIDENCE_ORDER = {"CONFIRMED": 0, "ESTIMATED": 1, "NEEDS_REVIEW": 2}


def _worse(a: str, b: str) -> str:
    """판정 신뢰도는 가장 약한 근거를 따른다."""
    return a if _CONFIDENCE_ORDER[a] >= _CONFIDENCE_ORDER[b] else b


def _as_json(value: object) -> JsonValue:
    """JudgementResult 가 담을 수 있는 형태로 좁힌다."""
    if isinstance(value, (list, tuple, set)):
        return [v for v in value if isinstance(v, (bool, int, float, str))]
    if isinstance(value, (bool, int, float, str)) or value is None:
        return value
    return str(value)
