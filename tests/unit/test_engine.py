"""룰 엔진 테스트.

가장 중요한 것은 마지막 섹션의 대조 테스트다.
벡터화 경로(evaluate.py)와 기준 구현(rules.py)이 같은 답을 내는지 무작위 입력으로
확인한다. 이게 깨지면 최적화가 판정 의미를 바꾼 것이고, G2 정확도(92%)는
그 위에서 아무 의미가 없다.
"""

import random
from datetime import date, timedelta

import pytest

from app.engine.compile import CompileError, compile_snapshot
from app.engine.evaluate import explain, judge_all
from app.engine.rules import Outcome, RuleEvaluationError, all_rules, evaluate_rule
from app.engine.timeline import satisfiable_from
from app.schemas.policy import Dept, Meta, PolicySchema, Quality, Rule, Source
from app.schemas.user import Core, UserProfile

TODAY = date(2026, 9, 13)


def R(rule_id: str, field: str, op: str, value, **kw) -> Rule:
    return Rule(rule_id=rule_id, field=field, op=op, value=value, source_quote=f"{field} {op}", **kw)


def P(policy_id: str, eligibility=(), exclusions=()) -> PolicySchema:
    return PolicySchema(
        policy_id=policy_id,
        status="published",
        meta=Meta(
            title=policy_id,
            category="housing",
            authority_level="local",
            region_code=["41465"],
            dept=Dept(name="청년정책과", tel="031-324-0000"),
        ),
        source=Source(origin_url=f"https://example.kr/{policy_id}"),
        eligibility=list(eligibility),
        exclusions=list(exclusions),
    )


def profile(**kw) -> UserProfile:
    core = {"birth_date": date(2001, 3, 14), "region_code": "41465"}
    history = kw.pop("history", None)
    core.update(kw)
    p = UserProfile(core=Core(**core))
    if history is not None:
        p.history = history
    return p


def verdict_of(snap, verdicts, policy_id: str) -> str:
    i = snap.index_of(policy_id)
    if verdicts.ineligible[i]:
        return "INELIGIBLE"
    return "NEEDS_INFO" if verdicts.needs_info[i] else "ELIGIBLE"


# --- 판정 우선순위 ----------------------------------------------------------


def test_모든_조건_충족이면_적격():
    snap = compile_snapshot([P("A", [R("AGE", "age", "between", [19, 34])])])
    v = judge_all(snap, profile(), TODAY)
    assert verdict_of(snap, v, "A") == "ELIGIBLE"


def test_한_조건이라도_미충족이면_부적격():
    snap = compile_snapshot([P("A", [R("AGE", "age", "between", [40, 50])])])
    assert verdict_of(snap, judge_all(snap, profile(), TODAY), "A") == "INELIGIBLE"


def test_미확인_값이_있으면_확인필요():
    snap = compile_snapshot([P("A", [R("INC", "household_income_ratio_median", "<=", 150)])])
    assert verdict_of(snap, judge_all(snap, profile(), TODAY), "A") == "NEEDS_INFO"


def test_부적격이_확인필요보다_우선한다():
    """'왜 안 되는지'를 말해줄 수 있으면 물어보는 것보다 그게 낫다."""
    snap = compile_snapshot(
        [
            P(
                "A",
                [
                    R("AGE", "age", "between", [40, 50]),  # 확정 미충족
                    R("INC", "household_income_ratio_median", "<=", 150),  # 미확인
                ],
            )
        ]
    )
    assert verdict_of(snap, judge_all(snap, profile(), TODAY), "A") == "INELIGIBLE"


def test_제외조항도_자격요건과_같은_방식으로_평가된다():
    """공고문의 제외 조항은 스키마에서 '충족해야 할 조건'으로 표현된다."""
    policy = P(
        "A",
        [R("AGE", "age", "between", [19, 34])],
        [R("SIM", "similar_program_participation_2y", "==", False)],
    )
    snap = compile_snapshot([policy])

    unknown = profile()
    assert verdict_of(snap, judge_all(snap, unknown, TODAY), "A") == "NEEDS_INFO"

    answered = profile()
    answered.answers["similar_program_participation_2y"] = True
    assert verdict_of(snap, judge_all(snap, answered, TODAY), "A") == "INELIGIBLE"

    clean = profile()
    clean.answers["similar_program_participation_2y"] = False
    assert verdict_of(snap, judge_all(snap, clean, TODAY), "A") == "ELIGIBLE"


