"""역질문 큐 테스트 (US-03 / G3 게이트).

G3 기준 중 '중복 질문 0건'과 화면 문구의 정확성을 여기서 지킨다.
"이 답변으로 N개 정책 판정이 완료됩니다"가 틀리면 사용자를 속이는 것이다.
"""

from datetime import date

import pytest

from app.engine.compile import compile_snapshot
from app.engine.evaluate import judge_all
from app.engine.questions import MAX_QUESTIONS, build_queue, unanswerable_policies
from app.schemas.policy import Dept, Meta, PolicySchema, Rule, Source
from app.schemas.user import Core, UserProfile

TODAY = date(2026, 9, 13)


def R(rule_id, field, op, value, **kw):
    return Rule(rule_id=rule_id, field=field, op=op, value=value, source_quote=f"{field} 요건", **kw)


def P(policy_id, rules):
    return PolicySchema(
        policy_id=policy_id,
        status="published",
        meta=Meta(
            title=policy_id,
            category="welfare",
            authority_level="local",
            region_code=["41465"],
            dept=Dept(name="청년정책과", tel="031-000-0000"),
        ),
        source=Source(origin_url=f"https://example.kr/{policy_id}"),
        eligibility=list(rules),
    )


def bare_profile():
    """연령·지역만 아는 사용자. 나머지는 전부 미확인이다."""
    return UserProfile(core=Core(birth_date=date(2001, 3, 14), region_code="41465"))


def queue_for(policies, profile=None, limit=MAX_QUESTIONS):
    snap = compile_snapshot(policies)
    user = profile or bare_profile()
    return snap, user, build_queue(snap, user, TODAY, judge_all(snap, user, TODAY), limit=limit)


INCOME = ("INC", "household_income_ratio_median", "<=", 150)
SIZE = ("SZ", "household_size", ">=", 2)


# --- G3: 중복 질문 0건 ------------------------------------------------------


def test_같은_필드는_정책이_몇_개든_질문_하나로_병합된다():
    policies = [P(f"P{i}", [R(*INCOME)]) for i in range(20)]
    _, _, q = queue_for(policies)
    assert len(q.questions) == 1
    assert q.questions[0].field == "household_income_ratio_median"


def test_큐에_같은_필드가_두_번_나오지_않는다():
    policies = [
        P("A", [R(*INCOME), R(*SIZE)]),
        P("B", [R(*INCOME)]),
        P("C", [R(*SIZE)]),
    ]
    _, _, q = queue_for(policies)
    fields = [x.field for x in q.questions]
    assert len(fields) == len(set(fields))


def test_한_정책_안에_같은_필드_룰이_여러_개여도_한_번만_센다():
    policies = [P("A", [R("I1", "household_income_ratio_median", ">=", 50),
                        R("I2", "household_income_ratio_median", "<=", 150)])]
    _, _, q = queue_for(policies)
    assert len(q.questions) == 1
    assert q.questions[0].affects == 1


# --- 화면 문구가 사실이어야 한다 ---------------------------------------------


def test_resolves_는_답하면_실제로_판정이_끝나는_정책_수다():
    """미확인이 2개인 정책은 하나만 답해도 안 끝난다 — 그걸 세면 거짓말이 된다."""
    policies = [
        P("ONE_A", [R(*INCOME)]),  # 소득만 모름 → 답하면 끝
        P("ONE_B", [R(*INCOME)]),  # 소득만 모름 → 답하면 끝
        P("TWO", [R(*INCOME), R(*SIZE)]),  # 둘 다 모름 → 소득만 답해선 안 끝남
    ]
    _, _, q = queue_for(policies)
    income_q = next(x for x in q.questions if x.field == "household_income_ratio_median")
    assert income_q.resolves == 2  # ONE_A, ONE_B 만
    assert income_q.affects == 3  # TWO 도 참조는 한다


def test_resolves_주장이_실제_재판정과_일치한다():
    """문구가 약속한 수만큼 실제로 판정이 완료되는지 직접 확인한다."""
    policies = [
        P("ONE_A", [R(*INCOME)]),
        P("ONE_B", [R(*INCOME)]),
        P("TWO", [R(*INCOME), R(*SIZE)]),
    ]
    snap, user, q = queue_for(policies)
    before = judge_all(snap, user, TODAY).summary().needs_info

    income_q = next(x for x in q.questions if x.field == "household_income_ratio_median")
    user.answers["household_income_ratio_median"] = 120
    after = judge_all(snap, user, TODAY).summary().needs_info

    assert before - after == income_q.resolves


def test_해소되는_정책이_많은_질문이_먼저_나온다():
    policies = [
        *[P(f"INC{i}", [R(*INCOME)]) for i in range(5)],
        *[P(f"SZ{i}", [R(*SIZE)]) for i in range(2)],
    ]
    _, _, q = queue_for(policies)
    assert [x.field for x in q.questions] == [
        "household_income_ratio_median",
        "household_size",
    ]
    assert q.questions[0].resolves == 5


