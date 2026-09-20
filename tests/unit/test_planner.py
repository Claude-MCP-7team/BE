"""영업일 계산 · 착수일 역산 · ICS 내보내기 (BE-M5).

G5 게이트: 2026년 공휴일 전수 테스트 100%.
"""

from __future__ import annotations

from datetime import date

import msgspec
import pytest

from app.engine.compile import compile_snapshot
from app.engine.evaluate import judge_all
from app.planner.backplan import (
    DEFAULT_UNKNOWN_LEAD_BUSINESS_DAYS,
    SUBMISSION_BUFFER_BUSINESS_DAYS,
    build_plan,
)
from app.planner.businessday import (
    CalendarCoverageError,
    HolidayCalendar,
    bundled_calendar,
    calendar_from_rows,
)
from app.planner.documents import NAME_ALIASES
from app.planner.documents import master as doc_master
from app.planner.documents import resolve as doc_resolve
from app.planner.ics import to_ics
from app.schemas.plan import PlanResponse
from app.schemas.policy import Benefit, Dept, Document, Meta, Period, PolicySchema, Rule, Source
from app.schemas.user import Core, UserProfile

TODAY = date(2026, 3, 10)  # 화요일, 공휴일 아님


# --- 픽스처 ---------------------------------------------------------------


def _profile() -> UserProfile:
    return UserProfile(core=Core(birth_date=date(2000, 1, 1), region_code="41135"))


def _policy(
    pid: str,
    *,
    title: str | None = None,
    apply_end: str | None = None,
    apply_start: str | None = None,
    is_rolling: bool = False,
    documents: list[Document] | None = None,
    amount: int = 1_000_000,
) -> PolicySchema:
    return PolicySchema(
        policy_id=pid,
        status="published",
        meta=Meta(
            title=title or f"정책 {pid}",
            category="job",
            authority_level="local",
            region_code=["41135"],
            dept=Dept(name="청년정책과", tel="031-000-0000"),
        ),
        source=Source(origin_url=f"https://example.kr/{pid}"),
        benefit=Benefit(type="cash_lump", amount_krw=amount, estimated_total_krw=amount),
        period=Period(apply_start=apply_start, apply_end=apply_end, is_rolling=is_rolling),
        eligibility=[
            Rule(
                rule_id=f"{pid}-r1",
                field="age",
                op=">=",
                value=19,
                source_quote="만 19세 이상",
            )
        ],
        documents=documents or [],
    )


def _plan_of(policies: list[PolicySchema], today: date = TODAY) -> PlanResponse:
    snapshot = compile_snapshot(policies, version="plan-test")
    verdicts = judge_all(snapshot, _profile(), today)
    return build_plan(snapshot, verdicts, today)


def _by_id(plan: PlanResponse, pid: str):
    return next(p for p in plan.plans if p.policy_id == pid)


# --- G5: 2026 공휴일 전수 -------------------------------------------------

# 관공서의 공휴일에 관한 규정 기준 2026년 전체.
# 대체공휴일 포함. 현충일(6/6 토)은 대체 대상이 아니다.
HOLIDAYS_2026 = [
    (date(2026, 1, 1), "신정"),
    (date(2026, 2, 16), "설날 연휴"),
    (date(2026, 2, 17), "설날"),
    (date(2026, 2, 18), "설날 연휴"),
    (date(2026, 3, 1), "삼일절"),
    (date(2026, 3, 2), "삼일절 대체"),
    (date(2026, 5, 5), "어린이날"),
    (date(2026, 5, 24), "부처님오신날"),
    (date(2026, 5, 25), "부처님오신날 대체"),
    (date(2026, 6, 3), "지방선거"),
    (date(2026, 6, 6), "현충일"),
    (date(2026, 8, 15), "광복절"),
    (date(2026, 8, 17), "광복절 대체"),
    (date(2026, 9, 24), "추석 연휴"),
    (date(2026, 9, 25), "추석"),
    (date(2026, 9, 26), "추석 연휴"),
    (date(2026, 9, 28), "추석 대체"),
    (date(2026, 10, 3), "개천절"),
    (date(2026, 10, 5), "개천절 대체"),
    (date(2026, 10, 9), "한글날"),
    (date(2026, 12, 25), "성탄절"),
]


@pytest.mark.parametrize(("day", "name"), HOLIDAYS_2026, ids=lambda v: str(v))
def test_g5_모든_2026_공휴일은_영업일이_아니다(day: date, name: str) -> None:
    cal = bundled_calendar()
    assert not cal.is_business_day(day), f"{name} ({day}) 가 영업일로 계산됩니다"
    assert cal.holiday_name(day) is not None


def test_g5_공휴일표에_빠지거나_더해진_날이_없다() -> None:
    """전수 대조 — 표에 있는 2026년 날짜 집합이 기대 목록과 정확히 같아야 한다.

    개별 날짜 테스트는 '빠진 날'을 못 잡는다. 집합 비교가 그 구멍을 막는다.
    """
    cal = bundled_calendar()
    expected = {d for d, _ in HOLIDAYS_2026}
    actual = {
        d
        for d in _all_days(2026)
        if cal.holiday_name(d) is not None
    }
    assert actual == expected