def test_요약_건수는_전체와_일치한다():
    snap = compile_snapshot(
        [
            P("OK", [R("AGE", "age", "between", [19, 34])]),
            P("NO", [R("AGE", "age", "between", [40, 50])]),
            P("ASK", [R("INC", "household_income_ratio_median", "<=", 150)]),
        ]
    )
    s = judge_all(snap, profile(), TODAY).summary()
    assert (s.eligible, s.ineligible, s.needs_info) == (1, 1, 1)
    assert s.total == 3


# --- 지역 접두 체인 ---------------------------------------------------------


@pytest.mark.parametrize(
    "user_region,expected",
    [("41465", "ELIGIBLE"), ("41135", "INELIGIBLE"), ("11680", "INELIGIBLE")],
)
def test_지역_조건은_접두_체인으로_판정된다(user_region, expected):
    snap = compile_snapshot([P("A", [R("REG", "region_code", "in", ["41465"])])])
    v = judge_all(snap, profile(region_code=user_region), TODAY)
    assert verdict_of(snap, v, "A") == expected


def test_전국_정책은_모든_지역에서_적격():
    """'00'(전국) 하나만 걸린 정책이 서울 사용자에게도 잡혀야 한다."""
    snap = compile_snapshot([P("A", [R("REG", "region_code", "in", ["00"])])])
    for region in ("41465", "41135", "11680"):
        v = judge_all(snap, profile(region_code=region), TODAY)
        assert verdict_of(snap, v, "A") == "ELIGIBLE", f"{region} 에서 전국 정책이 빠졌습니다"


def test_시도_단위_정책은_그_안의_시군구에만_적격():
    snap = compile_snapshot([P("A", [R("REG", "region_code", "in", ["41"])])])
    assert verdict_of(snap, judge_all(snap, profile(region_code="41465"), TODAY), "A") == "ELIGIBLE"
    assert verdict_of(snap, judge_all(snap, profile(region_code="41135"), TODAY), "A") == "ELIGIBLE"
    assert verdict_of(snap, judge_all(snap, profile(region_code="11680"), TODAY), "A") == "INELIGIBLE"


# --- 컴파일 단계에서 잘못된 룰을 잡는다 --------------------------------------


def test_모르는_필드는_컴파일에서_거부된다():
    with pytest.raises(CompileError, match="모르는 필드"):
        compile_snapshot([P("A", [R("X", "반려동물", "==", True)])])


def test_숫자_비교에_문자열_기준값은_거부된다():
    with pytest.raises(CompileError, match="숫자가 아닙니다"):
        compile_snapshot([P("A", [R("X", "age", ">=", "열아홉")])])


def test_목록형_필드에_동등비교는_거부된다():
    """'내 목록이 이 값과 같은가'와 '품는가'는 다른 질문이다."""
    with pytest.raises(CompileError, match="모호"):
        compile_snapshot([P("A", [R("X", "received_policy_ids", "==", "GG-1")])])


# --- 설명 조립 --------------------------------------------------------------


def test_미충족_항목에_충족예상일이_붙는다():
    policy = P(
        "A",
        [R("RES", "residence_months_continuous", ">=", 6, unit="months", time_satisfiable=True)],
    )
    snap = compile_snapshot([policy])
    u = profile(residence_start_date=date(2026, 5, 15))
    r = explain(snap, u, TODAY, 0, "INELIGIBLE")

    assert len(r.unmatched) == 1
    assert r.unmatched[0].satisfiable_from == "2026-11-15"
    assert u.residence_months(date(2026, 11, 15)) == 6


def test_연령_상한_초과는_날짜가_아니라_영구불가로_표시된다():
    snap = compile_snapshot([P("A", [R("AGE", "age", "between", [19, 34])])])
    old = profile(birth_date=date(1988, 1, 1))
    r = explain(snap, old, TODAY, 0, "INELIGIBLE")
    assert r.unmatched[0].permanently_unsatisfiable is True
    assert r.unmatched[0].satisfiable_from is None


def test_신뢰도는_가장_약한_근거를_따른다():
    policy = P(
        "A",
        [
            R("AGE", "age", "between", [40, 50], confidence="CONFIRMED"),
            R("INC", "household_income_ratio_median", "<=", 150, confidence="ESTIMATED"),
        ],
    )
    snap = compile_snapshot([policy])
    r = explain(snap, profile(), TODAY, 0, "INELIGIBLE")
    assert r.confidence == "ESTIMATED"


