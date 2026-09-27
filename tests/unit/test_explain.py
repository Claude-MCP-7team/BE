"""C2 설명문 — 템플릿 문장과 LLM 경로의 근거 검사.

여기서 보려는 것: 설명이 결과에 없는 숫자를 절대 들여오지 않는가, 판정마다 막다른 길 없이
다음 행동(언제부터·무엇을 물어야·누구에게 확인)이 들어가는가.
"""

from __future__ import annotations

from typing import Any

from app.llm.client import LLMError
from app.llm.explain import EXPLAIN_SCHEMA, explain_all, template_explanation
from app.schemas.judgement import JudgementResult, MatchedRule, UnknownRule, UnmatchedRule

TITLES = {"P": "용인시 청년 월세 지원"}


def matched(field="age", value=24):
    return MatchedRule(rule_id=f"P:{field}", field=field, user_value=value, source_quote="q")


def ineligible(**over: Any) -> JudgementResult:
    base: dict[str, Any] = {
        "policy_id": "P",
        "verdict": "INELIGIBLE",
        "confidence": "CONFIRMED",
        "matched": [matched()],
        "unmatched": [
            UnmatchedRule(
                rule_id="P:res",
                field="residence_months_continuous",
                user_value=4,
                required=6,
                unit="months",
                source_quote="6개월 이상 계속 거주",
                time_satisfiable=True,
                satisfiable_from="2026-11-15",
            )
        ],
        "origin_url": "https://example.kr",
    }
    base.update(over)
    return JudgementResult(**base)


# --- 템플릿 ---------------------------------------------------------------------


def test_미충족은_내_값과_기준과_근거와_충족일을_모두_말한다():
    text = template_explanation(ineligible(), TITLES["P"])
    assert "연속 거주 기간" in text
    assert "내 값 4개월" in text and "공고 기준 6개월" in text
    assert '"6개월 이상 계속 거주"' in text
    assert "2026-11-15부터 충족돼요" in text
    assert "다른 조건 1개는 충족했어요" in text  # 막다른 길이 아니다


def test_영구_미충족은_그렇다고_말한다():
    r = ineligible(
        unmatched=[
            UnmatchedRule(
                rule_id="P:age",
                field="age",
                user_value=36,
                required=[19, 34],
                source_quote="만 19세 이상 34세 이하",
                time_satisfiable=True,
                permanently_unsatisfiable=True,
            )
        ]
    )
    text = template_explanation(r, TITLES["P"])
    assert "내 값 36세, 공고 기준 19~34세" in text
    assert "시간이 지나도 충족되지 않아요" in text


def test_지역_미충족은_코드_목록을_보여주지_않는다():
    r = ineligible(
        unmatched=[
            UnmatchedRule(
                rule_id="P:reg",
                field="region_code",
                user_value=["00", "41", "41460"],
                required=["41820"],
                source_quote="시행 지역 zipCd: 41820",
            )
        ]
    )
    text = template_explanation(r, TITLES["P"])
    assert "대상 지역이 아니에요" in text
    assert "41460" not in text and "zipCd" not in text  # 코드 목록은 사용자에게 뜻이 없다


def test_상하한이_같은_구간은_한_값으로_쓴다():
    r = ineligible(
        unmatched=[
            UnmatchedRule(
                rule_id="P:age",
                field="age",
                user_value=23,
                required=[24, 24],
                source_quote="24세 청년",
                time_satisfiable=True,
                satisfiable_from="2026-10-15",
            )
        ]
    )
    text = template_explanation(r, TITLES["P"])
    assert "내 값 23세, 공고 기준 24세" in text and "24~24" not in text


def test_확인_필요는_질문을_그대로_나열한다():
    r = ineligible(
        verdict="NEEDS_INFO",
        unmatched=[],
        unknown=[
            UnknownRule(
                rule_id="P:inc",
                field="household_income_ratio_median",
                source_quote="중위소득 150% 이하",
                question_template="가구 소득이 기준 중위소득의 몇 %인가요?",
            )
        ],
    )
    text = template_explanation(r, TITLES["P"])
    assert "1가지 정보가 더 필요해요" in text
    assert "(1) 가구 소득이 기준 중위소득의 몇 %인가요?" in text


def test_추정_판정에는_담당부서_연락처가_붙는다():
    r = ineligible(
        verdict="ELIGIBLE",
        unmatched=[],
        confidence="ESTIMATED",
        dept_name="청년정책과",
        dept_tel="031-000-0000",
    )
    text = template_explanation(r, TITLES["P"])
    assert "조건 1개를 모두 충족해요" in text
    assert "청년정책과(031-000-0000)에 확인을 권해요" in text


def test_사용자_값_라벨은_한국어다():
    r = ineligible(
        unmatched=[
            UnmatchedRule(
                rule_id="P:emp",
                field="employment_status",
                user_value="student",
                required=["employed"],
                source_quote="재직자",
            )
        ]
    )
    text = template_explanation(r, TITLES["P"])
    assert "내 값 재학, 공고 기준 재직" in text


# --- LLM 경로 -------------------------------------------------------------------


class FakeLLM:
    def __init__(self, data: Any):
        self.data = data
        self.calls = 0

    def complete_json(self, *, system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
        self.calls += 1
        assert schema is EXPLAIN_SCHEMA
        if isinstance(self.data, Exception):
            raise self.data
        return self.data


def test_llm_없으면_템플릿이다():
    r = ineligible()
    out = explain_all([r], TITLES, None)
    assert out == {"P": template_explanation(r, TITLES["P"])}


def test_llm_문장에_결과에_없는_숫자가_있으면_템플릿으로_되돌린다():
    r = ineligible()
    invented = "거주 기간이 4개월이라 12개월 조건에 못 미쳐요."  # 12 는 어디에도 없다
    out = explain_all(
        [r], TITLES, FakeLLM({"explanations": [{"policy_id": "P", "explanation": invented}]})
    )
    assert out["P"] == template_explanation(r, TITLES["P"])


def test_llm_문장이_결과_안의_숫자만_쓰면_채택한다():
    r = ineligible()
    ok = "지금은 4개월째 살고 계셔서 6개월 조건에 못 미치지만, 2026-11-15부터는 신청할 수 있어요."
    out = explain_all(
        [r], TITLES, FakeLLM({"explanations": [{"policy_id": "P", "explanation": ok}]})
    )
    assert out["P"] == ok


def test_llm_이_모르는_정책이나_빈_문장은_무시한다():
    r = ineligible()
    fake = FakeLLM(
        {
            "explanations": [
                {"policy_id": "X", "explanation": "..."},
                {"policy_id": "P", "explanation": ""},
            ]
        }
    )
    out = explain_all([r], TITLES, fake)
    assert out["P"] == template_explanation(r, TITLES["P"])


def test_llm_실패는_템플릿으로_조용히_떨어진다():
    r = ineligible()
    out = explain_all([r], TITLES, FakeLLM(LLMError("boom")))
    assert out["P"] == template_explanation(r, TITLES["P"])


def test_결과가_없으면_호출하지_않는다():
    fake = FakeLLM({"explanations": []})
    assert explain_all([], TITLES, fake) == {} and fake.calls == 0
