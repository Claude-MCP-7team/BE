"""역질문 기본 템플릿 — 필드별 1문장.

역질문은 정책별이 아니라 **필드별로 병합**된다 (app/engine/questions.py). 그래서 같은
필드에 정책마다 다른 문구가 붙으면 어느 하나가 임의로 선택된다. 모델이 정책 맥락에
맞는 문구를 주면 그것을 쓰되, 비어 있으면 여기 표준 문구로 채워 askable 룰이 검증
(ASKABLE_WITHOUT_QUESTION)에서 떨어지지 않게 한다.

문구 원칙 (마일스톤 §M3 AI): 예/아니오·숫자·날짜·선택형으로 답할 수 있어야 하고,
공고문 용어("유사사업")를 사용자 말("비슷한 지원")로 바꾼다.
"""

from __future__ import annotations

DEFAULT_QUESTION_TEMPLATES: dict[str, str] = {
    "residence_months_continuous": (
        "현재 사는 지역(시·군·구)에 언제부터 계속 살고 계신가요? (전입일)"
    ),
    "employment_months": "현재 직장에 입사한 날짜가 언제인가요?",
    "employment_status": "현재 취업 상태를 알려주세요. (재직 / 구직 중 / 재학 / 창업 / 쉬는 중)",
    "education": "최종 학력을 알려주세요. (고졸 / 대학 재학 / 대졸 / 대학원)",
    "marital_status": "현재 혼인 상태를 알려주세요. (미혼 / 기혼 / 이혼 / 사별)",
    "household_size": "함께 사는 가구원이 본인 포함 몇 명인가요?",
    "household_income_ratio_median": (
        "가구 소득이 기준 중위소득의 몇 % 정도인가요? (건강보험료 고지서로 확인할 수 있어요)"
    ),
    "similar_program_participation_2y": (
        "최근 2년 안에 비슷한 지원 사업에 참여한 적이 있나요? (예/아니오)"
    ),
}

# 사용자에게 물어서 알 수 있는 필드. age 는 생년월일에서 항상 계산되므로 묻지 않는다.
ASKABLE_FIELDS: frozenset[str] = frozenset(DEFAULT_QUESTION_TEMPLATES)


def question_for(field: str, proposed: str | None) -> str | None:
    """모델이 준 문구가 있으면 그것을, 없으면 표준 문구를. 물을 수 없는 필드면 None."""
    if field not in ASKABLE_FIELDS:
        return None
    text = (proposed or "").strip()
    return text or DEFAULT_QUESTION_TEMPLATES[field]