def test_모든_근거에_원문_인용과_링크가_붙는다():
    policy = P(
        "A",
        [
            R("AGE", "age", "between", [19, 34]),
            R("RES", "residence_months_continuous", ">=", 6),
        ],
    )
    snap = compile_snapshot([policy])
    r = explain(snap, profile(), TODAY, 0, "NEEDS_INFO")
    for item in [*r.matched, *r.unmatched, *r.unknown]:
        assert item.source_quote
        assert item.source_url
    assert r.origin_url and r.dept_tel


# --- 🔴 벡터화 경로 ↔ 기준 구현 대조 (가장 중요) ------------------------------

_NUMERIC = {
    "age": (18, 40),
    "residence_months_continuous": (0, 24),
    "employment_months": (0, 36),
    "household_income_ratio_median": (50, 200),
    "household_size": (1, 6),
}
_CATEGORICAL = {
    "education": ["high_school_graduated", "university_enrolled", "university_graduated"],
    "employment_status": ["employed", "job_seeking", "student"],
    "marital_status": ["single", "married"],
}
# 사용자 값이 목록인 필드 — in / not_in / contains 만 쓸 수 있다
_LIST_FIELDS = {
    "region_code": ["00", "41", "41465", "11", "11680"],
    "received_policy_ids": ["GG-1", "GG-2", "MOLIT-1"],
}


def _random_rule(rnd: random.Random, i: int) -> Rule:
    kind = rnd.choice(["numeric", "categorical", "bool", "list", "exists"])

    if kind == "numeric":
        field, (lo, hi) = rnd.choice(list(_NUMERIC.items()))
        op = rnd.choice([">", ">=", "<", "<=", "between", "==", "!="])
        value = [rnd.randint(lo, hi - 5), rnd.randint(lo + 5, hi)] if op == "between" else rnd.randint(lo, hi)
        if op == "between":
            value = sorted(value)
        return R(f"N{i}", field, op, value)

    if kind == "categorical":
        field, choices = rnd.choice(list(_CATEGORICAL.items()))
        op = rnd.choice(["in", "not_in", "==", "!="])
        value = rnd.sample(choices, rnd.randint(1, len(choices))) if op in ("in", "not_in") else rnd.choice(choices)
        return R(f"C{i}", field, op, value)

    if kind == "bool":
        return R(f"B{i}", "similar_program_participation_2y", rnd.choice(["==", "!="]), rnd.choice([True, False]))

    if kind == "list":
        field, pool = rnd.choice(list(_LIST_FIELDS.items()))
        op = rnd.choice(["in", "not_in", "contains"])
        value = (
            rnd.sample(pool, rnd.randint(1, len(pool)))
            if op in ("in", "not_in")
            else rnd.choice(pool)
        )
        return R(f"L{i}", field, op, value)

    field = rnd.choice(["employment_months", "household_income_ratio_median"])
    return R(f"E{i}", field, "exists", rnd.choice([True, False]))


def _random_profile(rnd: random.Random) -> UserProfile:
    def maybe(value):
        return value if rnd.random() < 0.7 else None

    u = UserProfile(
        core=Core(
            birth_date=date(rnd.randint(1986, 2008), rnd.randint(1, 12), rnd.randint(1, 28)),
            region_code=rnd.choice(["41465", "41135", "11680"]),
            residence_start_date=maybe(date(2024, rnd.randint(1, 12), rnd.randint(1, 28))),
            employment_start_date=maybe(date(2025, rnd.randint(1, 12), rnd.randint(1, 28))),
            education=maybe(rnd.choice(_CATEGORICAL["education"])),
            employment_status=maybe(rnd.choice(_CATEGORICAL["employment_status"])),
            marital_status=maybe(rnd.choice(_CATEGORICAL["marital_status"])),
            household_size=maybe(rnd.randint(1, 5)),
            household_income_ratio_median=maybe(rnd.randint(60, 190)),
        )
    )
    u.history.received_policy_ids = rnd.sample(["GG-1", "GG-2", "MOLIT-1"], rnd.randint(0, 3))
    if rnd.random() < 0.6:
        u.answers["similar_program_participation_2y"] = rnd.choice([True, False])
    return u


def _reference_verdict(policy: PolicySchema, user: UserProfile, today: date) -> str:
    """rules.py 만으로 내린 판정. 벡터화 경로가 이것과 같아야 한다."""
    failed = unresolved = False
    for rule in all_rules(policy.eligibility, policy.exclusions):
        outcome = evaluate_rule(rule, user.resolve(rule.field, today))
        if outcome is Outcome.FAIL:
            failed = True
        elif outcome is Outcome.UNKNOWN:
            unresolved = True
    if failed:
        return "INELIGIBLE"
    return "NEEDS_INFO" if unresolved else "ELIGIBLE"


