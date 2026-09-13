"""스키마 계약 테스트.

여기서 지키는 것은 타입 검사가 아니라 게이트 기준이다.
이 테스트가 깨지면 G1(source_quote 100%) 또는 US-02/US-06 수용기준이 깨진 것이다.
"""

from datetime import date

import msgspec
import pytest

from app.schemas import (
    Core,
    JudgementResult,
    Meta,
    PolicySchema,
    Rule,
    Source,
    UnknownRule,
    UnmatchedRule,
    UserProfile,
    assert_valid_policy,
    region_chain,
    validate_judgement,
    validate_policy,
)
from app.schemas.validate import SchemaValidationError


def _codes(violations):
    return {v.code for v in violations}


def _policy(**kw) -> PolicySchema:
    """게시 가능한 최소 정책. 각 테스트가 필요한 부분만 덮어쓴다."""
    base = dict(
        policy_id="GG-YONGIN-2026-0042",
        status="published",
        meta=Meta(
            title="용인시 청년 월세 지원사업",
            category="housing",
            authority_level="local",
            region_code=["41465"],
        ),
        source=Source(origin_url="https://example.gov.kr/notice/42"),
        eligibility=[
            Rule(
                rule_id="AGE_RANGE",
                field="age",
                op="between",
                value=[19, 34],
                unit="years",
                source_quote="만 19세 이상 34세 이하 청년",
            )
        ],
    )
    base.update(kw)
    return PolicySchema(**base)


# --- G1: source_quote 100% --------------------------------------------------


def test_최소_정책은_위반이_없다():
    assert validate_policy(_policy()) == []


def test_근거_구절이_없는_룰은_거부된다():
    p = _policy(
        eligibility=[
            Rule(rule_id="AGE_RANGE", field="age", op="between", value=[19, 34], source_quote="")
        ]
    )
    assert "MISSING_SOURCE_QUOTE" in _codes(validate_policy(p))


def test_공백만_있는_근거_구절도_거부된다():
    p = _policy(
        eligibility=[
            Rule(rule_id="AGE_RANGE", field="age", op="between", value=[19, 34], source_quote="   \n ")
        ]
    )
    assert "MISSING_SOURCE_QUOTE" in _codes(validate_policy(p))


def test_assert_valid_policy_는_위반시_예외를_던진다():
    p = _policy(eligibility=[Rule(rule_id="R", field="age", op=">=", value=19, source_quote="")])
    with pytest.raises(SchemaValidationError) as e:
        assert_valid_policy(p)
    assert e.value.violations


# --- 룰 엔진이 평가할 수 없는 룰을 걸러낸다 ---------------------------------


def test_알_수_없는_필드는_조용히_통과하지_않는다():
    """알 수 없는 필드를 무시하면 '부적격인데 적격' 오판정이 나온다."""
    p = _policy(
        eligibility=[
            Rule(rule_id="X", field="반려동물_유무", op="==", value=True, source_quote="반려동물을 키우지 않을 것")
        ]
    )
    assert "UNKNOWN_FIELD" in _codes(validate_policy(p))


def test_시간으로_충족_불가능한_필드에_time_satisfiable_금지():
    """소득에 충족 예상일을 달면 사용자에게 거짓 날짜가 표시된다."""
    p = _policy(
        eligibility=[
            Rule(
                rule_id="INCOME",
                field="household_income_ratio_median",
                op="<=",
                value=150,
                time_satisfiable=True,
                source_quote="기준 중위소득 150% 이하",
            )
        ]
    )
    assert "NOT_TIME_SATISFIABLE" in _codes(validate_policy(p))


def test_거주기간은_time_satisfiable_이_허용된다():
    p = _policy(
        eligibility=[
            Rule(
                rule_id="RES",
                field="residence_months_continuous",
                op=">=",
                value=6,
                unit="months",
                time_satisfiable=True,
                source_quote="신청일 기준 용인시에 6개월 이상 계속하여 거주",
            )
        ]
    )
    assert validate_policy(p) == []


def test_역질문_대상인데_질문_템플릿이_없으면_거부():
    p = _policy(
        exclusions=[
            Rule(
                rule_id="SIM",
                field="similar_program_participation_2y",
                op="==",
                value=False,
                askable=True,
                source_quote="최근 2년 이내 타 유사사업 참여자는 제외",
            )
        ]
    )
    assert "ASKABLE_WITHOUT_QUESTION" in _codes(validate_policy(p))


# --- op ↔ value 형태 --------------------------------------------------------


@pytest.mark.parametrize(
    "op,value,code",
    [
        ("between", 19, "BAD_VALUE_SHAPE"),
        ("between", [19, 34, 50], "BAD_VALUE_SHAPE"),
        ("between", [34, 19], "RANGE_INVERTED"),
        (">=", [19], "BAD_VALUE_SHAPE"),
        (">=", "열아홉", "BAD_VALUE_TYPE"),
        ("in", 19, "BAD_VALUE_SHAPE"),
        ("in", [], "EMPTY_VALUE_LIST"),
    ],
)
def test_op와_value_형태가_맞지_않으면_거부(op, value, code):
    p = _policy(eligibility=[Rule(rule_id="R", field="age", op=op, value=value, source_quote="근거")])
    assert code in _codes(validate_policy(p))


# --- 게시 전제조건 ----------------------------------------------------------


def test_원문_링크가_없으면_게시할_수_없다():
    """원문 링크 제공은 예외 없음 (PRD §7.6)."""
    p = _policy(source=Source())
    assert "NO_ORIGIN_URL" in _codes(validate_policy(p))


def test_자격요건이_없으면_게시할_수_없다():
    assert "NO_ELIGIBILITY_RULE" in _codes(validate_policy(_policy(eligibility=[])))