def test_2026_평일_중_공휴일이_아닌_날은_모두_영업일() -> None:
    cal = bundled_calendar()
    holidays = {d for d, _ in HOLIDAYS_2026}
    for d in _all_days(2026):
        if d.weekday() >= 5 or d in holidays:
            assert not cal.is_business_day(d)
        else:
            assert cal.is_business_day(d), f"{d} 이 영업일이 아니라고 계산됩니다"


def _all_days(year: int) -> list[date]:
    from datetime import timedelta

    days, cursor = [], date(year, 1, 1)
    while cursor.year == year:
        days.append(cursor)
        cursor += timedelta(days=1)
    return days


# --- 영업일 산술 ----------------------------------------------------------


def test_주말은_영업일이_아니다() -> None:
    cal = bundled_calendar()
    assert not cal.is_business_day(date(2026, 3, 7))  # 토
    assert not cal.is_business_day(date(2026, 3, 8))  # 일
    assert cal.is_business_day(date(2026, 3, 9))  # 월


def test_다음_영업일은_연휴를_건너뛴다() -> None:
    cal = bundled_calendar()
    # 2/13(금) 다음 영업일: 2/14~15 주말 + 2/16~18 설연휴 → 2/19(목)
    assert cal.next_business_day(date(2026, 2, 13)) == date(2026, 2, 19)


def test_이전_영업일은_연휴를_거꾸로_건너뛴다() -> None:
    cal = bundled_calendar()
    assert cal.previous_business_day(date(2026, 2, 19)) == date(2026, 2, 13)


def test_inclusive_는_당일이_영업일이면_그대로_돌려준다() -> None:
    cal = bundled_calendar()
    assert cal.next_business_day(date(2026, 3, 10), inclusive=True) == date(2026, 3, 10)
    assert cal.next_business_day(date(2026, 3, 10)) == date(2026, 3, 11)


def test_영업일_더하기와_빼기는_서로의_역이다() -> None:
    cal = bundled_calendar()
    for days in (1, 3, 5, 10, 20):
        forward = cal.add_business_days(date(2026, 2, 10), days)
        assert cal.subtract_business_days(forward, days) == date(2026, 2, 10)


def test_영업일_0일은_그대로() -> None:
    cal = bundled_calendar()
    assert cal.add_business_days(date(2026, 2, 17), 0) == date(2026, 2, 17)  # 휴일이어도


def test_설연휴를_가로지르는_역산() -> None:
    cal = bundled_calendar()
    # 2/20(금)에서 영업일 3일 전 → 2/19(목), 2/13(금), 2/12(목)
    assert cal.subtract_business_days(date(2026, 2, 20), 3) == date(2026, 2, 12)


def test_구간_영업일_수는_시작포함_끝제외() -> None:
    cal = bundled_calendar()
    # 3/9(월)~3/13(금): 9,10,11,12 = 4일 (13 제외)
    assert cal.business_days_between(date(2026, 3, 9), date(2026, 3, 13)) == 4
    assert cal.business_days_between(date(2026, 3, 9), date(2026, 3, 9)) == 0


def test_구간이_거꾸로면_음수() -> None:
    cal = bundled_calendar()
    assert cal.business_days_between(date(2026, 3, 13), date(2026, 3, 9)) == -4


def test_덮지_않는_연도는_스스로_안다() -> None:
    cal = bundled_calendar()
    assert cal.covers(date(2026, 6, 1))
    assert not cal.covers(date(2030, 6, 1))
    with pytest.raises(CalendarCoverageError):
        cal.require_coverage(date(2030, 6, 1))


def test_DB_행으로_달력을_만들_수_있다() -> None:
    cal = calendar_from_rows([(date(2027, 1, 1), "신정")], source_ref="kasi-2027")
    assert not cal.is_business_day(date(2027, 1, 1))
    assert cal.covers(date(2027, 5, 1))
    assert not cal.covers(date(2026, 5, 1))
    assert cal.source_ref == "kasi-2027"


def test_빈_공휴일표는_거부된다() -> None:
    with pytest.raises(ValueError, match="0건"):
        calendar_from_rows([], source_ref="empty")


# --- 착수일 역산 ----------------------------------------------------------


def test_서류가_없어도_제출_버퍼는_남긴다() -> None:
    plan = _plan_of([_policy("P1", apply_end="2026-04-30")])
    item = _by_id(plan, "P1")
    assert item.preparation_business_days == SUBMISSION_BUFFER_BUSINESS_DAYS
    assert item.recommended_start_date == "2026-04-29"
    assert item.status == "ON_TRACK"


def test_서류_발급은_병렬이라_최댓값을_쓴다() -> None:
    """더하면 3+1+2=6, 병렬이면 max(3,1,2)=3. 후자가 맞다."""
    docs = [
        Document(name="A", doc_code="A", lead_time_business_days=3),
        Document(name="B", doc_code="B", lead_time_business_days=1),
        Document(name="C", doc_code="C", lead_time_business_days=2),
    ]
    plan = _plan_of([_policy("P1", apply_end="2026-04-30", documents=docs)])
    item = _by_id(plan, "P1")
    assert item.preparation_business_days == 3 + SUBMISSION_BUFFER_BUSINESS_DAYS