def test_벡터화_경로가_기준_구현과_완전히_일치한다():
    rnd = random.Random(20260913)
    policies = [
        P(f"P{p:03d}", [_random_rule(rnd, i) for i in range(rnd.randint(1, 6))])
        for p in range(120)
    ]
    snap = compile_snapshot(policies)

    compared = 0
    for _ in range(60):
        user = _random_profile(rnd)
        verdicts = judge_all(snap, user, TODAY)
        for i, policy in enumerate(policies):
            expected = _reference_verdict(policy, user, TODAY)
            actual = (
                "INELIGIBLE"
                if verdicts.ineligible[i]
                else "NEEDS_INFO"
                if verdicts.needs_info[i]
                else "ELIGIBLE"
            )
            assert actual == expected, (
                f"{policy.policy_id} 에서 경로가 갈렸습니다: "
                f"벡터={actual} 기준={expected} / 룰={[(r.field, r.op, r.value) for r in policy.eligibility]}"
            )
            compared += 1

    assert compared == 120 * 60  # 7,200건 대조


def test_판정_분류는_서로_겹치지_않는다():
    rnd = random.Random(7)
    policies = [P(f"P{i}", [_random_rule(rnd, i) for i in range(3)]) for i in range(50)]
    snap = compile_snapshot(policies)
    v = judge_all(snap, _random_profile(rnd), TODAY)
    assert not (v.eligible & v.ineligible).any()
    assert not (v.eligible & v.needs_info).any()
    assert not (v.ineligible & v.needs_info).any()
    assert (v.eligible | v.ineligible | v.needs_info).all()


# --- 기준 구현 자체의 경계 --------------------------------------------------


def test_exists_는_None_을_미확인으로_보지_않는다():
    """값의 유무를 묻는 연산자라, 없다는 사실 자체가 답이다."""
    assert evaluate_rule(R("X", "employment_months", "exists", False), None) is Outcome.PASS
    assert evaluate_rule(R("X", "employment_months", "exists", True), None) is Outcome.FAIL
    assert evaluate_rule(R("X", "employment_months", "exists", True), 3) is Outcome.PASS


def test_숫자_비교에_문자열이_들어오면_조용히_넘기지_않는다():
    with pytest.raises(RuleEvaluationError):
        evaluate_rule(R("X", "age", ">=", 19), "스물다섯")


def test_미확인은_충족도_미충족도_아니다():
    assert evaluate_rule(R("X", "age", ">=", 19), None) is Outcome.UNKNOWN


def test_충족예상일은_판정과_같은_날짜에_바뀐다():
    """화면의 날짜와 실제 재판정이 어긋나면 안 된다."""
    rnd = random.Random(11)
    for _ in range(30):
        start = date(2026, rnd.randint(1, 12), rnd.randint(1, 28))
        need = rnd.randint(1, 24)
        u = profile(residence_start_date=start)
        s = satisfiable_from("residence_months_continuous", u, start, minimum=need)
        assert s.date is not None
        assert u.residence_months(s.date) >= need
        assert u.residence_months(s.date - timedelta(days=1)) < need


# --- 옮기지 못한 조건 (A2 unrepresentable_conditions) -----------------------


def _with_review(policy: PolicySchema, *fields: str) -> PolicySchema:
    policy.quality = Quality(needs_review_fields=list(fields))
    return policy


def test_옮기지_못한_조건이_있으면_적격을_확정으로_내보내지_않는다():
    """무주택·보증금처럼 룰로 못 옮긴 조건이 남아 있으면 '적격'은 반쪽짜리다."""
    policy = _with_review(P("A", [R("AGE", "age", "between", [19, 34])]), "무주택 여부")
    snap = compile_snapshot([policy])

    r = explain(snap, profile(), TODAY, 0, "ELIGIBLE")

    assert r.verdict == "ELIGIBLE"  # 아는 조건은 실제로 충족했다
    assert r.confidence == "NEEDS_REVIEW"
    assert r.needs_review_fields == ["무주택 여부"]
    # 확인이 필요하다고만 하고 확인할 방법을 안 주면 사용자는 아무것도 못 한다
    assert r.dept_tel and r.origin_url


def test_확인필요_판정도_같은_이유로_신뢰도가_내려간다():
    policy = _with_review(
        P("A", [R("INC", "household_income_ratio_median", "<=", 150)]), "임차보증금"
    )
    snap = compile_snapshot([policy])

    assert explain(snap, profile(), TODAY, 0, "NEEDS_INFO").confidence == "NEEDS_REVIEW"


