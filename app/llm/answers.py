"""역질문 답변 정규화 (AI-M3) — 사람이 말한 값을 룰 엔진이 비교할 수 있는 값으로.

사용자는 "120%", "한 백이십", "예", "혼자 살아요", "작년 5월에 이사" 처럼 답한다. 엔진은
정수·불리언·날짜만 비교한다. 이 둘 사이를 잇되, **모르면 None** 을 돌려준다 — 잘못 바꾼 값은
확신에 찬 오판정이 되고, None 은 그냥 다시 묻게 된다.

FE 폼은 타입이 잡혀 있어 이걸 거의 안 탄다. 주로 MCP 경로(Claude 가 사용자 말을 옮길 때)와
자유 입력에서 쓴다.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

# 필드별 기대 타입. rule_fields.json 의 known_fields 중 사용자가 답할 수 있는 것.
FIELD_TYPES: dict[str, str] = {
    "household_income_ratio_median": "percent",
    "household_size": "count",
    "residence_months_continuous": "months",
    "employment_months": "months",
    "similar_program_participation_2y": "bool",
    "education": "education",
    "employment_status": "employment_status",
    "marital_status": "marital_status",
}

_YES = {
    "예",
    "네",
    "응",
    "맞아",
    "맞아요",
    "있어",
    "있어요",
    "있음",
    "했어",
    "했어요",
    "y",
    "yes",
    "true",
    "o",
}
_NO = {
    "아니오",
    "아니요",
    "아뇨",
    "아니",
    "없어",
    "없어요",
    "없음",
    "안 했어",
    "안했어",
    "n",
    "no",
    "false",
    "x",
}

_EDU = {
    "middle_or_below": ["중졸", "중학교", "초등"],
    "high_school_enrolled": ["고등학생", "고교 재학", "고교재학", "고3", "고2", "고1"],
    "high_school_graduated": ["고졸", "고등학교 졸업", "고교 졸업"],
    "university_enrolled": ["대학생", "대학 재학", "재학 중", "재학중", "휴학"],
    "university_graduated": ["대졸", "대학 졸업", "졸업했", "학사"],
    "graduate_school": ["대학원", "석사", "박사"],
}
_EMP = {
    "employed": ["재직", "직장", "회사 다", "취업했", "근무 중", "근무중", "일하고"],
    "founder": ["창업", "사업", "자영업", "대표"],
    "student": ["학생", "재학"],
    "job_seeking": ["구직", "취준", "취업 준비", "취업준비"],
    "neet": ["쉬는", "쉬고", "무직", "백수"],
}
_MAR = {
    "married": ["기혼", "결혼했", "부부", "배우자 있"],
    "divorced": ["이혼"],
    "widowed": ["사별"],
    "single": ["미혼", "결혼 안", "결혼안", "싱글", "혼자"],
}

# 긴 말이 먼저 — "다섯" 안에 "섯"이, "넷"과 "네 명"이 섞이지 않게 순서를 둔다
_COUNT_WORDS: dict[str, int] = {
    "다섯": 5,
    "여섯": 6,
    "일곱": 7,
    "여덟": 8,
    "아홉": 9,
    "넷": 4,
    "네 명": 4,
    "네명": 4,
    "셋": 3,
    "세 명": 3,
    "세명": 3,
    "둘": 2,
    "두 명": 2,
    "두명": 2,
    "하나": 1,
    "한 명": 1,
    "한명": 1,
}

_NUM = re.compile(r"-?\d+(?:\.\d+)?")
_KOREAN_DIGITS = {
    "영": 0,
    "일": 1,
    "이": 2,
    "삼": 3,
    "사": 4,
    "오": 5,
    "육": 6,
    "칠": 7,
    "팔": 8,
    "구": 9,
}


def normalize_answer(field: str, raw: Any, *, today: date | None = None) -> Any:
    """필드에 맞는 타입으로 바꾼다. 못 바꾸면 None (= 다시 묻는다)."""
    kind = FIELD_TYPES.get(field)
    if kind is None:
        return None
    if raw is None:
        return None
    if isinstance(raw, bool):
        return raw if kind == "bool" else None
    if isinstance(raw, (int, float)) and kind in ("percent", "count", "months"):
        return int(raw)

    text = str(raw).strip()
    if not text:
        return None

    if kind == "bool":
        t = text.lower()
        if t in _YES or any(t.startswith(y) for y in ("있", "했", "예", "네")):
            return True
        if t in _NO or t.startswith(("없", "아니", "안 ", "안했")):
            return False
        return None

    if kind == "percent":
        return _first_int(text)

    if kind == "count":
        if "혼자" in text or "1인" in text or "독립" in text:
            return 1
        if (n := _first_int(text)) is not None:
            return n
        # "둘이", "셋이서", "네 명" — 사람 수를 세는 고유어
        for word, n in _COUNT_WORDS.items():
            if word in text:
                return n
        return None

    if kind == "months":
        return _months(text, today or date.today())

    table = {"education": _EDU, "employment_status": _EMP, "marital_status": _MAR}[kind]
    for code, keys in table.items():
        if any(k in text for k in keys):
            return code
    return text if text in table else None  # 코드값을 그대로 준 경우


# 개월 수 필드는 엔진이 answers 를 보지 않고 core 의 시작일에서 직접 센다 (UserProfile.resolve).
# 그래서 답은 개월이 아니라 시작일로 바꿔 core 에 넣어야 판정에 반영된다.
_MONTHS_TO_CORE_DATE = {
    "residence_months_continuous": "residence_start_date",
    "employment_months": "employment_start_date",
}


def apply_answers(
    profile: dict[str, Any], raw_answers: dict[str, Any], *, today: date | None = None
) -> dict[str, Any]:
    """자유 형식 답변을 프로필에 넣는다. 변환 실패는 빼서 엔진이 계속 UNKNOWN 으로 보게 한다.

    개월 수 필드 → core 의 시작일, 나머지 → answers. 입력 profile 은 바꾸지 않는다.
    """
    today = today or date.today()
    core = dict(profile.get("core", {}))
    answers = dict(profile.get("answers", {}))
    for field, raw in raw_answers.items():
        value = normalize_answer(field, raw, today=today)
        if value is None:
            continue
        if (core_key := _MONTHS_TO_CORE_DATE.get(field)) is not None:
            core[core_key] = _start_date(int(value), today).isoformat()
        else:
            answers[field] = value
    out = dict(profile)
    out["core"] = core
    if answers:
        out["answers"] = answers
    return out


def _start_date(months_ago: int, today: date) -> date:
    y, m = divmod(today.month - 1 - months_ago, 12)
    return date(today.year + y, m + 1, min(today.day, 28))


def _first_int(text: str) -> int | None:
    m = _NUM.search(text.replace(",", ""))
    if m:
        return int(float(m.group()))
    # "백이십" 같은 한글 숫자는 100 단위까지만 다룬다
    return _korean_number(text)


def _korean_number(text: str) -> int | None:
    t = re.sub(r"[^가-힣]", "", text)
    if not t:
        return None
    total, cur = 0, 0
    for ch in t:
        if ch in _KOREAN_DIGITS:
            cur = _KOREAN_DIGITS[ch]
        elif ch == "십":
            total += (cur or 1) * 10
            cur = 0
        elif ch == "백":
            total += (cur or 1) * 100
            cur = 0
        else:
            return None if total == 0 and cur == 0 else total + cur
    return total + cur if (total or cur) else None


def _months(text: str, today: date) -> int | None:
    """'6개월', '2년', '1년 반', '2024년 5월부터', '작년 5월', '2024-05-01' → 개월 수."""
    # "작년 5월", "올해 3월", "재작년 12월" — 연도를 상대어로 말하는 경우
    relative = {"재작년": -2, "작년": -1, "올해": 0, "금년": 0}
    for word, delta in relative.items():
        if m := re.search(word + r"\s*(\d{1,2})\s*월", text):
            text = f"{today.year + delta}년 {m.group(1)}월"
            break
    if m := re.search(r"(\d{4})[-./년]\s*(\d{1,2})(?:[-./월]\s*(\d{1,2}))?", text):
        y, mo = int(m.group(1)), int(m.group(2))
        d = int(m.group(3) or 1)
        try:
            start = date(y, mo, d)
        except ValueError:
            return None
        elapsed = (today.year - start.year) * 12 + (today.month - start.month)
        if today.day < start.day:
            elapsed -= 1
        return max(elapsed, 0)
    years = re.search(r"(\d+)\s*년", text)
    months = re.search(r"(\d+)\s*(개월|달)", text)
    if years or months:
        total = (int(years.group(1)) * 12 if years else 0) + (int(months.group(1)) if months else 0)
        if "반" in text and not months:
            total += 6
        return total
    return None
