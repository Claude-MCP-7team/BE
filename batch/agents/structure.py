"""A2 구조화 — 공고문 텍스트를 PolicySchema 에 병합한다.

두 함수로 나뉜다.
  structure_policy()  텍스트 조립 → LLM 호출 → merge()
  merge()             LLM 출력(dict) + 원문 → 검증·병합 (순수 함수, 네트워크 없음)

검증 순서와 이유:
  1. 인용문 원문 대조 — 원문에 없는 인용문이 붙은 항목은 그 자리에서 버린다. 모델이
     문장을 다듬거나 지어낸 경우이며, 이 검사를 통과한 인용문만 사용자 화면에 나간다.
  2. 기존 룰과의 불일치 — API 코드가 만든 룰과 텍스트가 다르게 말하면 어느 쪽도 믿지
     않고 needs_review 로 보낸다. 한쪽을 골라 확정하면 틀렸을 때 아무도 모른다.
  3. validate_policy — BE 밸리데이터를 룰 1건 단위로 돌려 떨어지는 룰만 뺀다. 정책
     전체를 거부하면 정상 룰까지 사라지고, 그건 '조용한 누락'이다.

버린 것은 전부 A2Report 에 남는다. 리포트가 비어 있는데 룰이 적으면 그건 모델이
아니라 공고문이 조용한 것이다.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any

import msgspec

from app.llm.client import LLM, load_prompt
from app.schemas.enums import TIME_SATISFIABLE_FIELDS, Confidence
from app.schemas.policy import (
    Benefit,
    Conflict,
    Dept,
    Document,
    Meta,
    Period,
    PolicySchema,
    Rule,
)
from app.schemas.validate import SchemaViolation, validate_policy
from batch.agents.contract import A2_OUTPUT_SCHEMA, STRUCTURABLE_FIELDS
from batch.agents.questions import ASKABLE_FIELDS, question_for
from batch.agents.text import Record, assemble_text, describe_known_rules, quote_found

BASIS = "A2 공고문 구조화"
# 규칙으로 옮길 수 없는 조건이 하나라도 확인되면 needs_review_fields 에 남기는 표식.
# 엔진이 평가하지 못하는 조건이 있는 정책은 ELIGIBLE 이어도 '확인 필요' 여야 한다.
UNREPRESENTABLE_MARKER = "unrepresentable_conditions"

_LIST_OPS = frozenset({"between", "in", "not_in"})
_NUMERIC_OPS = frozenset({">=", "<="})


@dataclass
class Rejected:
    path: str
    code: str
    message: str
    source_quote: str = ""


@dataclass
class A2Report:
    policy_id: str
    text_chars: int = 0
    proposed_conditions: int = 0
    accepted_conditions: int = 0
    rejected: list[Rejected] = field(default_factory=list)
    disagreements: list[str] = field(default_factory=list)
    unrepresentable: list[dict[str, str]] = field(default_factory=list)
    accepted_documents: int = 0
    accepted_conflicts: int = 0
    usage: dict[str, int] = field(default_factory=dict)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def structure_policy(
    base: PolicySchema,
    rec: Record,
    llm: LLM,
    *,
    announcement: str | None = None,
    system_prompt: str | None = None,
) -> tuple[PolicySchema, A2Report]:
    """정책 1건을 구조화한다. 텍스트가 없으면 호출 없이 base 를 그대로 돌려준다."""
    text = assemble_text(rec, announcement=announcement)
    report = A2Report(policy_id=base.policy_id, text_chars=len(text))
    if not text:
        return base, report

    known = describe_known_rules(base)
    user = f"{text}\n\n{known}" if known else text
    data = llm.complete_json(
        system=system_prompt or load_prompt("a2_structure"),
        user=user,
        schema=A2_OUTPUT_SCHEMA,
    )
    report.usage = dict(getattr(llm, "last_usage", {}) or {})
    return merge(base, data, text, report=report)


def merge(
    base: PolicySchema,
    data: dict[str, Any],
    text: str,
    *,
    report: A2Report | None = None,
) -> tuple[PolicySchema, A2Report]:
    """LLM 출력을 검증해 base 에 병합한다. base 는 바꾸지 않는다."""
    report = report or A2Report(policy_id=base.policy_id, text_chars=len(text))
    review: list[str] = list(base.quality.needs_review_fields)
    existing = {r.field: r for r in [*base.eligibility, *base.exclusions]}

    def verified(quote: Any, path: str) -> bool:
        if isinstance(quote, str) and quote_found(text, quote):
            return True
        report.rejected.append(
            Rejected(
                path=path,
                code="QUOTE_NOT_VERBATIM",
                message="인용문을 원문에서 찾을 수 없어 버렸습니다",
                source_quote=str(quote or ""),
            )
        )
        return False

    # --- 조건 -----------------------------------------------------------------
    eligibility = list(base.eligibility)
    exclusions = list(base.exclusions)
    seen: set[tuple[str, str, str]] = set()
    conditions = data.get("conditions") or []
    report.proposed_conditions = len(conditions)

    for i, c in enumerate(conditions):
        path = f"conditions[{i}]"
        fld = c.get("field")
        if fld not in STRUCTURABLE_FIELDS:
            report.rejected.append(Rejected(path, "UNKNOWN_FIELD", f"'{fld}' 는 만들 수 없는 필드"))
            continue
        if not verified(c.get("source_quote"), path):
            review.append(fld)
            continue

        value = _pick_value(c)
        if value is None:
            report.rejected.append(
                Rejected(path, "BAD_VALUE_SHAPE", "op 에 맞는 value 칸이 비어 있음")
            )
            review.append(fld)
            continue

        key = (fld, str(c.get("op")), repr(value))
        if key in seen:
            continue  # 같은 룰을 두 번 낸 것은 오류가 아니다
        seen.add(key)

        if (prev := existing.get(fld)) is not None:
            if prev.op == c.get("op") and prev.value == value:
                continue  # API 코드와 일치 — 이미 있는 룰을 그대로 둔다
            report.disagreements.append(
                f"{fld}: API {prev.op} {prev.value!r} vs 텍스트 {c.get('op')} {value!r}"
            )
            review.append(fld)
            continue

        ambiguous = bool(c.get("ambiguous"))
        confidence: Confidence = c.get("confidence") or "CONFIRMED"
        if ambiguous and confidence == "CONFIRMED":
            confidence = "ESTIMATED"

        rule = Rule(
            rule_id=f"{base.policy_id}:a2:{fld}:{i}",
            field=fld,
            op=c["op"],
            value=value,
            source_quote=c["source_quote"],
            unit=c.get("unit") or None,
            basis=BASIS,
            time_satisfiable=bool(c.get("time_satisfiable")) and fld in TIME_SATISFIABLE_FIELDS,
            ambiguous=ambiguous,
            askable=fld in ASKABLE_FIELDS,
            question_template=question_for(fld, c.get("question_template")),
            confidence=confidence,
        )
        if violations := _rule_violations(base, rule):
            for v in violations:
                report.rejected.append(Rejected(path, v.code, v.message, rule.source_quote))
            review.append(fld)
            continue

        (exclusions if c.get("kind") == "exclusion" else eligibility).append(rule)
        existing[fld] = rule
        report.accepted_conditions += 1

    # --- 규칙으로 못 옮긴 조건 ------------------------------------------------
    for i, u in enumerate(data.get("unrepresentable_conditions") or []):
        if verified(u.get("source_quote"), f"unrepresentable_conditions[{i}]"):
            report.unrepresentable.append(
                {
                    "summary": str(u.get("summary") or ""),
                    "source_quote": str(u["source_quote"]),
                    "reason": str(u.get("reason") or ""),
                }
            )
    if report.unrepresentable:
        review.append(UNREPRESENTABLE_MARKER)

    # --- 혜택 · 기간 · 서류 · 상충 · 담당부서 ---------------------------------
    benefit = _merge_benefit(base.benefit, data.get("benefit") or {}, verified)
    period = _merge_period(base.period, data.get("period") or {}, verified)

    documents = list(base.documents)
    names = {d.name for d in documents}
    for i, d in enumerate(data.get("documents") or []):
        name = str(d.get("name") or "").strip()
        if not name or name in names:
            continue
        if verified(d.get("source_quote"), f"documents[{i}]"):
            documents.append(
                Document(name=name, issuer=d.get("issuer") or None, source_quote=d["source_quote"])
            )
            names.add(name)
            report.accepted_documents += 1

    conflicts = list(base.conflicts)
    for i, c in enumerate(data.get("conflicts") or []):
        path = f"conflicts[{i}]"
        if not verified(c.get("source_quote"), path):
            continue
        conflict = Conflict(
            type=c["type"],
            source_quote=c["source_quote"],
            target_policy_name=c.get("target_policy_name") or None,
            target_category=c.get("target_category") or None,
            target_authority=c.get("target_authority") or None,
            confidence=c.get("confidence") or "ESTIMATED",
        )
        if violations := _conflict_violations(base, conflict):
            for v in violations:
                report.rejected.append(Rejected(path, v.code, v.message, conflict.source_quote))
            continue
        conflicts.append(conflict)
        report.accepted_conflicts += 1

    meta = _merge_dept(base.meta, data.get("dept") or {}, verified)

    # 텍스트로 확정(CONFIRMED)된 필드는 더 이상 검토 대상이 아니다
    confirmed = {r.field for r in [*eligibility, *exclusions] if r.confidence == "CONFIRMED"}
    disagreed = {d.split(":", 1)[0] for d in report.disagreements}
    review_final = [f for f in _dedupe(review) if f not in confirmed or f in disagreed]

    quality = msgspec.structs.replace(
        base.quality,
        parse_confidence=(
            report.accepted_conditions / report.proposed_conditions
            if report.proposed_conditions
            else 0.0
        ),
        needs_review_fields=review_final,
    )

    merged = msgspec.structs.replace(
        base,
        meta=meta,
        benefit=benefit,
        period=period,
        eligibility=eligibility,
        exclusions=exclusions,
        conflicts=conflicts,
        documents=documents,
        quality=quality,
    )
    return merged, report


# --- 부속 ---------------------------------------------------------------------


def _pick_value(c: dict[str, Any]) -> Any:
    """op 에 맞는 value 칸 하나를 고른다. 정수로 떨어지는 실수는 정수로 바꾼다."""
    op = c.get("op")
    if op in _LIST_OPS:
        lst = c.get("value_list")
        if not isinstance(lst, list) or not lst:
            return None
        return [_int_if_whole(v) for v in lst]
    if op in _NUMERIC_OPS:
        n = c.get("value_number")
        return _int_if_whole(n) if isinstance(n, (int, float)) and not isinstance(n, bool) else None
    # "==" : bool > text > number 순으로 채워진 칸을 쓴다
    if isinstance(c.get("value_bool"), bool):
        return c["value_bool"]
    if isinstance(c.get("value_text"), str) and c["value_text"].strip():
        return c["value_text"].strip()
    n = c.get("value_number")
    if isinstance(n, (int, float)) and not isinstance(n, bool):
        return _int_if_whole(n)
    return None


def _int_if_whole(v: Any) -> Any:
    if isinstance(v, float) and v.is_integer():
        return int(v)
    return v


def _rule_violations(base: PolicySchema, rule: Rule) -> list[SchemaViolation]:
    """BE 밸리데이터를 룰 1건에만 적용한다. status=draft 라 정책 수준 검사는 빠진다."""
    probe = PolicySchema(policy_id=base.policy_id, meta=base.meta, eligibility=[rule])
    return validate_policy(probe)


def _conflict_violations(base: PolicySchema, conflict: Conflict) -> list[SchemaViolation]:
    probe = PolicySchema(policy_id=base.policy_id, meta=base.meta, conflicts=[conflict])
    return validate_policy(probe)


def _merge_benefit(base: Benefit, b: dict[str, Any], verified: Any) -> Benefit:
    # API 가 금액을 이미 줬으면 그것이 권위다
    if base.amount_krw is not None or base.estimated_total_krw is not None:
        return base
    has_any = any(b.get(k) is not None for k in ("type", "amount_krw", "duration_months"))
    if not has_any:
        return base
    if b.get("amount_krw") is not None and not verified(b.get("source_quote"), "benefit"):
        return base  # 금액은 인용 없이는 받지 않는다. 유형만 있는 경우는 인용을 요구하지 않는다
    return Benefit(
        type=b.get("type") or base.type,
        amount_krw=_int_or_none(b.get("amount_krw")),
        duration_months=_int_or_none(b.get("duration_months")),
        estimated_total_krw=_int_or_none(b.get("estimated_total_krw")),
        amount_confidence=b.get("amount_confidence") or "ESTIMATED",
    )


def _merge_period(base: Period, p: dict[str, Any], verified: Any) -> Period:
    if base.apply_start or base.apply_end or base.is_rolling:
        return base  # API 신청기간이 권위
    start, end = _iso_or_none(p.get("apply_start")), _iso_or_none(p.get("apply_end"))
    rolling = bool(p.get("is_rolling"))
    if not (start or end or rolling):
        return base
    if not verified(p.get("source_quote"), "period"):
        return base
    return Period(apply_start=start, apply_end=end, is_rolling=rolling)


def _merge_dept(base: Meta, d: dict[str, Any], verified: Any) -> Meta:
    if base.dept.name and base.dept.tel:
        return base  # API 값이 권위
    tel = (d.get("tel") or "").strip() or None
    name = (d.get("name") or "").strip() or None
    if tel is None and name is None:
        return base
    # 전화번호는 NEEDS_REVIEW 판정에 병기되어 사용자가 실제로 거는 번호다. 인용 없이는 받지 않는다.
    if tel and not verified(d.get("source_quote"), "dept"):
        tel = None
    return msgspec.structs.replace(
        base, dept=Dept(name=base.dept.name or name, tel=base.dept.tel or tel)
    )


def _iso_or_none(v: Any) -> str | None:
    if not isinstance(v, str) or not v.strip():
        return None
    try:
        return date.fromisoformat(v.strip()).isoformat()
    except ValueError:
        return None


def _int_or_none(v: Any) -> int | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return int(v)


def _dedupe(items: list[str]) -> list[str]:
    out: list[str] = []
    for x in items:
        if x not in out:
            out.append(x)
    return out
