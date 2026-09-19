"""교차검증 — 두 번 읽은 결과가 다를 때 룰을 지우지 않고 NEEDS_REVIEW 로 내리는가."""

from __future__ import annotations

from batch.agents.crosscheck import cross_check
from batch.agents.structure import merge
from tests.unit.test_agents_structure import assemble_text, base_policy, cond, output, rec

TEXT = assemble_text(rec())


def structured(*conditions, **over):
    merged, _ = merge(base_policy(), output(conditions=list(conditions), **over), TEXT)
    return merged


def rules_of(p):
    return {r.field: r for r in [*p.eligibility, *p.exclusions]}


RES = cond()  # residence_months_continuous >= 6, CONFIRMED
INC = cond(
    field="household_income_ratio_median",
    op="<=",
    value_number=150,
    unit="%",
    source_quote="기준 중위소득 150% 이하",
)
INC_OTHER = cond(
    field="household_income_ratio_median",
    op="<=",
    value_number=100,
    unit="%",
    source_quote="기준 중위소득 150% 이하",  # 같은 문장을 다르게 읽은 상황
)
EXCL = cond(
    kind="exclusion",
    field="similar_program_participation_2y",
    op="==",
    value_number=None,
    value_bool=False,
    source_quote="최근 2년 이내 유사한 청년 지원사업에 참여한 자는 제외",
    time_satisfiable=False,
)


def test_완전히_같으면_AGREE_이고_아무것도_바뀌지_않는다():
    a, b = structured(RES, INC), structured(RES, INC)
    merged, report = cross_check(a, b)
    assert report.agree and report.agreed == 2
    assert merged.quality.cross_check == "AGREE"
    assert merged.eligibility == a.eligibility
    assert merged.quality.needs_review_fields == a.quality.needs_review_fields


def test_한쪽에만_있는_룰은_남기되_NEEDS_REVIEW_다():
    merged, report = cross_check(structured(RES, INC), structured(RES))
    r = rules_of(merged)
    assert r["household_income_ratio_median"].confidence == "NEEDS_REVIEW"
    assert r["residence_months_continuous"].confidence == "CONFIRMED"
    assert report.only_a == ["household_income_ratio_median <= 150"] and report.only_b == []
    assert "household_income_ratio_median" in merged.quality.needs_review_fields
    assert merged.quality.cross_check == "DISAGREE"


def test_B_에만_있는_룰도_들어오되_NEEDS_REVIEW_다():
    merged, report = cross_check(structured(RES), structured(RES, EXCL))
    assert report.only_b == ["similar_program_participation_2y == False"]
    excl = merged.exclusions[0]  # kind 도 B 의 것을 따른다
    assert excl.field == "similar_program_participation_2y"
    assert excl.confidence == "NEEDS_REVIEW"


def test_값이_다르면_A_를_남기되_NEEDS_REVIEW_와_ambiguous_다():
    merged, report = cross_check(structured(RES, INC), structured(RES, INC_OTHER))
    r = rules_of(merged)["household_income_ratio_median"]
    assert r.value == 150 and r.confidence == "NEEDS_REVIEW" and r.ambiguous
    assert report.value_disagreements == ["household_income_ratio_median: A <= 150 vs B <= 100"]
    assert [x.field for x in merged.eligibility].count("household_income_ratio_median") == 1


def test_confidence_는_둘_중_낮은_쪽을_따른다():
    a = structured(INC)
    b = structured(cond(**{**INC, "ambiguous": True}))  # B 는 ESTIMATED 로 읽었다
    merged, report = cross_check(a, b)
    assert report.agree
    assert rules_of(merged)["household_income_ratio_median"].confidence == "ESTIMATED"


def test_API_코드_룰은_비교하지_않는다():
    # 양쪽 다 A2 룰이 없으면 API 룰(age·region)만 있고 그대로 AGREE
    merged, report = cross_check(structured(), structured())
    assert report.agree and report.agreed == 0
    assert len(merged.eligibility) == len(base_policy().eligibility)


def test_서류와_상충은_합집합이다():
    doc = {
        "name": "주민등록등본",
        "issuer": None,
        "canonical_name": None,
        "source_quote": "주민등록등본 1부",
    }
    conflict = {
        "type": "category_overlap",
        "target_policy_name": None,
        "target_category": "housing",
        "target_benefit_type": "cash_monthly",
        "target_authority": None,
        "source_quote": "청년월세 한시 특별지원과 중복 수혜 불가",
        "confidence": "ESTIMATED",
    }
    a = structured(RES, documents=[doc])
    b = structured(RES, conflicts=[conflict])
    merged, report = cross_check(a, b)
    assert [d.name for d in merged.documents] == ["주민등록등본"]
    assert len(merged.conflicts) == 1 and report.conflicts_added_from_b == 1
    assert report.agree  # 서류·상충 차이는 불일치로 세지 않는다


def test_금액이_다르면_추정으로_내린다():
    b_amt = {
        "type": "cash_monthly",
        "amount_krw": 200000,
        "duration_months": 12,
        "estimated_total_krw": 2400000,
        "amount_confidence": "CONFIRMED",
        "source_quote": "월 20만원, 최대 12개월 지원",
    }
    a = structured(RES, benefit=b_amt)
    b = structured(RES, benefit={**b_amt, "estimated_total_krw": 1200000})
    merged, report = cross_check(a, b)
    assert merged.benefit.estimated_total_krw == 2400000
    assert merged.benefit.amount_confidence == "ESTIMATED"
    assert report.benefit_disagreement == "A 200000/2400000 vs B 200000/1200000"
