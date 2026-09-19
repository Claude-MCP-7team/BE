"""C2 — 판정 결과 설명문.

룰 엔진이 확정한 JudgementResult 를 사용자가 읽을 문장으로 바꾼다. **판정을 바꾸지 않는다.**
설명은 결과에 이미 있는 것(어느 조건이 왜 안 맞는지, 언제부터 되는지, 무엇을 물어야 하는지)을
말로 풀 뿐이고, 결과에 없는 숫자·날짜·조건을 덧붙이지 않는다.

두 경로가 있다.
  template_explanation()  결정론 문장 조립. 항상 동작하고 비용이 없다. 기본값.
  explain_all()           LLM 으로 문장을 다듬는다. 결과에 없는 숫자가 하나라도 섞이면 그 항목은
                          템플릿으로 되돌린다 — 금액·기간·조건을 지어내는 것이 C2 의 유일한 실패
                          모드이고(PRD §23), 그건 문장이 어색한 것보다 나쁘다.

LLM 경로는 한 요청의 결과 전체를 한 번에 보낸다. 정책마다 호출하면 판정 3ms 뒤에 LLM 왕복이
N번 붙어 응답이 초 단위가 된다.
"""

from __future__ import annotations

import re
from typing import Any

import msgspec

from app.llm.client import LLM, LLMError, load_prompt
from app.schemas.judgement import JudgementResult, UnknownRule, UnmatchedRule

# 사용자에게 보이는 조건 이름. 룰의 field 는 계약(rule_fields.json)의 식별자라 그대로 못 보여준다.
FIELD_LABELS: dict[str, str] = {
    "age": "나이",
    "region_code": "거주 지역",
    "residence_months_continuous": "현재 지역 연속 거주 기간",
    "employment_months": "재직 기간",
    "education": "학력",
    "employment_status": "취업 상태",
    "marital_status": "혼인 상태",
    "household_size": "가구원 수",
    "household_income_ratio_median": "가구 소득(기준 중위소득 대비)",
    "similar_program_participation_2y": "최근 2년 유사 사업 참여",
    "received_policy_ids": "기수혜 이력",
}

_VALUE_LABELS: dict[str, str] = {
    "employed": "재직",
    "job_seeking": "구직 중",
    "student": "재학",
    "founder": "창업",
    "neet": "쉬는 중",
    "middle_or_below": "중졸 이하",
    "high_school_enrolled": "고교 재학",
    "high_school_graduated": "고졸",
    "university_enrolled": "대학 재학",
    "university_graduated": "대졸",
    "graduate_school": "대학원",
    "single": "미혼",
    "married": "기혼",
    "divorced": "이혼",
    "widowed": "사별",
}

_UNITS: dict[str, str] = {
    "age": "세",
    "residence_months_continuous": "개월",
    "employment_months": "개월",
    "household_size": "명",
    "household_income_ratio_median": "%",
}


# --- 템플릿 경로 ---------------------------------------------------------------


def template_explanation(result: JudgementResult, title: str) -> str:
    """결과만으로 만드는 설명. 결과에 없는 정보는 한 글자도 들어가지 않는다."""
    if result.verdict == "ELIGIBLE":
        return _eligible(result, title)
    if result.verdict == "NEEDS_INFO":
        return _needs_info(result, title)
    return _ineligible(result, title)


def _eligible(result: JudgementResult, title: str) -> str:
    n = len(result.matched)
    lines = [f"'{title}'의 확인된 조건 {n}개를 모두 충족해요."]
    if result.needs_dept_contact:
        lines.append(_dept_line(result, "다만 공고문이 두 가지로 읽히는 조건이 있어"))
    return " ".join(lines)


def _needs_info(result: JudgementResult, title: str) -> str:
    asks = [_question(u) for u in result.unknown]
    head = f"'{title}'은(는) 판단에 {len(asks)}가지 정보가 더 필요해요."
    if result.matched:
        head += f" 지금까지 확인된 조건 {len(result.matched)}개는 충족했어요."
    return head + " " + " ".join(f"({i}) {q}" for i, q in enumerate(asks, 1))


