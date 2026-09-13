"""룰 평가의 의미 정의 — 이 파일이 '무엇이 충족인가'의 유일한 기준이다.

실제 판정은 evaluate.py 의 벡터화 경로가 수행한다. 그런데 최적화된 코드에
의미를 직접 써 넣으면, 성능을 손볼 때마다 판정 결과가 조용히 바뀔 수 있다.
그래서 느리지만 읽기 쉬운 기준 구현을 여기 두고, 벡터화 경로가 이것과
일치하는지 테스트로 대조한다 (tests/unit/test_engine.py).

G2 게이트(정확도 92%)를 지키려면 '빠른 경로와 느린 경로가 같은 답을 낸다'가
먼저 보장되어야 한다.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from app.schemas.policy import Rule


class Outcome(StrEnum):
    PASS = "PASS"  # 조건 충족
    FAIL = "FAIL"  # 조건 미충족 — 이유를 사용자에게 보여준다
    UNKNOWN = "UNKNOWN"  # 판단에 필요한 값이 없다 — 역질문 대상


class RuleEvaluationError(TypeError):
    """룰과 사용자 값의 타입이 맞지 않는다. 컴파일 단계에서 걸러져야 한다."""


_NUMERIC_OPS = {">", ">=", "<", "<=", "between"}


def evaluate_rule(rule: Rule, user_value: Any) -> Outcome:
    """룰 1건을 평가한다.

    eligibility 와 exclusions 를 구분하지 않는다. 공고문의 제외 조항도
    PolicySchema 에서는 '충족해야 할 조건' 형태로 표현되기 때문이다.
    (예: "타 유사사업 참여자는 제외" → similar_program_participation_2y == false)
    구분은 설명을 만들 때만 쓰인다.
    """
    op = rule.op

    # exists 는 '값의 유무'를 묻는 유일한 연산자라 None 을 미확인으로 보지 않는다
    if op == "exists":
        want = bool(rule.value)
        has = user_value is not None and user_value != ""
        return Outcome.PASS if has == want else Outcome.FAIL

    if user_value is None:
        return Outcome.UNKNOWN

    if op in _NUMERIC_OPS:
        return _numeric(op, rule.value, user_value, rule.rule_id)
    if op in ("==", "!="):
        equal = user_value == rule.value
        return _pass_if(equal if op == "==" else not equal)
    if op in ("in", "not_in"):
        return _membership(op, rule.value, user_value, rule.rule_id)
    if op == "contains":
        if not isinstance(user_value, (list, tuple, set)):
            raise RuleEvaluationError(
                f"[{rule.rule_id}] contains 는 사용자 값이 목록이어야 합니다: {user_value!r}"
            )
        return _pass_if(rule.value in user_value)

    raise RuleEvaluationError(f"[{rule.rule_id}] 알 수 없는 연산자: {op}")


def _numeric(op: str, expected: Any, user_value: Any, rule_id: str) -> Outcome:
    if isinstance(user_value, bool) or not isinstance(user_value, (int, float)):
        raise RuleEvaluationError(
            f"[{rule_id}] {op} 는 숫자 비교인데 사용자 값이 {type(user_value).__name__} 입니다"
        )

    if op == "between":
        if not isinstance(expected, list) or len(expected) != 2:
            raise RuleEvaluationError(f"[{rule_id}] between 은 [하한, 상한] 이어야 합니다")
        lo, hi = expected
        return _pass_if(lo <= user_value <= hi)

    if not isinstance(expected, (int, float)) or isinstance(expected, bool):
        raise RuleEvaluationError(f"[{rule_id}] {op} 의 기준값이 숫자가 아닙니다: {expected!r}")

    if op == ">":
        return _pass_if(user_value > expected)
    if op == ">=":
        return _pass_if(user_value >= expected)
    if op == "<":
        return _pass_if(user_value < expected)
    return _pass_if(user_value <= expected)


def _membership(op: str, expected: Any, user_value: Any, rule_id: str) -> Outcome:
    if not isinstance(expected, list):
        raise RuleEvaluationError(f"[{rule_id}] {op} 의 기준값은 목록이어야 합니다")

    # 사용자 값이 목록이면 교집합 여부로 본다.
    # (기수혜 이력처럼 '내가 받은 것 중 하나라도 해당되면' 이라는 조건이 있다)
    if isinstance(user_value, (list, tuple, set)):
        hit = bool(set(user_value) & set(expected))
    else:
        hit = user_value in expected

    return _pass_if(hit if op == "in" else not hit)


def _pass_if(condition: bool) -> Outcome:
    return Outcome.PASS if condition else Outcome.FAIL


def all_rules(policy_eligibility: list[Rule], policy_exclusions: list[Rule]) -> list[Rule]:
    """두 목록을 하나로 본다. 평가 의미가 같으므로 분리해 다룰 이유가 없다."""
    return [*policy_eligibility, *policy_exclusions]
