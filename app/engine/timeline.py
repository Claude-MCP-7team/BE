"""충족 예상일 계산 (US-02) — "언제부터 신청 가능한가".

⚠️ 여기서 달력 산술을 따로 구현하지 않는다.
   UserProfile.age() / residence_months() 가 '지금 몇 개월인가'를 세는 방식과
   조금이라도 다르게 계산하면, 화면에는 "11월 15일부터 가능"이라 적혀 있는데
   그날 다시 판정하면 여전히 부적격인 상황이 생긴다.

   그래서 계산식을 복제하는 대신, **세는 함수의 역함수**로 정의한다:
   "그 함수가 N 이상을 돌려주는 첫 날짜"를 찾는다. 정의상 어긋날 수 없다.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, timedelta

from app.schemas.enums import TIME_SATISFIABLE_FIELDS
from app.schemas.user import UserProfile

# 이 기간을 넘어서야 충족되는 조건은 사용자에게 의미가 없다 (정책이 먼저 사라진다)
_HORIZON_YEARS = 10
_HORIZON_DAYS = 365 * _HORIZON_YEARS


class Satisfiability:
    """계산 결과. 날짜가 없다는 것과 영원히 안 된다는 것은 다르다."""

    __slots__ = ("date", "permanently_unsatisfiable", "reason")

    def __init__(
        self,
        date: date | None = None,
        permanently_unsatisfiable: bool = False,
        reason: str = "",
    ) -> None:
        self.date = date
        self.permanently_unsatisfiable = permanently_unsatisfiable
        self.reason = reason

    def __repr__(self) -> str:  # pragma: no cover - 디버깅 편의
        if self.permanently_unsatisfiable:
            return f"Satisfiability(영구불가: {self.reason})"
        return f"Satisfiability({self.date or '계산불가: ' + self.reason})"


def satisfiable_from(
    field: str,
    profile: UserProfile,
    today: date,
    minimum: int | None = None,
    maximum: int | None = None,
) -> Satisfiability:
    """`field` 가 [minimum, maximum] 범위에 들어가는 가장 이른 날짜를 찾는다.

    minimum 미달은 기다리면 해결되고, maximum 초과는 영원히 해결되지 않는다.
    후자에 날짜를 붙이면 거짓말이 되므로 명시적으로 구분한다.
    """
    if field not in TIME_SATISFIABLE_FIELDS:
        return Satisfiability(reason="시간 경과로 충족되는 조건이 아닙니다")

    counter = _counter_for(field, profile)
    if counter is None:
        return Satisfiability(reason="기준 날짜가 입력되지 않았습니다")

    current = counter(today)
    if current is None:
        return Satisfiability(reason="현재 값을 계산할 수 없습니다")

    # 상한을 이미 넘었다면 값은 앞으로 더 커지기만 한다
    if maximum is not None and current > maximum:
        return Satisfiability(
            permanently_unsatisfiable=True,
            reason=f"이미 상한({maximum})을 초과했으며 시간이 지나도 되돌아가지 않습니다",
        )

    if minimum is None or current >= minimum:
        return Satisfiability(date=today)

    found = _first_day_reaching(counter, minimum, today)
    if found is None:
        return Satisfiability(reason=f"{_HORIZON_YEARS}년 내에 충족되지 않습니다")

    # 기다려서 하한을 넘겼는데 그 사이 상한을 지나쳐 버리는 경우
    if maximum is not None and (counter(found) or 0) > maximum:
        return Satisfiability(
            permanently_unsatisfiable=True,
            reason=f"하한({minimum})에 도달하기 전에 상한({maximum})을 넘습니다",
        )

    return Satisfiability(date=found)


def _counter_for(field: str, profile: UserProfile) -> Callable[[date], int | None] | None:
    """판정에 쓰는 것과 '같은' 세는 함수를 돌려준다.

    기준 날짜가 없으면 None 을 돌려준다 — 역질문으로 "24개월째"라고 답했더라도
    마찬가지다. 그 답변은 `resolve()` 가 현재 판정에 쓰지만 여기서는 쓰지 않는다.

    왜 쓰지 않는가: 개월수는 '오늘 기준 몇 개월'이라 미래 날짜를 만들려면 입사일을
    역산해야 하는데, "24개월"은 [24, 25) 개월 어딘가라서 역산한 날짜가 최대 한 달
    틀린다. 그 날짜는 화면에 "11월 15일부터 가능"으로 나가고, 그날 다시 판정하면
    여전히 부적격일 수 있다 — 이 모듈이 계산식 복제를 금지하면서까지 막으려는
    바로 그 상황이다. 약속할 수 없는 날짜는 주지 않는 편이 낫다.

    그래서 답변만 있는 경우 지금 충족되면 적격이 되고, 모자라면 날짜 없는
    부적격이 된다. 정확한 날짜가 필요하면 개월수가 아니라 시작일을 받아야 한다.
    """
    if field == "age":
        return profile.age
    if field == "residence_months_continuous":
        if profile.core.residence_start_date is None or not profile.core.residence_continuous:
            return None
        return profile.residence_months
    if field == "employment_months":
        if profile.core.employment_start_date is None:
            return None
        return profile.employment_months
    return None


def _first_day_reaching(
    counter: Callable[[date], int | None], target: int, not_before: date
) -> date | None:
    """counter 가 target 이상을 돌려주는 첫 날짜. 없으면 None.

    counter 가 날짜에 대해 단조 증가한다는 것만 가정한다 (나이도 경과 개월도 그렇다).
    '한 단위가 며칠인가'를 추정하지 않으므로, 연 단위든 월 단위든 같은 코드로 동작한다.
    지수 탐색으로 상한을 잡고 이분 탐색으로 첫 날짜를 좁힌다.
    """

    def value_at(d: date) -> int:
        return counter(d) or 0

    if value_at(not_before) >= target:
        return not_before

    # 1) 조건을 만족하는 날짜를 하나 찾을 때까지 간격을 2배씩 늘린다
    step = 1
    while step <= _HORIZON_DAYS:
        if value_at(not_before + timedelta(days=step)) >= target:
            break
        step *= 2
    else:
        return None

    # 2) (lo, hi] 구간에서 경계를 이분 탐색한다
    lo, hi = not_before, not_before + timedelta(days=step)
    while (hi - lo).days > 1:
        mid = lo + timedelta(days=(hi - lo).days // 2)
        if value_at(mid) >= target:
            hi = mid
        else:
            lo = mid

    return hi