def test_역산은_공휴일을_건너뛴다() -> None:
    docs = [Document(name="등본", doc_code="RESIDENT", lead_time_business_days=3)]
    # 마감 2026-02-20(금), 준비 4영업일 → 2/19, 2/13, 2/12, 2/11
    plan = _plan_of([_policy("P1", apply_end="2026-02-20", documents=docs)], today=date(2026, 2, 2))
    assert _by_id(plan, "P1").recommended_start_date == "2026-02-11"


def test_소요일을_모르는_서류는_추정치로_메우고_표시한다() -> None:
    docs = [Document(name="미상서류", lead_time_business_days=None)]
    plan = _plan_of([_policy("P1", apply_end="2026-04-30", documents=docs)])
    item = _by_id(plan, "P1")
    assert item.estimated is True
    assert item.documents[0].lead_time_estimated is True
    assert item.documents[0].lead_time_business_days == DEFAULT_UNKNOWN_LEAD_BUSINESS_DAYS
    assert "추정치" in item.reason


def test_상시모집은_역산하지_않는다() -> None:
    plan = _plan_of([_policy("P1", is_rolling=True)])
    item = _by_id(plan, "P1")
    assert item.status == "ROLLING"
    assert item.recommended_start_date is None
    assert plan.summary.rolling == 1


def test_마감_미상은_상시모집과_구분된다() -> None:
    """둘 다 날짜가 없지만 하나는 '역산 불필요', 하나는 '역산 불가'다."""
    plan = _plan_of([_policy("P1", is_rolling=False)])
    item = _by_id(plan, "P1")
    assert item.status == "UNKNOWN"
    assert item.recommended_start_date is None
    assert item.dept_tel == "031-000-0000"  # 확인할 곳을 알려줘야 한다
    assert plan.summary.unknown_deadline == 1


def test_파싱되지_않는_마감일은_날짜를_지어내지_않는다() -> None:
    plan = _plan_of([_policy("P1", apply_end="2026년 4월 중")])
    item = _by_id(plan, "P1")
    assert item.status == "UNKNOWN"
    assert item.recommended_start_date is None


def test_이미_마감된_정책() -> None:
    plan = _plan_of([_policy("P1", apply_end="2026-01-31")])
    item = _by_id(plan, "P1")
    assert item.status == "CLOSED"
    assert plan.summary.closed == 1


def test_오늘_시작해야_하면_URGENT() -> None:
    # 마감 3/12(목), 준비 2영업일(서류1일+버퍼1일) → 착수 3/10 = 오늘
    docs = [Document(name="등본", doc_code="R", lead_time_business_days=1)]
    plan = _plan_of([_policy("P1", apply_end="2026-03-12", documents=docs)])
    item = _by_id(plan, "P1")
    assert item.status == "URGENT"
    assert item.recommended_start_date == TODAY.isoformat()
    assert item.slack_business_days == 0


def test_지금_시작해도_늦으면_INFEASIBLE() -> None:
    docs = [Document(name="심사서류", doc_code="S", lead_time_business_days=10)]
    plan = _plan_of([_policy("P1", apply_end="2026-03-12", documents=docs)])
    item = _by_id(plan, "P1")
    assert item.status == "INFEASIBLE"
    assert "확인해" in item.reason
    assert plan.summary.infeasible == 1


def test_급한_순서로_정렬된다() -> None:
    docs_slow = [Document(name="심사", doc_code="S", lead_time_business_days=10)]
    docs_fast = [Document(name="등본", doc_code="R", lead_time_business_days=1)]
    plan = _plan_of(
        [
            _policy("ROLL", is_rolling=True),
            _policy("OK", apply_end="2026-12-01"),
            _policy("URG", apply_end="2026-03-12", documents=docs_fast),
            _policy("BAD", apply_end="2026-03-12", documents=docs_slow),
        ]
    )
    assert [p.policy_id for p in plan.plans] == ["BAD", "URG", "OK", "ROLL"]


def test_달력이_덮지_않는_구간은_표시된다() -> None:
    plan = _plan_of([_policy("P1", apply_end="2030-04-30")])
    assert _by_id(plan, "P1").outside_calendar_coverage is True


def test_덮는_구간은_표시되지_않는다() -> None:
    plan = _plan_of([_policy("P1", apply_end="2026-04-30")])
    assert _by_id(plan, "P1").outside_calendar_coverage is False


def test_특정_정책만_계획할_수_있다() -> None:
    snapshot = compile_snapshot(
        [_policy("P1", apply_end="2026-04-30"), _policy("P2", apply_end="2026-05-30")],
        version="v",
    )
    verdicts = judge_all(snapshot, _profile(), TODAY)
    plan = build_plan(snapshot, verdicts, TODAY, policy_ids=["P2"])
    assert [p.policy_id for p in plan.plans] == ["P2"]


def test_부적격_정책은_계획에_없다() -> None:
    young = UserProfile(core=Core(birth_date=date(2015, 1, 1), region_code="41135"))
    snapshot = compile_snapshot([_policy("P1", apply_end="2026-04-30")], version="v")
    verdicts = judge_all(snapshot, young, TODAY)
    plan = build_plan(snapshot, verdicts, TODAY)
    assert plan.plans == []
    assert plan.summary.total == 0


