"""역질문으로 받은 개월수가 실제로 판정에 반영되는지.

여기서 지키는 것은 '답을 물어봤으면 그 답을 쓴다'이다. 반영되지 않으면 사용자는
입력을 했는데 화면이 그대로인 것을 보게 되고, 그 정책은 영원히 NEEDS_INFO 에
남는다. 틀린 판정이 아니라 판정이 아예 안 나오는 쪽이라 아무도 신고하지 않는다.
"""

from datetime import date

import pytest

from app.engine.compile import compile_snapshot
from app.engine.evaluate import explain, judge_all
from app.engine.questions import _ANSWER_TYPES, _fallback_text, build_queue
from app.schemas.policy import Dept, Meta, PolicySchema, Rule, Source
from app.schemas.user import Core, UserProfile

TODAY = date(2026, 9, 19)


def P(policy_id: str, rules: list[Rule]) -> PolicySchema:
    return PolicySchema(
        policy_id=policy_id,
        status="published",
        meta=Meta(
            title=policy_id,
            category="job",
            authority_level="local",
            region_code=["41465"],
            dept=Dept(name="청년정책과", tel="031-000-0000"),
        ),
        source=Source(origin_url=f"https://example.kr/{policy_id}"),
        eligibility=list(rules),
    )


def R(rule_id: str, field: str, op: str, value: object, **kw: object) -> Rule:
    return Rule(
        rule_id=rule_id, field=field, op=op, value=value,
        source_quote=f"{field} 요건", **kw,
    )


CORE = {"birth_date": date(1998, 3, 14), "region_code": "41465"}


def _verdict(policy: PolicySchema, profile: UserProfile) -> str:
    snap = compile_snapshot([policy])
    v = judge_all(snap, profile, TODAY)
    if v.eligible[0]:
        return "ELIGIBLE"
    return "NEEDS_INFO" if v.needs_info[0] else "INELIGIBLE"


@pytest.mark.parametrize(
    "field",
    ["employment_months", "residence_months_continuous"],
)
def test_답변한_개월수가_판정을_바꾼다(field: str) -> None:
    """이 테스트가 잡는 회귀: resolve() 가 answers 를 보지 않던 상태.

    질문은 나가는데 답변은 버려져서, 답을 해도 NEEDS_INFO 에 머물렀다.
    """
    policy = P("P1", [R("M", field, ">=", 6)])

    before = UserProfile(core=Core(**CORE))
    assert _verdict(policy, before) == "NEEDS_INFO"

    after = UserProfile(core=Core(**CORE), answers={field: 24})
    assert _verdict(policy, after) == "ELIGIBLE"


@pytest.mark.parametrize(
    "field", ["employment_months", "residence_months_continuous"]
)
def test_답변이_모자라면_부적격이_된다(field: str) -> None:
    """반영된다는 것은 통과시킨다는 뜻이 아니다. 모자라면 부적격이어야 한다."""
    policy = P("P1", [R("M", field, ">=", 24)])
    user = UserProfile(core=Core(**CORE), answers={field: 3})
    assert _verdict(policy, user) == "INELIGIBLE"


def test_온보딩_날짜가_역질문_답변을_이긴다() -> None:
    """core 가 우선이라는 규칙은 다른 필드와 같아야 한다."""
    policy = P("P1", [R("M", "employment_months", ">=", 24)])
    user = UserProfile(
        core=Core(**CORE, employment_start_date=date(2026, 3, 19)),  # 6개월
        answers={"employment_months": 99},
    )
    assert user.resolve("employment_months", TODAY) == 6
    assert _verdict(policy, user) == "INELIGIBLE"


def test_답변한_개월수로는_충족예상일을_약속하지_않는다() -> None:
    """"3개월째"는 [3,4) 개월 어딘가라, 역산한 시작일이 최대 한 달 틀린다.

    그 오차로 만든 날짜는 "11월 15일부터 가능"으로 화면에 나가고 그날 다시
    판정하면 여전히 부적격일 수 있다. 날짜를 주지 않는 쪽이 맞다.
    """
    policy = P("P1", [R("M", "employment_months", ">=", 24, time_satisfiable=True)])
    user = UserProfile(core=Core(**CORE), answers={"employment_months": 3})
    snap = compile_snapshot([policy])
    result = explain(snap, user, TODAY, 0, "INELIGIBLE")
    assert result.future_eligible_from is None


def test_시작일을_주면_충족예상일이_나온다() -> None:
    """날짜를 받으면 약속할 수 있다 — 위 테스트의 반대쪽."""
    policy = P("P1", [R("M", "employment_months", ">=", 24, time_satisfiable=True)])
    user = UserProfile(
        core=Core(**CORE, employment_start_date=date(2026, 3, 19)),
        answers={},
    )
    snap = compile_snapshot([policy])
    result = explain(snap, user, TODAY, 0, "INELIGIBLE")
    assert result.future_eligible_from == "2028-03-19"


def test_답한_필드는_질문_큐에서_사라진다() -> None:
    """큐에 남아 있으면 같은 걸 또 묻게 된다 (G3: 중복 질문 0건)."""
    policy = P("P1", [R("M", "employment_months", ">=", 6)])
    snap = compile_snapshot([policy])

    bare = UserProfile(core=Core(**CORE))
    q1 = build_queue(snap, bare, TODAY, judge_all(snap, bare, TODAY))
    assert [x.field for x in q1.questions] == ["employment_months"]

    answered = UserProfile(core=Core(**CORE), answers={"employment_months": 24})
    q2 = build_queue(snap, answered, TODAY, judge_all(snap, answered, TODAY))
    assert q2.questions == []


@pytest.mark.parametrize(
    "field", sorted(f for f, t in _ANSWER_TYPES.items() if t == "number")
)
def test_숫자로_받는_질문은_숫자를_묻는다(field: str) -> None:
    """답변 형태가 number 인데 문구가 날짜를 물으면 사용자가 연도를 적는다.

    "언제부터 살고 계신가요?" 밑에 숫자 입력칸이 있으면 2023 을 적게 되고,
    그 값은 개월수로 읽힌다. 문구와 입력 형태는 같은 것을 가리켜야 한다.
    """
    text = _fallback_text(field)
    assert "언제부터" not in text, f"{field}: number 인데 날짜를 묻는다 — {text}"
