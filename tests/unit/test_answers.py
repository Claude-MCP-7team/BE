"""역질문 답변 정규화 — 사람 말을 엔진 값으로. 확신 없으면 None (다시 묻는다)."""

from __future__ import annotations

from datetime import date

from app.llm.answers import apply_answers, normalize_answer

TODAY = date(2026, 9, 21)


def test_퍼센트():
    f = "household_income_ratio_median"
    assert normalize_answer(f, "120%") == 120
    assert normalize_answer(f, "한 120 정도") == 120
    assert normalize_answer(f, "백이십") == 120
    assert normalize_answer(f, 150) == 150
    assert normalize_answer(f, "모르겠어요") is None


def test_가구원_수():
    f = "household_size"
    assert normalize_answer(f, "혼자 살아요") == 1
    assert normalize_answer(f, "1인 가구") == 1
    assert normalize_answer(f, "부모님이랑 셋이") == 3
    assert normalize_answer(f, "3명") == 3


def test_예_아니오():
    f = "similar_program_participation_2y"
    assert normalize_answer(f, "예") is True
    assert normalize_answer(f, "있어요") is True
    assert normalize_answer(f, "아니요") is False
    assert normalize_answer(f, "없음") is False
    assert normalize_answer(f, True) is True
    assert normalize_answer(f, "글쎄요") is None


def test_선택형():
    assert normalize_answer("education", "대학생이에요") == "university_enrolled"
    assert normalize_answer("education", "대졸") == "university_graduated"
    assert normalize_answer("education", "university_enrolled") == "university_enrolled"
    assert normalize_answer("employment_status", "회사 다녀요") == "employed"
    assert normalize_answer("employment_status", "취준 중") == "job_seeking"
    assert normalize_answer("marital_status", "결혼 안 했어요") == "single"
    assert normalize_answer("marital_status", "기혼") == "married"
    assert normalize_answer("marital_status", "이혼했어요") == "divorced"
    assert normalize_answer("education", "뭐라고요") is None


def test_기간은_개월로():
    f = "residence_months_continuous"
    assert normalize_answer(f, "6개월", today=TODAY) == 6
    assert normalize_answer(f, "2년", today=TODAY) == 24
    assert normalize_answer(f, "1년 반", today=TODAY) == 18
    assert normalize_answer(f, "2024년 5월부터", today=TODAY) == 28
    assert normalize_answer(f, "2026-05-15", today=TODAY) == 4
    assert normalize_answer(f, "작년 5월부터", today=TODAY) == 16
    assert normalize_answer(f, "올해 3월에 이사", today=TODAY) == 6
    assert normalize_answer(f, "오래 살았어요", today=TODAY) is None


def test_모르는_필드는_바꾸지_않는다():
    assert normalize_answer("age", "24") is None
    assert normalize_answer("region_code", "41465") is None


def test_apply_answers_는_개월_수를_core_시작일로_넣는다():
    profile = {"core": {"birth_date": "2002-03-01", "region_code": "41465"}}
    out = apply_answers(
        profile,
        {
            "household_income_ratio_median": "120%",
            "residence_months_continuous": "4개월",
            "similar_program_participation_2y": "없어요",
            "education": "모름",  # 변환 실패 → 빠진다
        },
        today=TODAY,
    )
    assert out["core"]["residence_start_date"] == "2026-05-21"
    assert out["answers"] == {
        "household_income_ratio_median": 120,
        "similar_program_participation_2y": False,
    }
    assert "education" not in out["answers"]
    assert profile == {"core": {"birth_date": "2002-03-01", "region_code": "41465"}}  # 입력 불변