# --- 서류 기준 뷰 ---------------------------------------------------------


def test_같은_서류를_여러_정책이_요구하면_한_줄로_합친다() -> None:
    """표기가 달라도 마스터의 같은 코드로 모이면 한 줄이 된다.

    '주민등록등본'과 '주민등록표 등본'은 공고마다 다르게 적히지만 같은 서류다.
    이름으로 합치면 두 줄로 남아 사용자가 주민센터에 두 번 간다.
    """
    doc = Document(name="주민등록등본", lead_time_business_days=0)
    other = Document(name="주민등록표 등본", lead_time_business_days=0)
    plan = _plan_of(
        [
            _policy("P1", apply_end="2026-04-30", documents=[doc]),
            _policy("P2", apply_end="2026-05-30", documents=[other]),
        ]
    )
    tasks = [t for t in plan.documents if t.doc_code == "D001"]
    assert len(tasks) == 1
    assert sorted(tasks[0].required_by) == ["P1", "P2"]


def test_코드가_없는_서류는_이름으로만_합친다() -> None:
    plan = _plan_of(
        [
            _policy("P1", apply_end="2026-04-30", documents=[Document(name="특이서류")]),
            _policy("P2", apply_end="2026-05-30", documents=[Document(name="다른서류")]),
        ]
    )
    names = {t.name for t in plan.documents}
    assert names == {"특이서류", "다른서류"}


def test_같은_서류의_소요일이_다르면_오래_걸리는_쪽을_남긴다() -> None:
    """짧은 쪽을 믿으면 그 서류 때문에 마감을 놓친다."""
    plan = _plan_of(
        [
            _policy(
                "P1",
                apply_end="2026-04-30",
                documents=[Document(name="증명서", doc_code="X", lead_time_business_days=1)],
            ),
            _policy(
                "P2",
                apply_end="2026-05-30",
                documents=[Document(name="증명서", doc_code="X", lead_time_business_days=5)],
            ),
        ]
    )
    task = next(t for t in plan.documents if t.doc_code == "X")
    assert task.lead_time_business_days == 5


def test_서류_기한은_가장_이른_착수일이다() -> None:
    doc = Document(name="등본", doc_code="R", lead_time_business_days=0)
    plan = _plan_of(
        [
            _policy("P1", apply_end="2026-04-30", documents=[doc]),
            _policy("P2", apply_end="2026-12-30", documents=[doc]),
        ]
    )
    task = next(t for t in plan.documents if t.doc_code == "R")
    assert task.needed_by_date == _by_id(plan, "P1").recommended_start_date


def test_서류_비용이_합산된다() -> None:
    plan = _plan_of(
        [
            _policy(
                "P1",
                apply_end="2026-04-30",
                documents=[
                    Document(name="등본", doc_code="R", cost_krw=400),
                    Document(name="소득증명", doc_code="I", cost_krw=1000),
                ],
            )
        ]
    )
    assert plan.total_document_cost_krw == 1400


def test_계획에_없는_정책의_서류는_목록에_없다() -> None:
    young = UserProfile(core=Core(birth_date=date(2015, 1, 1), region_code="41135"))
    snapshot = compile_snapshot(
        [_policy("P1", apply_end="2026-04-30", documents=[Document(name="등본", doc_code="R")])],
        version="v",
    )
    verdicts = judge_all(snapshot, young, TODAY)
    assert build_plan(snapshot, verdicts, TODAY).documents == []


def test_달력_출처가_응답에_실린다() -> None:
    plan = _plan_of([_policy("P1", apply_end="2026-04-30")])
    assert plan.calendar_source_ref
    assert plan.generated_for_date == TODAY.isoformat()


# --- ICS ------------------------------------------------------------------


def test_ics_기본_구조() -> None:
    plan = _plan_of([_policy("P1", apply_end="2026-04-30")])
    text = to_ics(plan).decode("utf-8")
    assert text.startswith("BEGIN:VCALENDAR\r\n")
    assert text.rstrip().endswith("END:VCALENDAR")
    assert text.count("BEGIN:VEVENT") == 2  # 착수일 + 마감일
    assert "\r\n" in text and "\n\n" not in text


def test_ics_종일이벤트의_DTEND_는_다음날() -> None:
    plan = _plan_of([_policy("P1", apply_end="2026-04-30")])
    text = to_ics(plan).decode("utf-8")
    assert "DTSTART;VALUE=DATE:20260430" in text
    assert "DTEND;VALUE=DATE:20260501" in text


def test_ics_쉼표와_세미콜론이_이스케이프된다() -> None:
    plan = _plan_of([_policy("P1", title="청년, 월세; 지원", apply_end="2026-04-30")])
    text = to_ics(plan).decode("utf-8")
    assert "\\," in text
    assert "\\;" in text


def test_ics_긴_한글_줄은_75옥텟_단위로_접힌다() -> None:
    long_title = "청년월세한시특별지원사업" * 6  # 72자 → UTF-8 216옥텟
    plan = _plan_of([_policy("P1", title=long_title, apply_end="2026-04-30")])
    text = to_ics(plan).decode("utf-8")
    for line in text.split("\r\n"):
        assert len(line.encode("utf-8")) <= 75, f"접히지 않은 줄: {line[:40]}..."
    # 접힌 줄은 공백으로 시작한다
    assert any(line.startswith(" ") for line in text.split("\r\n"))


