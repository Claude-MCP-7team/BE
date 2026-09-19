"""역질문 큐 생성 (C1 레이어의 결정론 부분).

AI 역할이 만드는 것은 '질문 문장'이고, 여기서 하는 것은 '무엇을 몇 개 물을지'다.
후자는 세는 문제라 LLM 이 아니라 코드가 한다.

규칙 (PRD §7.3)
  1. 전 정책의 미확인 필드를 필드 단위로 집계한다
  2. 같은 필드의 질문은 1개로 병합한다 (중복 질문 금지 — G3 게이트: 0건)
  3. 답하면 판정이 끝나는 정책 수 기준으로 내림차순 정렬한다
  4. 상위 10개만 제시한다
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from dataclasses import field as dc_field
from datetime import date

from app.engine.compile import Snapshot
from app.engine.evaluate import Verdicts
from app.engine.rules import Outcome, evaluate_rule
from app.schemas.enums import LIST_VALUED_FIELDS
from app.schemas.question import Question, QuestionQueue
from app.schemas.user import UserProfile

MAX_QUESTIONS = 10

# 필드별 답변 형태. 예/아니오나 선택지로 좁혀야 사용자가 빨리 답한다.
_ANSWER_TYPES: dict[str, str] = {
    "similar_program_participation_2y": "boolean",
    "household_income_ratio_median": "number",
    "household_size": "number",
    "employment_months": "number",
    "residence_months_continuous": "number",
    "education": "choice",
    "employment_status": "choice",
    "marital_status": "choice",
}

_CHOICES: dict[str, list[str]] = {
    "education": [
        "middle_or_below",
        "high_school_enrolled",
        "high_school_graduated",
        "university_enrolled",
        "university_graduated",
        "graduate_school",
    ],
    "employment_status": ["employed", "job_seeking", "student", "founder", "neet"],
    "marital_status": ["single", "married", "divorced", "widowed"],
}


@dataclass(slots=True)
class _FieldTally:
    field: str
    resolves: int = 0  # 이 필드가 마지막 남은 미확인인 정책 수
    affects: int = 0  # 이 필드를 참조하는 미확인 정책 수
    templates: Counter[str] = dc_field(default_factory=Counter)
    quotes: Counter[str] = dc_field(default_factory=Counter)
    policy_ids: list[str] = dc_field(default_factory=list)


def build_queue(
    snapshot: Snapshot,
    profile: UserProfile,
    today: date,
    verdicts: Verdicts,
    limit: int = MAX_QUESTIONS,
) -> QuestionQueue:
    """NEEDS_INFO 정책들의 미확인 필드를 모아 질문 큐를 만든다."""
    tallies: dict[str, _FieldTally] = {}
    needs_info_count = 0

    for policy_index in range(snapshot.size):
        if not verdicts.needs_info[policy_index]:
            continue
        needs_info_count += 1

        unknown_fields = _unknown_fields(snapshot, profile, today, policy_index)
        policy_id = snapshot.policy_ids[policy_index]
        # 미확인이 1개뿐인 정책은, 그 하나에 답하면 판정이 끝난다
        is_last_one = len(unknown_fields) == 1

        for fld, template, quote in unknown_fields:
            tally = tallies.setdefault(fld, _FieldTally(field=fld))
            tally.affects += 1
            if is_last_one:
                tally.resolves += 1
            if template:
                tally.templates[template] += 1
            if quote:
                tally.quotes[quote] += 1
            if policy_id not in tally.policy_ids:
                tally.policy_ids.append(policy_id)

    ordered = sorted(tallies.values(), key=lambda t: (-t.resolves, -t.affects, t.field))

    return QuestionQueue(
        snapshot_version=snapshot.version,
        questions=[_to_question(t) for t in ordered[:limit]],
        total_unresolved_fields=len(tallies),
        needs_info_policies=needs_info_count,
    )


def _unknown_fields(
    snapshot: Snapshot, profile: UserProfile, today: date, policy_index: int
) -> list[tuple[str, str | None, str]]:
    """한 정책에서 미확인인 (필드, 질문템플릿, 근거구절) 목록. 필드 기준 중복 제거."""
    seen: dict[str, tuple[str, str | None, str]] = {}
    for ref_index in snapshot.rules_by_policy[policy_index]:
        rule = snapshot.rule_refs[ref_index].rule
        if rule.field in seen:
            continue
        if evaluate_rule(rule, profile.resolve(rule.field, today)) is Outcome.UNKNOWN:
            seen[rule.field] = (rule.field, rule.question_template, rule.source_quote)
    return list(seen.values())


def _to_question(tally: _FieldTally) -> Question:
    return Question(
        field=tally.field,
        text=_merge_text(tally),
        resolves=tally.resolves,
        affects=tally.affects,
        answer_type=_ANSWER_TYPES.get(tally.field, "boolean"),
        choices=_CHOICES.get(tally.field, []),
        source_quote=tally.quotes.most_common(1)[0][0] if tally.quotes else "",
        source_policy_ids=tally.policy_ids[:20],
    )


def _merge_text(tally: _FieldTally) -> str:
    """같은 필드의 여러 질문 문장을 하나로 고른다.

    가장 많이 쓰인 문장을 택하고, 동점이면 짧은 쪽을 쓴다.
    여러 공고가 같은 조건을 다르게 적었을 때, 가장 흔한 표현이 보통 가장 일반적이다.
    """
    if tally.templates:
        top = max(tally.templates.values())
        candidates: list[str] = [t for t, c in tally.templates.items() if c == top]
        return min(candidates, key=len)
    return _fallback_text(tally.field)


def _fallback_text(field: str) -> str:
    """AI 가 질문 템플릿을 못 만든 필드의 최소 문구.

    공고문 용어가 아니라 일상어로 쓴다 (PRD §7.3).
    """
    return {
        "age": "만 나이가 어떻게 되시나요?",
        "residence_months_continuous": "현재 주소지에 언제부터 계속 살고 계신가요?",
        "employment_months": "지금 직장에서 몇 개월째 일하고 계신가요?",
        "employment_status": "현재 어떤 상태에 가장 가까우신가요?",
        "education": "최종 학력이 어떻게 되시나요?",
        "marital_status": "혼인 상태가 어떻게 되시나요?",
        "household_size": "함께 사는 가구원이 본인 포함 몇 명인가요?",
        "household_income_ratio_median": "가구 소득이 기준 중위소득의 몇 %인가요?",
        "similar_program_participation_2y": "최근 2년 이내 비슷한 청년지원사업에 참여한 적 있나요?",
        "received_policy_ids": "지금까지 받아본 청년정책이 있나요?",
    }.get(field, f"{field} 항목을 확인해 주세요.")


def unanswerable_policies(
    snapshot: Snapshot, profile: UserProfile, today: date, verdicts: Verdicts, field: str
) -> list[str]:
    """사용자가 '모르겠음'을 고른 필드 때문에 막히는 정책들.

    이 정책들은 NEEDS_REVIEW 로 두고 담당부서 연락처로 폴백한다 (PRD §7.3).
    """
    out = []
    for policy_index in range(snapshot.size):
        if not verdicts.needs_info[policy_index]:
            continue
        if any(f == field for f, _, _ in _unknown_fields(snapshot, profile, today, policy_index)):
            out.append(snapshot.policy_ids[policy_index])
    return out


__all__ = ["MAX_QUESTIONS", "QuestionQueue", "build_queue", "unanswerable_policies"]


# LIST_VALUED_FIELDS 는 답변 형태를 정할 때 참고한다 (목록형은 다중 선택)
for _f in LIST_VALUED_FIELDS:
    _ANSWER_TYPES.setdefault(_f, "choice")
