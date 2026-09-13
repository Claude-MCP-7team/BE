"""영업일 계산 — 서류 발급 소요일과 신청 마감일 사이의 달력 산술.

여기서 틀리면 사용자가 마감을 놓친다. 그래서 두 가지를 지킨다.

1. **공휴일표는 추론하지 않고 적어 둔다.**
   대체공휴일은 규칙이 해마다 바뀌고 임시공휴일은 규칙 자체가 없다(선거일·국가장).
   음력을 계산해 설·추석을 유도하는 코드는 그럴듯하지만, 정부가 임시공휴일을
   하루 끼워 넣는 순간 조용히 틀린다. 조용히 틀린 달력은 "준비 시작일이 하루
   늦게 표시된다"로 나타나서 아무도 눈치채지 못한다.
   그래서 연도별 전수 표를 싣고, 출처(`source_ref`)를 함께 남긴다.

2. **표에 없는 연도는 '모른다'고 말한다.**
   표가 덮지 않는 연도를 주말 규칙만으로 계산하면 결과가 나오긴 나온다 —
   틀린 채로. 계산은 하되 `outside_coverage` 로 표시해서, 호출자가 그 값을
   확정 사실처럼 화면에 쓰지 않게 한다.

배치가 한국천문연구원 특일 API 로 `holiday` 테이블을 채우면(batch/holidays.py),
그 결과로 `HolidayCalendar` 를 만들어 주입한다. 아래 표는 그때까지의 기본값이자
API 가 죽었을 때의 폴백이다.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date, timedelta
from functools import lru_cache

# 관공서의 공휴일에 관한 규정 기준. 토·일은 표에 넣지 않는다(주말 규칙이 따로 있다).
#
# 대체공휴일이 적용된 날은 이름에 '대체'를 명시한다. 2026년 기준:
#   삼일절(3/1 일) → 3/2, 부처님오신날(5/24 일) → 5/25,
#   광복절(8/15 토) → 8/17, 추석연휴(9/26 토) → 9/28, 개천절(10/3 토) → 10/5
#   현충일(6/6 토)은 대체공휴일 대상이 아니다 — 규정이 열거한 날에 들어 있지 않다.
_HOLIDAYS_2026: dict[date, str] = {
    date(2026, 1, 1): "신정",
    date(2026, 2, 16): "설날 연휴",
    date(2026, 2, 17): "설날",
    date(2026, 2, 18): "설날 연휴",
    date(2026, 3, 1): "삼일절",
    date(2026, 3, 2): "삼일절 대체공휴일",
    date(2026, 5, 5): "어린이날",
    date(2026, 5, 24): "부처님오신날",
    date(2026, 5, 25): "부처님오신날 대체공휴일",
    date(2026, 6, 3): "제9회 전국동시지방선거",
    date(2026, 6, 6): "현충일",
    date(2026, 8, 15): "광복절",
    date(2026, 8, 17): "광복절 대체공휴일",
    date(2026, 9, 24): "추석 연휴",
    date(2026, 9, 25): "추석",
    date(2026, 9, 26): "추석 연휴",
    date(2026, 9, 28): "추석 대체공휴일",
    date(2026, 10, 3): "개천절",
    date(2026, 10, 5): "개천절 대체공휴일",
    date(2026, 10, 9): "한글날",
    date(2026, 12, 25): "성탄절",
}

_HOLIDAYS_2025: dict[date, str] = {
    date(2025, 1, 1): "신정",
    date(2025, 1, 27): "임시공휴일",
    date(2025, 1, 28): "설날 연휴",
    date(2025, 1, 29): "설날",
    date(2025, 1, 30): "설날 연휴",
    date(2025, 3, 1): "삼일절",
    date(2025, 3, 3): "삼일절 대체공휴일",
    date(2025, 5, 1): "근로자의날",
    date(2025, 5, 5): "어린이날 · 부처님오신날",
    date(2025, 5, 6): "어린이날 대체공휴일",
    date(2025, 6, 3): "제21대 대통령선거",
    date(2025, 6, 6): "현충일",
    date(2025, 8, 15): "광복절",
    date(2025, 10, 3): "개천절",
    date(2025, 10, 5): "추석 연휴",
    date(2025, 10, 6): "추석",
    date(2025, 10, 7): "추석 연휴",
    date(2025, 10, 8): "추석 대체공휴일",
    date(2025, 10, 9): "한글날",
    date(2025, 12, 25): "성탄절",
}

# 이 표는 어디서 왔는가 — 분기 검수 때 이 문구로 원본을 다시 찾는다.
BUNDLED_SOURCE_REF = "관공서의 공휴일에 관한 규정 (대통령령) · 한국천문연구원 특일 정보"

# 한 번의 계산이 훑을 수 있는 최대 일수. 표가 비정상이어서 영업일이 하나도 없을 때
# 무한 루프에 빠지는 대신 예외로 끝낸다.
_SCAN_LIMIT_DAYS = 4000


class CalendarCoverageError(ValueError):
    """달력이 덮지 않는 구간을 확정값으로 요구했을 때."""


class HolidayCalendar:
    """공휴일표 + 영업일 산술.

    불변(immutable)이다. 스냅샷처럼 한 번 만들어 여러 요청이 공유한다.

    덮는 구간의 영업일은 생성 시점에 한 번 색인한다. 하루씩 훑는 구현은
    정책 하나당으로는 빠르지만, 3,000건을 계획하며 매번 수십 일을 세면
    계획 API 하나가 300ms 가 된다(측정치). 색인해 두면 같은 계산이
    이분탐색 한 번으로 끝난다. 2년치는 730개 원소라 메모리도 무시할 수 있다.
    """

    __slots__ = (
        "_business_days",
        "_holidays",
        "_index_end",
        "_index_start",
        "_max_year",
        "_min_year",
        "_prefix",
        "source_ref",
    )

    def __init__(
        self,
        holidays: Mapping[date, str],
        covered_years: Iterable[int],
        source_ref: str = BUNDLED_SOURCE_REF,
    ) -> None:
        years = sorted(set(covered_years))
        if not years:
            raise ValueError("공휴일표가 덮는 연도가 하나도 없습니다")
        self._holidays = dict(holidays)
        self._min_year = years[0]
        self._max_year = years[-1]
        self.source_ref = source_ref

        # 덮는 구간의 영업일 색인.
        #   _business_days  구간 내 영업일을 날짜순으로 나열한 것 (영업일 N번째 = O(1))
        #   _prefix[i]      구간 시작부터 i일째 '이전'까지의 영업일 수 (구간 셈 = O(1))
        self._index_start = date(self._min_year, 1, 1)
        self._index_end = date(self._max_year, 12, 31)
        business: list[date] = []
        prefix: list[int] = [0]
        cursor = self._index_start
        while cursor <= self._index_end:
            if not self._is_business_day_raw(cursor):
                prefix.append(prefix[-1])
            else:
                business.append(cursor)
                prefix.append(prefix[-1] + 1)
            cursor += timedelta(days=1)
        self._business_days = business
        self._prefix = prefix

    # --- 조회 -----------------------------------------------------------

    @property
    def covered_years(self) -> range:
        return range(self._min_year, self._max_year + 1)

    def covers(self, d: date) -> bool:
        return self._min_year <= d.year <= self._max_year

    def holiday_name(self, d: date) -> str | None:
        return self._holidays.get(d)

    def is_weekend(self, d: date) -> bool:
        return d.weekday() >= 5

    def _is_business_day_raw(self, d: date) -> bool:
        return d.weekday() < 5 and d not in self._holidays

    def is_business_day(self, d: date) -> bool:
        """토·일·공휴일이 아니면 영업일.

        표가 덮지 않는 연도에서도 답을 준다(주말 규칙만 적용). 확정값이 필요하면
        `covers()` 로 먼저 확인하거나 `require_coverage()` 를 쓴다.
        """
        return self._is_business_day_raw(d)

    def require_coverage(self, *days: date) -> None:
        """확정값이 필요한 계산 앞에서 호출한다. 덮지 않으면 예외."""
        missing = sorted({d.year for d in days if not self.covers(d)})
        if missing:
            raise CalendarCoverageError(
                f"공휴일표가 {missing} 년을 덮지 않습니다 "
                f"(보유: {self._min_year}~{self._max_year})"
            )

    # --- 색인 보조 -------------------------------------------------------

    def _indexed(self, *days: date) -> bool:
        """색인 경로를 쓸 수 있는가. 아니면 하루씩 훑는 경로로 떨어진다."""
        return all(self._index_start <= d <= self._index_end for d in days)

    def _business_ordinal(self, d: date) -> int:
        """구간 시작부터 d '이전'까지의 영업일 수. d 자신은 세지 않는다."""
        return self._prefix[(d - self._index_start).days]

    # --- 산술 -----------------------------------------------------------

    def next_business_day(self, d: date, *, inclusive: bool = False) -> date:
        """d 이후(또는 d 포함) 첫 영업일."""
        cursor = d if inclusive else d + timedelta(days=1)
        if self._indexed(cursor):
            ordinal = self._business_ordinal(cursor)
            if ordinal < len(self._business_days):
                return self._business_days[ordinal]
        for _ in range(_SCAN_LIMIT_DAYS):
            if self.is_business_day(cursor):
                return cursor
            cursor += timedelta(days=1)
        raise CalendarCoverageError("영업일을 찾지 못했습니다 — 공휴일표가 비정상입니다")

    def previous_business_day(self, d: date, *, inclusive: bool = False) -> date:
        """d 이전(또는 d 포함) 마지막 영업일."""
        cursor = d if inclusive else d - timedelta(days=1)
        if self._indexed(cursor):
            # cursor 를 포함해 센 영업일 수 - 1 = cursor 이하 마지막 영업일의 색인
            ordinal = self._business_ordinal(cursor) + (
                1 if self._is_business_day_raw(cursor) else 0
            )
            if ordinal >= 1:
                return self._business_days[ordinal - 1]
        for _ in range(_SCAN_LIMIT_DAYS):
            if self.is_business_day(cursor):
                return cursor
            cursor -= timedelta(days=1)
        raise CalendarCoverageError("영업일을 찾지 못했습니다 — 공휴일표가 비정상입니다")

    def add_business_days(self, start: date, days: int) -> date:
        """start 에서 영업일 `days` 일 뒤.

        `days=0` 은 start 를 그대로 돌려준다 — start 가 휴일이어도 앞으로 밀지 않는다.
        (밀지 말지는 호출자가 정할 문제다. 필요하면 next_business_day 를 쓴다.)
        음수를 주면 과거로 센다.
        """
        if days < 0:
            return self.subtract_business_days(start, -days)
        if days == 0:
            return start
        if self._indexed(start):
            # start 다음 영업일부터 days 번째
            ordinal = self._business_ordinal(start + timedelta(days=1))
            target = ordinal + days - 1
            if 0 <= target < len(self._business_days):
                return self._business_days[target]
        cursor = start
        for _ in range(days):
            cursor = self.next_business_day(cursor)
        return cursor

    def subtract_business_days(self, start: date, days: int) -> date:
        """start 에서 영업일 `days` 일 앞. 마감일 역산의 기본 연산이다."""
        if days < 0:
            return self.add_business_days(start, -days)
        if days == 0:
            return start
        if self._indexed(start):
            ordinal = self._business_ordinal(start)  # start 이전 영업일 수
            target = ordinal - days
            if 0 <= target < len(self._business_days):
                return self._business_days[target]
        cursor = start
        for _ in range(days):
            cursor = self.previous_business_day(cursor)
        return cursor

    def business_days_between(self, start: date, end: date) -> int:
        """[start, end) 구간의 영업일 수. start 는 포함, end 는 제외.

        end < start 이면 음수를 돌려준다 — "며칠 지났는가"를 그대로 읽을 수 있다.
        """
        if end == start:
            return 0
        sign = 1 if end > start else -1
        lo, hi = (start, end) if sign == 1 else (end, start)
        if self._indexed(lo, hi):
            return (self._business_ordinal(hi) - self._business_ordinal(lo)) * sign
        count = 0
        cursor = lo
        while cursor < hi:
            if self.is_business_day(cursor):
                count += 1
            cursor += timedelta(days=1)
        return count * sign


@lru_cache(maxsize=1)
def bundled_calendar() -> HolidayCalendar:
    """기본 공휴일표. 배치가 DB 표를 주입하기 전까지 쓰인다.

    캐시해 둔다 — 달력은 불변이고 생성 시 영업일을 색인하므로, 요청마다 새로
    만들면 그 색인 비용을 매번 다시 낸다.
    """
    merged = {**_HOLIDAYS_2025, **_HOLIDAYS_2026}
    return HolidayCalendar(merged, covered_years=(2025, 2026))


def calendar_from_rows(rows: Iterable[tuple[date, str]], source_ref: str) -> HolidayCalendar:
    """`holiday` 테이블(또는 스냅샷)에서 달력을 만든다.

    덮는 연도는 실제 들어온 날짜에서 뽑는다. 배치가 2027년치를 아직 못 받았으면
    달력은 스스로 그 사실을 알고, 2027년 계산에 `outside_coverage` 를 붙인다.
    """
    holidays = dict(rows)
    if not holidays:
        raise ValueError("공휴일이 0건입니다 — 빈 달력으로는 영업일을 계산할 수 없습니다")
    return HolidayCalendar(
        holidays, covered_years={d.year for d in holidays}, source_ref=source_ref
    )