def test_ics_접힌_줄을_다시_이으면_원문이_나온다() -> None:
    long_title = "청년월세한시특별지원사업" * 6
    plan = _plan_of([_policy("P1", title=long_title, apply_end="2026-04-30")])
    text = to_ics(plan).decode("utf-8")
    unfolded = text.replace("\r\n ", "")
    assert f"SUMMARY:[신청 마감] {long_title}" in unfolded


def test_ics_날짜가_없는_정책은_이벤트를_만들지_않는다() -> None:
    plan = _plan_of([_policy("P1", is_rolling=True), _policy("P2")])
    text = to_ics(plan).decode("utf-8")
    assert "BEGIN:VEVENT" not in text


def test_ics_마감된_정책은_마감_이벤트를_만들지_않는다() -> None:
    plan = _plan_of([_policy("P1", apply_end="2026-01-31")])
    text = to_ics(plan).decode("utf-8")
    assert "BEGIN:VEVENT" not in text


def test_ics_알림이_붙는다() -> None:
    plan = _plan_of([_policy("P1", apply_end="2026-04-30")])
    text = to_ics(plan).decode("utf-8")
    assert "BEGIN:VALARM" in text
    assert "TRIGGER:-P1D" in text


def test_ics_설명에_담당부서가_들어간다() -> None:
    plan = _plan_of([_policy("P1", apply_end="2026-04-30")])
    text = to_ics(plan).decode("utf-8").replace("\r\n ", "")
    assert "031-000-0000" in text


def test_ics_UID_는_정책마다_다르다() -> None:
    plan = _plan_of(
        [_policy("P1", apply_end="2026-04-30"), _policy("P2", apply_end="2026-05-30")]
    )
    text = to_ics(plan).decode("utf-8")
    uids = [ln for ln in text.split("\r\n") if ln.startswith("UID:")]
    assert len(uids) == len(set(uids)) == 4


# --- 직렬화 계약 ----------------------------------------------------------


def test_계획_응답은_JSON_으로_왕복한다() -> None:
    plan = _plan_of(
        [
            _policy(
                "P1",
                apply_end="2026-04-30",
                documents=[Document(name="등본", doc_code="R", lead_time_business_days=0)],
            )
        ]
    )
    raw = msgspec.json.encode(plan)
    back = msgspec.json.decode(raw, type=PlanResponse)
    assert back.plans[0].policy_id == "P1"
    assert back.documents[0].doc_code == "R"


def test_달력은_읽기전용으로_공유해도_안전하다() -> None:
    """HolidayCalendar 는 요청 간 공유된다 — 계산이 내부 상태를 바꾸면 안 된다."""
    cal = bundled_calendar()
    before = cal.holiday_name(date(2026, 3, 1))
    cal.add_business_days(date(2026, 2, 25), 10)
    cal.subtract_business_days(date(2026, 3, 20), 10)
    assert cal.holiday_name(date(2026, 3, 1)) == before


def test_직접_만든_달력도_같은_산술을_쓴다() -> None:
    cal = HolidayCalendar({date(2026, 7, 1): "가상휴일"}, covered_years=[2026], source_ref="t")
    assert not cal.is_business_day(date(2026, 7, 1))
    assert cal.next_business_day(date(2026, 6, 30)) == date(2026, 7, 2)


# --- 색인 경로 ↔ 기준 구현 대조 -------------------------------------------
#
# 영업일 산술은 색인(빠른 경로)과 하루씩 훑기(기준 구현) 두 갈래다.
# 빠른 경로가 조용히 어긋나면 "준비 시작일이 하루 이르거나 늦다"로만 나타나
# 아무도 눈치채지 못한다. 그래서 덮는 구간 전체를 전수 대조한다.


class _NaiveCalendar:
    """하루씩 훑는 기준 구현. 느리지만 명백히 맞다."""

    def __init__(self, cal: HolidayCalendar) -> None:
        self.cal = cal

    def next_business_day(self, d: date, *, inclusive: bool = False) -> date:
        from datetime import timedelta

        cur = d if inclusive else d + timedelta(days=1)
        while not self.cal.is_business_day(cur):
            cur += timedelta(days=1)
        return cur

    def previous_business_day(self, d: date, *, inclusive: bool = False) -> date:
        from datetime import timedelta

        cur = d if inclusive else d - timedelta(days=1)
        while not self.cal.is_business_day(cur):
            cur -= timedelta(days=1)
        return cur

    def add(self, start: date, days: int) -> date:
        cur = start
        for _ in range(days):
            cur = self.next_business_day(cur)
        return cur

    def subtract(self, start: date, days: int) -> date:
        cur = start
        for _ in range(days):
            cur = self.previous_business_day(cur)
        return cur

    def between(self, start: date, end: date) -> int:
        from datetime import timedelta

        if start == end:
            return 0
        sign = 1 if end > start else -1
        lo, hi = (start, end) if sign == 1 else (end, start)
        n, cur = 0, lo
        while cur < hi:
            if self.cal.is_business_day(cur):
                n += 1
            cur += timedelta(days=1)
        return n * sign