# --- 상한 · 부가 정보 --------------------------------------------------------


def test_질문은_상한을_넘지_않고_잘린_사실을_알린다():
    # 미확인을 실제로 만드는 룰이어야 한다 (exists 는 값의 유무 자체가 답이라 제외)
    specs = [
        ("household_income_ratio_median", "<=", 150),
        ("household_size", ">=", 2),
        ("employment_months", ">=", 6),
        ("education", "in", ["university_graduated"]),
        ("employment_status", "in", ["job_seeking"]),
        ("marital_status", "in", ["single"]),
        ("similar_program_participation_2y", "==", False),
        ("residence_months_continuous", ">=", 6),
    ]
    policies = [P(f"P{i}", [R(f"R{i}", *spec)]) for i, spec in enumerate(specs)]

    _, _, full = queue_for(policies)
    assert full.total_unresolved_fields == len(specs)

    _, _, q = queue_for(policies, limit=3)
    assert len(q.questions) == 3
    # 잘렸다는 사실이 드러나야 FE 가 "더 있음"을 표시할 수 있다
    assert q.total_unresolved_fields == len(specs)


def test_상한_기본값은_10개다():
    assert MAX_QUESTIONS == 10


def test_각_질문에_근거_구절과_정책_목록이_붙는다():
    policies = [P("A", [R(*INCOME)]), P("B", [R(*INCOME)])]
    _, _, q = queue_for(policies)
    assert q.questions[0].source_quote
    assert set(q.questions[0].source_policy_ids) == {"A", "B"}


# --- 질문 문장 --------------------------------------------------------------


def test_공고문_질문_템플릿이_있으면_그것을_쓴다():
    policies = [
        P("A", [R("SIM", "similar_program_participation_2y", "==", False, askable=True,
                  question_template="최근 2년 이내 유사 청년지원사업에 참여한 경험이 있나요?")])
    ]
    _, _, q = queue_for(policies)
    assert q.questions[0].text == "최근 2년 이내 유사 청년지원사업에 참여한 경험이 있나요?"


def test_템플릿이_여럿이면_가장_많이_쓰인_문장을_쓴다():
    def ask(text):
        return R("SIM", "similar_program_participation_2y", "==", False,
                 askable=True, question_template=text)

    policies = [P("A", [ask("흔한 표현")]), P("B", [ask("흔한 표현")]), P("C", [ask("드문 표현")])]
    _, _, q = queue_for(policies)
    assert q.questions[0].text == "흔한 표현"


def test_템플릿이_없으면_일상어_기본_문구를_쓴다():
    """AI 가 질문을 못 만들어도 사용자가 막히면 안 된다."""
    policies = [P("A", [R(*INCOME)])]
    _, _, q = queue_for(policies)
    assert q.questions[0].text
    assert "household_income" not in q.questions[0].text  # 필드명 노출 금지


@pytest.mark.parametrize(
    "field,op,value,expected_type",
    [
        ("similar_program_participation_2y", "==", False, "boolean"),
        ("household_income_ratio_median", "<=", 150, "number"),
        ("education", "in", ["university_graduated"], "choice"),
    ],
)
def test_답변_형태가_필드에_맞게_지정된다(field, op, value, expected_type):
    _, _, q = queue_for([P("A", [R("X", field, op, value)])])
    assert q.questions[0].answer_type == expected_type


def test_선택형_질문에는_선택지가_붙는다():
    _, _, q = queue_for([P("A", [R("E", "education", "in", ["university_graduated"])])])
    assert "university_graduated" in q.questions[0].choices


# --- 빈 큐 · 폴백 -----------------------------------------------------------


def test_미확인이_없으면_질문도_없다():
    user = UserProfile(core=Core(birth_date=date(2001, 3, 14), region_code="41465"))
    _, _, q = queue_for([P("A", [R("AGE", "age", "between", [19, 34])])], profile=user)
    assert q.questions == []
    assert q.needs_info_policies == 0


def test_부적격_정책의_미확인은_묻지_않는다():
    """이미 다른 이유로 떨어진 정책 때문에 질문을 늘리면 사용자만 피곤하다."""
    policies = [P("A", [R("AGE", "age", "between", [40, 50]), R(*INCOME)])]
    _, _, q = queue_for(policies)
    assert q.questions == []


def test_모르겠음_선택_시_막히는_정책을_알려준다():
    """그 정책들은 NEEDS_REVIEW 로 두고 담당부서로 안내한다."""
    policies = [P("A", [R(*INCOME)]), P("B", [R(*INCOME)]), P("C", [R(*SIZE)])]
    snap = compile_snapshot(policies)
    user = bare_profile()
    v = judge_all(snap, user, TODAY)
    blocked = unanswerable_policies(snap, user, TODAY, v, "household_income_ratio_median")
    assert set(blocked) == {"A", "B"}