def test_draft_는_게시_전제조건을_적용하지_않는다():
    """배치 중간 산출물까지 막으면 파이프라인이 진행되지 않는다."""
    p = _policy(status="draft", eligibility=[], source=Source(), meta=Meta(title="t", category="job", authority_level="local"))
    assert _codes(validate_policy(p)) == set()


# --- UserProfile 파생값 -----------------------------------------------------


def test_만나이_계산_생일_전후():
    u = UserProfile(core=Core(birth_date=date(2001, 3, 14), region_code="41465"))
    assert u.age(date(2026, 3, 13)) == 24  # 생일 하루 전
    assert u.age(date(2026, 3, 14)) == 25  # 생일 당일
    assert u.age(date(2026, 9, 13)) == 25


def test_거주개월은_일자가_안_찼으면_보수적으로_센다():
    u = UserProfile(core=Core(birth_date=date(2001, 3, 14), region_code="41465", residence_start_date=date(2026, 5, 15)))
    assert u.residence_months(date(2026, 9, 14)) == 3  # 15일 전 → 3개월
    assert u.residence_months(date(2026, 9, 15)) == 4  # 15일 도달 → 4개월


def test_거주가_끊겼으면_미확인이다():
    """'계속하여 거주' 조건이라, 끊긴 거주는 0개월이 아니라 판단 불가다."""
    u = UserProfile(
        core=Core(birth_date=date(2001, 3, 14), region_code="41465", residence_start_date=date(2020, 1, 1), residence_continuous=False)
    )
    assert u.residence_months(date(2026, 9, 13)) is None


@pytest.mark.parametrize(
    "code,expected",
    [
        ("41465", ["00", "41", "41465"]),
        ("11680", ["00", "11", "11680"]),
        ("41", ["00", "41"]),
        ("00", ["00"]),
    ],
)
def test_지역_접두체인_확장(code, expected):
    assert region_chain(code) == expected


def test_미확인_필드는_None_으로_구분된다():
    """False(아니오)와 None(안 물어봄)을 섞으면 역질문이 사라진다."""
    u = UserProfile(core=Core(birth_date=date(2001, 3, 14), region_code="41465"))
    on = date(2026, 9, 13)
    assert u.resolve("similar_program_participation_2y", on) is None
    u.answers["similar_program_participation_2y"] = False
    assert u.resolve("similar_program_participation_2y", on) is False


# --- US-06: 신뢰도 · 근거 고지 ----------------------------------------------


def _result(**kw) -> JudgementResult:
    base = dict(policy_id="P", verdict="ELIGIBLE", confidence="CONFIRMED", origin_url="https://x/1")
    base.update(kw)
    return JudgementResult(**base)


def test_ESTIMATED_판정에는_담당부서_연락처가_필수():
    assert "MISSING_DEPT_CONTACT" in _codes(validate_judgement(_result(confidence="ESTIMATED")))


def test_NEEDS_REVIEW_판정에도_연락처가_필수():
    assert "MISSING_DEPT_CONTACT" in _codes(validate_judgement(_result(confidence="NEEDS_REVIEW")))


def test_연락처가_있으면_통과():
    assert validate_judgement(_result(confidence="ESTIMATED", dept_tel="031-324-0000")) == []


def test_원문_링크는_예외없이_필수():
    assert "MISSING_ORIGIN_URL" in _codes(validate_judgement(_result(origin_url=None)))


def test_시간충족_가능한_미충족항목은_충족예상일이_필요():
    r = _result(
        verdict="INELIGIBLE",
        unmatched=[
            UnmatchedRule(
                rule_id="RES", field="residence_months_continuous", user_value=4, required=6,
                source_quote="6개월 이상 계속하여 거주", time_satisfiable=True,
            )
        ],
    )
    assert "MISSING_SATISFIABLE_DATE" in _codes(validate_judgement(r))


def test_영구_충족불가면_날짜가_없어도_된다():
    """연령 상한 초과처럼 영원히 안 되는 조건에 날짜를 붙이면 거짓말이 된다."""
    r = _result(
        verdict="INELIGIBLE",
        unmatched=[
            UnmatchedRule(
                rule_id="AGE", field="age", user_value=36, required=[19, 34],
                source_quote="만 19세 이상 34세 이하", time_satisfiable=True,
                permanently_unsatisfiable=True,
            )
        ],
    )
    assert validate_judgement(r) == []


def test_NEEDS_INFO_인데_물어볼_게_없으면_거부():
    """사용자가 빠져나갈 길이 없는 상태다."""
    assert "NEEDS_INFO_WITHOUT_QUESTION" in _codes(validate_judgement(_result(verdict="NEEDS_INFO")))


def test_NEEDS_INFO_에_질문이_있으면_통과():
    r = _result(
        verdict="NEEDS_INFO",
        unknown=[
            UnknownRule(
                rule_id="SIM", field="similar_program_participation_2y",
                source_quote="최근 2년 이내 타 유사사업 참여자는 제외",
                question_template="최근 2년 이내 유사 청년지원사업에 참여한 경험이 있나요?",
            )
        ],
    )
    assert validate_judgement(r) == []


# --- 직렬화 왕복 ------------------------------------------------------------


def test_PolicySchema_직렬화_왕복():
    p = _policy()
    assert msgspec.json.decode(msgspec.json.encode(p), type=PolicySchema) == p


def test_알_수_없는_키는_디코딩에서_거부된다():
    """AI 출력에 오타 필드가 섞이면 조용히 무시되지 않고 즉시 드러나야 한다."""
    raw = (
        '{"policy_id":"X","meta":{"title":"t","category":"housing",'
        '"authority_level":"local"},"오타필드":1}'
    ).encode()
    with pytest.raises(msgspec.ValidationError):
        msgspec.json.decode(raw, type=PolicySchema)