def test_대조_다음이전_영업일이_2025_2026_전_구간에서_일치() -> None:
    cal = bundled_calendar()
    naive = _NaiveCalendar(cal)
    for d in _all_days(2025) + _all_days(2026):
        if d < date(2025, 1, 5) or d > date(2026, 12, 25):
            continue  # 구간 경계 밖은 폴백 경로라 별도 테스트가 본다
        assert cal.next_business_day(d) == naive.next_business_day(d), d
        assert cal.previous_business_day(d) == naive.previous_business_day(d), d
        assert cal.next_business_day(d, inclusive=True) == naive.next_business_day(
            d, inclusive=True
        ), d
        assert cal.previous_business_day(d, inclusive=True) == naive.previous_business_day(
            d, inclusive=True
        ), d


@pytest.mark.parametrize("days", [1, 2, 3, 5, 10, 30, 60])
def test_대조_더하기빼기가_기준_구현과_일치(days: int) -> None:
    cal = bundled_calendar()
    naive = _NaiveCalendar(cal)
    for d in _all_days(2026):
        if d < date(2026, 4, 1) or d > date(2026, 9, 30):
            continue  # 앞뒤로 days 만큼 여유가 있는 구간만
        assert cal.add_business_days(d, days) == naive.add(d, days), (d, days)
        assert cal.subtract_business_days(d, days) == naive.subtract(d, days), (d, days)


def test_대조_구간_영업일수가_기준_구현과_일치() -> None:
    cal = bundled_calendar()
    naive = _NaiveCalendar(cal)
    anchors = [date(2026, m, 1) for m in range(1, 13)] + [date(2026, 2, 20), date(2026, 9, 30)]
    for a in anchors:
        for b in anchors:
            assert cal.business_days_between(a, b) == naive.between(a, b), (a, b)


def test_색인_구간_밖은_폴백_경로로_계산된다() -> None:
    """2027년은 표가 없다 — 주말 규칙만으로 계산하되 답은 나와야 한다."""
    cal = bundled_calendar()
    # 2027-01-04 는 월요일
    assert cal.next_business_day(date(2027, 1, 1)) == date(2027, 1, 4)
    assert cal.business_days_between(date(2027, 1, 4), date(2027, 1, 11)) == 5


def test_색인_구간_경계를_넘는_계산도_맞는다() -> None:
    cal = bundled_calendar()
    # 2026-12-31(목) 에서 2영업일 뒤 → 2027-01-01(금), 2027-01-04(월)
    assert cal.add_business_days(date(2026, 12, 31), 2) == date(2027, 1, 4)
    # 2025-01-02(목) 에서 2영업일 앞 → 2025-01-01은 신정, 2024-12-31(화), 12-30(월)
    assert cal.subtract_business_days(date(2025, 1, 2), 2) == date(2024, 12, 30)


# --- 서류 마스터 (BE-M5-1) -------------------------------------------------


def test_마스터가_36종을_읽는다() -> None:
    table = doc_master()
    assert len(table) == 36
    assert table["D001"].name == "주민등록표 등본"


def test_소요일은_범위로_들어오고_역산은_최댓값을_쓴다() -> None:
    """재직증명서는 회사 규정에 따라 1~5일이다. 평균을 쓰면 절반이 마감을 놓친다."""
    spec = doc_master()["D032"]
    assert (spec.lead_min_business_days, spec.lead_max_business_days) == (1, 5)
    assert spec.planning_lead_days == 5
    assert spec.has_lead_variance


def test_수수료는_발급_가능한_채널_중_최저가() -> None:
    """등본은 온라인 0원, 방문 400원. 온라인이 되면 0원으로 계산한다."""
    assert doc_master()["D001"].cheapest_fee_krw == 0


def test_온라인_불가_서류는_방문_수수료를_쓴다() -> None:
    """온라인 수수료가 빈칸인 것은 0원이 아니라 '그 채널로 발급 안 됨'이다."""
    spec = doc_master()["D007"]  # 본인서명사실확인서 — OFFLINE_ONLY
    assert spec.fee_online_krw is None
    assert spec.cheapest_fee_krw == 0  # 방문 수수료 면제 (2028-12-31까지)
    assert spec.requires_visit


def test_금액_미상은_무료와_다르다() -> None:
    """0 으로 뭉개면 화면이 '무료'라고 말하고 사용자는 창구에서 돈을 낸다.

    대학 제증명(D020·D022·D023)은 유료인데 금액이 국립대는 규칙, 사립대는 학칙에
    따라 달라 단일값이 없다. 2026-09-20 검증에서 근거 없던 1,000원을 지우면서
    두 칸이 모두 비었고, 그때 '무료'로 보이기 시작했다.
    """
    graduation = doc_master()["D020"]
    assert graduation.fee_online_krw is None
    assert graduation.fee_offline_krw is None
    assert graduation.cheapest_fee_krw is None, "금액 미상이 0원으로 나갑니다"

    free = doc_master()["D008"]  # 소득금액증명 — 실제로 무료
    assert free.cheapest_fee_krw == 0

    # 합계는 아는 것만 더하고, 모르는 건 건수로 따로 알린다.
    for code in ("D020", "D022", "D023"):
        assert doc_master()[code].cheapest_fee_krw is None, code


