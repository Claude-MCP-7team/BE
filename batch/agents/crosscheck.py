"""교차검증 (AI-M4) — 같은 공고를 두 번 구조화한 결과를 맞춰 본다.

A 와 B 는 같은 base(normalize 결과)에서 서로 다른 모델(또는 다른 effort)로 merge 한 것이다.
API 코드 룰은 양쪽에 똑같이 들어 있으므로 비교 대상은 **A2 가 만든 룰**(basis == BASIS)뿐이다.

판정 규칙 — 어느 경우에도 룰을 조용히 지우지 않는다:
  같은 (field, op, value) 가 양쪽에 있다
      → 유지. confidence 는 둘 중 낮은 쪽.
  한쪽에만 있다
      → 유지하되 NEEDS_REVIEW. 한 번만 본 조건은 있을 수도 없을 수도 있다. 지우면 조용한
        누락이고 CONFIRMED 로 두면 근거 없는 확정이다. 담당부서 확인이 붙는 값이 맞다.
  같은 field 인데 값이 다르다
      → A 쪽을 유지하되 NEEDS_REVIEW + ambiguous. 두 모델이 같은 문장을 다르게 읽었다는 것은
        문장이 애매하다는 뜻이다.
  서류·상충
      → 합집합. 둘 다 인용문 대조를 통과한 것들이고, 더 많이 아는 것이 계획·조합에 손해가 아니다.
  혜택 금액이 다르다
      → A 유지, amount_confidence=ESTIMATED.

quality.cross_check 는 위 중 '유지' 외의 일이 하나라도 있으면 DISAGREE, 없으면 AGREE.
needs_review_fields 에는 NEEDS_REVIEW 로 내린 필드가 들어간다.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import msgspec

from app.schemas.policy import Conflict, PolicySchema, Rule
from batch.agents.structure import BASIS

_RANK = {"CONFIRMED": 0, "ESTIMATED": 1, "NEEDS_REVIEW": 2}


@dataclass
class CrossCheckReport:
    policy_id: str
    agreed: int = 0
    only_a: list[str] = field(default_factory=list)
    only_b: list[str] = field(default_factory=list)
    value_disagreements: list[str] = field(default_factory=list)
    benefit_disagreement: str | None = None
    documents_added_from_b: int = 0
    conflicts_added_from_b: int = 0

    @property
    def agree(self) -> bool:
        return not (
            self.only_a or self.only_b or self.value_disagreements or self.benefit_disagreement
        )


def cross_check(a: PolicySchema, b: PolicySchema) -> tuple[PolicySchema, CrossCheckReport]:
    """A 를 기준으로 B 와 맞춰 본 PolicySchema 와 리포트를 돌려준다. 입력은 바꾸지 않는다."""
    if a.policy_id != b.policy_id:
        raise ValueError(f"다른 정책을 교차검증할 수 없습니다: {a.policy_id} vs {b.policy_id}")
    report = CrossCheckReport(policy_id=a.policy_id)
    review = list(a.quality.needs_review_fields)

    b_rules = {_sig(r): r for r in _a2_rules(b)}
    b_fields = {r.field for r in b_rules.values()}
    seen_sigs: set[tuple[str, str, str]] = set()

    def reconcile(rules: list[Rule]) -> list[Rule]:
        out: list[Rule] = []
        for r in rules:
            if r.basis != BASIS:
                out.append(r)  # API 코드 룰은 양쪽이 같다
                continue
            sig = _sig(r)
            seen_sigs.add(sig)
            if (twin := b_rules.get(sig)) is not None:
                report.agreed += 1
                worse = max(r.confidence, twin.confidence, key=lambda c: _RANK[c])
                out.append(msgspec.structs.replace(r, confidence=worse))
            elif r.field in b_fields:
                other = next(x for x in b_rules.values() if x.field == r.field)
                report.value_disagreements.append(
                    f"{r.field}: A {r.op} {r.value!r} vs B {other.op} {other.value!r}"
                )
                review.append(r.field)
                out.append(msgspec.structs.replace(r, confidence="NEEDS_REVIEW", ambiguous=True))
            else:
                report.only_a.append(f"{r.field} {r.op} {r.value!r}")
                review.append(r.field)
                out.append(msgspec.structs.replace(r, confidence="NEEDS_REVIEW"))
        return out

    eligibility = reconcile(a.eligibility)
    exclusions = reconcile(a.exclusions)

    # B 에만 있는 룰 — 값 불일치로 이미 A 쪽에 잡힌 필드는 제외
    a_fields = {r.field for r in _a2_rules(a)}
    for sig, r in b_rules.items():
        if sig in seen_sigs or r.field in a_fields:
            continue
        report.only_b.append(f"{r.field} {r.op} {r.value!r}")
        review.append(r.field)
        target = exclusions if _is_exclusion(b, r) else eligibility
        target.append(msgspec.structs.replace(r, confidence="NEEDS_REVIEW"))

    documents = list(a.documents)
    names = {d.name for d in documents}
    for d in b.documents:
        if d.name not in names:
            documents.append(d)
            names.add(d.name)
            report.documents_added_from_b += 1

    conflicts = list(a.conflicts)
    keys = {_conflict_key(c) for c in conflicts}
    for c in b.conflicts:
        if _conflict_key(c) not in keys:
            conflicts.append(c)
            keys.add(_conflict_key(c))
            report.conflicts_added_from_b += 1

    benefit = a.benefit
    if (a.benefit.amount_krw, a.benefit.estimated_total_krw) != (
        b.benefit.amount_krw,
        b.benefit.estimated_total_krw,
    ):
        report.benefit_disagreement = (
            f"A {a.benefit.amount_krw}/{a.benefit.estimated_total_krw} vs "
            f"B {b.benefit.amount_krw}/{b.benefit.estimated_total_krw}"
        )
        benefit = msgspec.structs.replace(a.benefit, amount_confidence="ESTIMATED")

    quality = msgspec.structs.replace(
        a.quality,
        cross_check="AGREE" if report.agree else "DISAGREE",
        needs_review_fields=_dedupe(review),
    )
    merged = msgspec.structs.replace(
        a,
        eligibility=eligibility,
        exclusions=exclusions,
        documents=documents,
        conflicts=conflicts,
        benefit=benefit,
        quality=quality,
    )
    return merged, report


def _a2_rules(p: PolicySchema) -> list[Rule]:
    return [r for r in [*p.eligibility, *p.exclusions] if r.basis == BASIS]


def _sig(r: Rule) -> tuple[str, str, str]:
    return (r.field, r.op, repr(r.value))


def _is_exclusion(p: PolicySchema, r: Rule) -> bool:
    return any(x.rule_id == r.rule_id for x in p.exclusions)


def _conflict_key(c: Conflict) -> tuple[str, str]:
    return (c.type, c.source_quote)


def _dedupe(items: list[str]) -> list[str]:
    out: list[str] = []
    for x in items:
        if x not in out:
            out.append(x)
    return out