def _ineligible(result: JudgementResult, title: str) -> str:
    reasons = [_unmatched_sentence(u) for u in result.unmatched]
    head = f"'{title}'은(는) 지금 기준으로 조건 {len(reasons)}개가 맞지 않아요."
    parts = [head, *reasons]
    if result.matched:
        parts.append(f"다른 조건 {len(result.matched)}개는 충족했어요.")
    if result.unknown:
        parts.append(
            "그 밖에 아직 확인하지 못한 항목: "
            + ", ".join(FIELD_LABELS.get(u.field, u.field) for u in result.unknown)
            + "."
        )
    if result.needs_dept_contact:
        parts.append(_dept_line(result, "공고문이 두 가지로 읽히는 조건이 있어"))
    return " ".join(parts)


def _unmatched_sentence(u: UnmatchedRule) -> str:
    label = FIELD_LABELS.get(u.field, u.field)
    unit = u.unit if u.unit and u.unit not in ("months", "years") else _UNITS.get(u.field, "")
    mine = _fmt(u.user_value, u.field, unit)
    need = _fmt(u.required, u.field, unit)

    if u.field == "region_code":
        # 지역 룰의 인용문은 법정동 코드 목록이라 사용자에게 뜻이 없다. 문장만 남긴다.
        s = f"{label}: 대상 지역이 아니에요"
    else:
        s = f'{label}: 내 값 {mine}, 공고 기준 {need} ("{u.source_quote}")'

    if u.permanently_unsatisfiable:
        s += ". 이 조건은 시간이 지나도 충족되지 않아요"
    elif u.satisfiable_from:
        s += f". 이 조건은 {u.satisfiable_from}부터 충족돼요"
    return s + "."


def _question(u: UnknownRule) -> str:
    return u.question_template or f"{FIELD_LABELS.get(u.field, u.field)}을(를) 알려주세요."


def _dept_line(result: JudgementResult, lead: str) -> str:
    who = result.dept_name or "담당부서"
    tel = f"({result.dept_tel})" if result.dept_tel else ""
    return f"{lead} {who}{tel}에 확인을 권해요."


def _fmt(value: Any, field: str, unit: str) -> str:
    if isinstance(value, bool):
        return "예" if value else "아니오"
    if isinstance(value, list):
        if field in _UNITS and len(value) == 2:  # 숫자 필드의 between 은 구간으로
            lo, hi = value
            return f"{lo}{unit}" if lo == hi else f"{lo}~{hi}{unit}"
        return ", ".join(_fmt(v, field, unit) for v in value)
    if isinstance(value, (int, float)):
        return f"{value}{unit}"
    if isinstance(value, str):
        return _VALUE_LABELS.get(value, value)
    return "미입력"


# --- LLM 경로 -------------------------------------------------------------------

_DIGITS = re.compile(r"\d+")
MAX_EXPLANATION_CHARS = 600

EXPLAIN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "explanations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "policy_id": {"type": "string"},
                    "explanation": {"type": "string"},
                },
                "required": ["policy_id", "explanation"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["explanations"],
    "additionalProperties": False,
}


def explain_all(
    results: list[JudgementResult],
    titles: dict[str, str],
    llm: LLM | None,
) -> dict[str, str]:
    """policy_id → 설명. llm 이 None 이거나 실패하면 전부 템플릿."""
    drafts = {
        r.policy_id: template_explanation(r, titles.get(r.policy_id, r.policy_id)) for r in results
    }
    if llm is None or not results:
        return drafts

    material = {
        r.policy_id: {
            "title": titles.get(r.policy_id, r.policy_id),
            "result": msgspec.to_builtins(r),
            "draft": drafts[r.policy_id],
        }
        for r in results
    }
    try:
        data = llm.complete_json(
            system=load_prompt("c2_explain"),
            user=msgspec.json.encode(material).decode(),
            schema=EXPLAIN_SCHEMA,
        )
    except LLMError:
        return drafts

    out = dict(drafts)
    for item in data.get("explanations") or []:
        pid, text = item.get("policy_id"), (item.get("explanation") or "").strip()
        if pid in out and _grounded(text, material[pid]):
            out[pid] = text
    return out


def _grounded(text: str, material: dict[str, Any]) -> bool:
    """설명 속 모든 숫자가 결과·제목·초안 어딘가에 있어야 한다. 아니면 지어낸 것이다."""
    if not text or len(text) > MAX_EXPLANATION_CHARS:
        return False
    allowed = set(_DIGITS.findall(msgspec.json.encode(material).decode()))
    return all(n in allowed for n in _DIGITS.findall(text))