def test_방문_필요_서류를_구분한다() -> None:
    assert doc_master()["D007"].requires_visit  # OFFLINE_ONLY
    assert doc_master()["D020"].requires_visit  # ANYWHERE_VISIT
    assert not doc_master()["D001"].requires_visit  # ONLINE_INSTANT


def test_유효기간이_없는_서류도_있다() -> None:
    """근로계약서 사본은 만료 개념이 없다. 0 과 None 은 다르다."""
    assert doc_master()["D034"].validity_days is None
    assert doc_master()["D011"].validity_days == 30


def test_유효기간에_근거가_없으면_화면은_계속_추정치라고_말한다() -> None:
    """2026-09-20 검증으로 21행의 소요일·수수료가 확인됐지만, 유효기간은 아니다.

    정부24 민원안내 페이지가 유효기간을 적지 않기 때문이다. 그 값은 '너무 일찍
    떼면 만료된다'는 하한을 정하므로, 근거 없이 검증 표시를 떼면 계획의 절반이
    추정인 채로 확정처럼 보인다.

    이 테스트는 원래 '전 항목이 미검증'이었다. 검증이 진행되면서 사실이 아니게
    됐지만, 지켜야 할 것은 그대로다 — 화면이 추정치를 추정치라고 말하는 것.
    """
    specs = list(doc_master().values())
    assert any(s.verified for s in specs), "검증 결과가 반영되지 않았습니다"

    # 2026-09-20 검증 2차에서 D012(지방세 납세증명서)가 처음으로 유효기간 근거를
    # 확보했다 — 지방세징수법 시행령 제7조. 이 행부터 화면에서 확정으로 나간다.
    grounded = {s.doc_code for s in specs if s.is_fully_grounded}
    assert grounded == {"D012"}, (
        f"유효기간 근거가 있는 행이 바뀌었다: {sorted(grounded)} — "
        f"늘었다면 이 목록을 갱신하고, 줄었다면 근거가 사라진 것이다"
    )

    # 나머지는 소요일만 확인된 상태다. 유효기간이 추정인 채로 딱지가 떨어지면 안 된다.
    lead_only = [s for s in specs if s.verified and not s.validity_grounded]
    assert lead_only, "전부 근거를 확보했다면 이 테스트를 지울 것"
    assert not any(s.is_fully_grounded for s in lead_only)


def test_doc_code_가_이름보다_우선한다() -> None:
    """코드는 배치가 확인한 결과, 이름은 실패할 수 있는 추측이다."""
    spec = doc_resolve("D011", "주민등록등본")
    assert spec is not None and spec.doc_code == "D011"


def test_표기가_달라도_같은_서류를_찾는다() -> None:
    for name in ("주민등록등본", "주민등록표 등본", "주민등록표등본"):
        spec = doc_resolve(None, name)
        assert spec is not None and spec.doc_code == "D001", name


def test_괄호_주기를_떼고_찾는다() -> None:
    spec = doc_resolve(None, "가족관계증명서")
    assert spec is not None and spec.doc_code == "D003"


def test_모르는_서류는_추측하지_않는다() -> None:
    """'재직'과 '퇴직'은 한 글자 차이인데 뜻이 정반대다. 비슷하다고 맞추면 안 된다."""
    assert doc_resolve(None, "무슨무슨증명서") is None
    assert doc_resolve(None, "") is None
    assert doc_resolve("없는코드", "없는이름") is None


def test_재직과_퇴직은_다른_서류로_구분된다() -> None:
    재직 = doc_resolve(None, "재직증명서")
    퇴직 = doc_resolve(None, "퇴직증명서")
    assert 재직 is not None and 재직.doc_code == "D032"
    assert 퇴직 is not None and 퇴직.doc_code == "D033"


def test_별칭이_실존하는_코드를_가리킨다() -> None:
    table = doc_master()
    for alias, code in NAME_ALIASES.items():
        assert code in table, f"별칭 {alias} → 없는 코드 {code}"


def test_마스터_값이_공고_값을_이긴다() -> None:
    """공고는 수백 건이 제각각 틀리고, 마스터는 한 곳에서 고치면 전부 고쳐진다."""
    doc = Document(name="주민등록표 등본", lead_time_business_days=7, cost_krw=99999)
    plan = _plan_of([_policy("P1", apply_end="2026-04-30", documents=[doc])])
    item = _by_id(plan, "P1").documents[0]
    assert item.lead_time_business_days == 0  # 마스터: 온라인 즉시
    assert item.cost_krw == 0
    assert item.doc_code == "D001"


def test_마스터에_없는_서류는_공고_값으로_떨어진다() -> None:
    doc = Document(name="지자체 자체 양식", lead_time_business_days=2, cost_krw=500)
    plan = _plan_of([_policy("P1", apply_end="2026-04-30", documents=[doc])])
    item = _by_id(plan, "P1").documents[0]
    assert item.doc_code is None
    assert item.lead_time_business_days == 2
    assert item.master_unverified is False  # 마스터를 안 거쳤다


def test_미검증_서류_수가_집계된다() -> None:
    doc = Document(name="주민등록표 등본")
    plan = _plan_of([_policy("P1", apply_end="2026-04-30", documents=[doc])])
    assert plan.unverified_document_count == 1