def test_부적격에는_적용하지_않는다():
    """조건은 전부 충족해야 하므로, 못 본 조건이 더 있어도 탈락은 뒤집히지 않는다.

    명확한 탈락에 '확인 필요'를 붙이면 진짜 확인이 필요한 판정과 섞인다.
    """
    policy = _with_review(P("A", [R("AGE", "age", "between", [40, 50])]), "무주택 여부")
    snap = compile_snapshot([policy])

    r = explain(snap, profile(), TODAY, 0, "INELIGIBLE")

    assert r.confidence == "CONFIRMED"
    # 낮추지 않더라도 무엇을 못 봤는지는 알려준다
    assert r.needs_review_fields == ["무주택 여부"]


def test_옮기지_못한_조건이_없으면_그대로_확정이다():
    policy = P("A", [R("AGE", "age", "between", [19, 34])])
    snap = compile_snapshot([policy])

    r = explain(snap, profile(), TODAY, 0, "ELIGIBLE")

    assert r.confidence == "CONFIRMED"
    assert r.needs_review_fields == []


# --- 충족 예상일 (마일스톤의 네 번째 상태 FUTURE_PASS) ----------------------


def test_미충족이_전부_시간으로_해결되면_가장_늦은_날이_나온다():
    """이른 쪽 날짜를 주면 그날 신청했다가 다른 조건 때문에 반려된다."""
    policy = P(
        "A",
        [
            R("AGE", "age", ">=", 30),  # 2001-03-14 생 → 2031-03-14
            R("RES", "residence_months_continuous", ">=", 6),  # 더 이른 날
        ],
    )
    snap = compile_snapshot([policy])

    r = explain(snap, profile(residence_start_date=date(2026, 5, 15)), TODAY, 0, "INELIGIBLE")

    dates = sorted(u.satisfiable_from for u in r.unmatched)
    assert r.future_eligible_from == dates[-1]
    assert r.future_eligible_from == "2031-03-14"


def test_시간과_무관한_조건이_섞이면_날짜를_주지_않는다():
    """소득은 기다린다고 해결되지 않는다. 날짜를 주면 '기다리면 된다'는 틀린 안내다."""
    policy = P(
        "A",
        [
            R("RES", "residence_months_continuous", ">=", 6),
            R("INC", "household_income_ratio_median", "<=", 50),
        ],
    )
    snap = compile_snapshot([policy])

    r = explain(
        snap,
        profile(residence_start_date=date(2026, 5, 15), household_income_ratio_median=120),
        TODAY,
        0,
        "INELIGIBLE",
    )

    assert r.future_eligible_from is None


def test_연령_상한_초과에는_날짜를_주지_않는다():
    policy = P("A", [R("AGE", "age", "between", [19, 20])])
    snap = compile_snapshot([policy])

    r = explain(snap, profile(), TODAY, 0, "INELIGIBLE")

    assert r.unmatched[0].permanently_unsatisfiable is True
    assert r.future_eligible_from is None


def test_미확인_조건이_남아_있으면_날짜를_주지_않는다():
    """그날 적격이 된다고 약속할 수 없다 — 소득을 모르면 결과를 모른다."""
    policy = P(
        "A",
        [
            R("RES", "residence_months_continuous", ">=", 6),
            R("INC", "household_income_ratio_median", "<=", 150),
        ],
    )
    snap = compile_snapshot([policy])

    r = explain(snap, profile(residence_start_date=date(2026, 5, 15)), TODAY, 0, "INELIGIBLE")

    assert r.unknown  # 소득 미입력
    assert r.future_eligible_from is None


def test_적격_정책에는_날짜가_없다():
    policy = P("A", [R("AGE", "age", "between", [19, 34])])
    snap = compile_snapshot([policy])

    assert explain(snap, profile(), TODAY, 0, "ELIGIBLE").future_eligible_from is None


def test_충족예상일을_줄_때는_못_본_조건이_신뢰도를_낮춘다():
    """'오늘 안 된다'와 '그날 된다'는 다른 주장이고, 뒤쪽은 얼마든지 뒤집힌다."""
    policy = _with_review(P("A", [R("RES", "residence_months_continuous", ">=", 6)]), "무주택 여부")
    snap = compile_snapshot([policy])

    r = explain(snap, profile(residence_start_date=date(2026, 5, 15)), TODAY, 0, "INELIGIBLE")

    assert r.future_eligible_from == "2026-11-15"
    assert r.confidence == "NEEDS_REVIEW"