def test_방문_필요_서류_수가_집계된다() -> None:
    docs = [Document(name="졸업증명서(대학)"), Document(name="주민등록표 등본")]
    plan = _plan_of([_policy("P1", apply_end="2026-04-30", documents=docs)])
    assert plan.visit_required_count == 1


# --- 유효기간: 너무 일찍 떼도 안 된다 -------------------------------------


def test_유효기간이_발급_하한을_만든다() -> None:
    """납세증명서는 30일. 마감 두 달 전에 떼면 제출일에 만료된 종이다."""
    doc = Document(name="납세증명서(국세완납)")  # D011, 유효 30일
    plan = _plan_of([_policy("P1", apply_end="2026-04-30", documents=[doc])])
    item = _by_id(plan, "P1")
    # 발급일 당일을 1일째로 세므로 4/30 제출이면 4/1 이후에 떼야 한다
    assert item.issue_not_before_date == "2026-04-01"
    assert "만료" in item.reason


def test_유효기간이_없으면_하한도_없다() -> None:
    doc = Document(name="근로계약서 사본")  # D034, 유효기간 없음
    plan = _plan_of([_policy("P1", apply_end="2026-04-30", documents=[doc])])
    assert _by_id(plan, "P1").issue_not_before_date is None


def test_가장_짧은_유효기간이_구간을_정한다() -> None:
    """30일짜리와 90일짜리를 함께 내면 30일 기준으로 움직여야 둘 다 살아 있다."""
    docs = [
        Document(name="납세증명서(국세완납)"),  # 30일
        Document(name="주민등록표 등본"),  # 90일
    ]
    plan = _plan_of([_policy("P1", apply_end="2026-04-30", documents=docs)])
    assert _by_id(plan, "P1").issue_not_before_date == "2026-04-01"


def test_서류마다_자기_유효기간의_하한을_갖는다() -> None:
    """90일짜리를 30일짜리와 같은 날 떼라고 하면 불필요한 재방문이 생긴다."""
    docs = [Document(name="납세증명서(국세완납)"), Document(name="주민등록표 등본")]
    plan = _plan_of([_policy("P1", apply_end="2026-04-30", documents=docs)])
    by_code = {d.doc_code: d for d in _by_id(plan, "P1").documents}
    assert by_code["D011"].issue_not_before == "2026-04-01"  # 30일
    assert by_code["D001"].issue_not_before == "2026-01-31"  # 90일


def test_유효기간_하한도_함께_보고된다() -> None:
    """준비 대기(1영업일)보다 유효기간 하한이 이르면 착수일은 그대로 두되,
    '이 날 이전엔 떼지 마라'를 따로 알려준다."""
    doc = Document(name="납세증명서(국세완납)")  # 0일 발급, 30일 유효
    plan = _plan_of(
        [_policy("P1", apply_end="2026-06-30", documents=[doc])], today=date(2026, 3, 2)
    )
    item = _by_id(plan, "P1")
    assert item.issue_not_before_date == "2026-06-01"
    assert item.recommended_start_date == "2026-06-29"


def test_유효기간은_달력일이지_영업일이_아니다() -> None:
    """법이 '발급일로부터 N일'로 쓴다. 영업일로 세면 구간이 잘못 넓어진다."""
    doc = Document(name="납세증명서(국세완납)")
    plan = _plan_of([_policy("P1", apply_end="2026-03-31", documents=[doc])])
    # 3/31 - 29일 = 3/2 (주말·공휴일 무관)
    assert _by_id(plan, "P1").issue_not_before_date == "2026-03-02"


def test_한_번_떼서_전부_커버되면_그렇다고_말한다() -> None:
    docs = [Document(name="주민등록표 등본")]  # 90일
    plan = _plan_of(
        [
            _policy("P1", apply_end="2026-04-30", documents=docs),
            _policy("P2", apply_end="2026-05-15", documents=docs),
        ]
    )
    task = next(t for t in plan.documents if t.doc_code == "D001")
    assert task.single_issue_covers_all is True


def test_유효기간_때문에_두_번_떼야_하면_알려준다() -> None:
    """첫 정책 때 뗀 서류를 들고 갔다가 두 번째에서 만료로 반려당하는 걸 막는다."""
    docs = [Document(name="납세증명서(국세완납)")]  # 30일
    plan = _plan_of(
        [
            _policy("P1", apply_end="2026-04-30", documents=docs),
            _policy("P2", apply_end="2026-08-31", documents=docs),
        ]
    )
    task = next(t for t in plan.documents if t.doc_code == "D011")
    assert task.single_issue_covers_all is False
    assert task.validity_days == 30


def test_계획_응답에_마스터_필드가_직렬화된다() -> None:
    docs = [Document(name="졸업증명서(대학)"), Document(name="납세증명서(국세완납)")]
    plan = _plan_of([_policy("P1", apply_end="2026-04-30", documents=docs)])
    back = msgspec.json.decode(msgspec.json.encode(plan), type=PlanResponse)
    grad = next(d for d in back.plans[0].documents if d.doc_code == "D020")
    assert grad.requires_visit
    assert grad.lead_time_min_business_days == 1
    assert grad.lead_time_business_days == 3
    assert grad.channel
