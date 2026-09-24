"""골든 정답표 채점 — A2 결과를 사람이 확정한 정답과 대조한다 (AI-M2-3).

정답표(tests/golden/a2_expected.json)는 정책마다 **반드시 나와야 할 룰**과 **나오면 안 되는
필드**를 적는다. 룰은 (field, op, value) 로 비교하고 confidence 는 정답표에 있을 때만 본다.

두 용도가 있다.
  1. 회귀 테스트 — 손으로 만든 데모 응답이 프롬프트·병합 코드 변경에도 같은 룰을 내는가.
  2. 모델 채점 — 실제 모델 응답(data/a2-cache 또는 --responses)을 같은 정답표로 재고
     정책별 precision / recall 을 낸다. 프롬프트를 고칠 때 숫자가 오르는지 본다.

  python -m batch.agents.golden data/manual/policies.json tests/golden/a2_expected.json
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import msgspec

from app.core.console import force_utf8_console
from app.schemas.policy import PolicySchema, Rule

RuleKey = tuple[str, str, str]


@dataclass
class PolicyScore:
    policy_id: str
    expected: int = 0
    found: int = 0  # 정답 중 실제로 나온 것
    # 실제로 나왔지만 정답표에 없는 것. 틀렸다는 뜻은 아니다 — 정답표는 부분집합이다
    extra: int = 0
    missing: list[str] = field(default_factory=list)
    forbidden_hits: list[str] = field(default_factory=list)
    confidence_mismatch: list[str] = field(default_factory=list)
    doc_code_missing: list[str] = field(default_factory=list)
    conflict_shortfall: int = 0
    marker_mismatch: bool = False

    @property
    def recall(self) -> float:
        return self.found / self.expected if self.expected else 1.0

    @property
    def passed(self) -> bool:
        return (
            not self.missing
            and not self.forbidden_hits
            and not self.confidence_mismatch
            and not self.doc_code_missing
            and self.conflict_shortfall == 0
            and not self.marker_mismatch
        )


def _key(field_name: str, op: str, value: Any) -> RuleKey:
    return (field_name, op, json.dumps(value, ensure_ascii=False, sort_keys=True))


def _rules(policy: PolicySchema) -> dict[RuleKey, Rule]:
    return {_key(r.field, r.op, r.value): r for r in [*policy.eligibility, *policy.exclusions]}


def score_policy(policy: PolicySchema, expected: dict[str, Any]) -> PolicyScore:
    s = PolicyScore(policy_id=policy.policy_id)
    actual = _rules(policy)

    for want in expected.get("rules", []):
        s.expected += 1
        k = _key(want["field"], want["op"], want["value"])
        rule = actual.get(k)
        if rule is None:
            s.missing.append(f"{want['field']} {want['op']} {want['value']!r}")
            continue
        s.found += 1
        if "confidence" in want and rule.confidence != want["confidence"]:
            s.confidence_mismatch.append(
                f"{want['field']}: {rule.confidence} (정답 {want['confidence']})"
            )

    expected_keys = {_key(w["field"], w["op"], w["value"]) for w in expected.get("rules", [])}
    s.extra = sum(1 for k in actual if k not in expected_keys)

    fields_present = {k[0] for k in actual}
    s.forbidden_hits = [f for f in expected.get("forbidden_fields", []) if f in fields_present]

    codes = {d.name: d.doc_code for d in policy.documents}
    for name, code in expected.get("doc_codes", {}).items():
        if codes.get(name) != code:
            s.doc_code_missing.append(f"{name}: {codes.get(name)} (정답 {code})")

    want_conflicts = expected.get("min_conflicts", 0)
    s.conflict_shortfall = max(0, want_conflicts - len(policy.conflicts))

    if "unrepresentable" in expected:
        has = "unrepresentable_conditions" in policy.quality.needs_review_fields
        s.marker_mismatch = has != bool(expected["unrepresentable"])

    return s


def score_all(policies: list[PolicySchema], golden: dict[str, Any]) -> list[PolicyScore]:
    by_id = {p.policy_id: p for p in policies}
    out: list[PolicyScore] = []
    for pid, expected in golden["policies"].items():
        if pid not in by_id:
            s = PolicyScore(policy_id=pid, expected=len(expected.get("rules", [])))
            s.missing = ["(정책이 결과에 없음)"]
            out.append(s)
            continue
        out.append(score_policy(by_id[pid], expected))
    return out


def render(scores: list[PolicyScore]) -> str:
    lines = []
    total_expected = sum(s.expected for s in scores)
    total_found = sum(s.found for s in scores)
    for s in scores:
        mark = "✅" if s.passed else "❌"
        lines.append(f"{mark} {s.policy_id}: 정답 룰 {s.found}/{s.expected} (추가 {s.extra})")
        for m in s.missing:
            lines.append(f"     누락  {m}")
        for f in s.forbidden_hits:
            lines.append(f"     금지 필드 등장  {f}")
        for c in s.confidence_mismatch:
            lines.append(f"     confidence  {c}")
        for d in s.doc_code_missing:
            lines.append(f"     doc_code  {d}")
        if s.conflict_shortfall:
            lines.append(f"     상충 부족  {s.conflict_shortfall}건")
        if s.marker_mismatch:
            lines.append("     unrepresentable 표식 불일치")
    recall = total_found / total_expected if total_expected else 1.0
    passed = sum(1 for s in scores if s.passed)
    tail = f"정답 룰 재현율 {recall:.0%} ({total_found}/{total_expected})"
    lines.append(f"\n{tail} · 통과 {passed}/{len(scores)}")
    return "\n".join(lines)


def load_policies(path: Path) -> list[PolicySchema]:
    return msgspec.json.decode(path.read_bytes(), type=list[PolicySchema])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="A2 골든 정답표 채점")
    parser.add_argument("policies", help="A2 결과 PolicySchema JSON (data/manual/policies.json 등)")
    parser.add_argument("golden", help="tests/golden/a2_expected.json")
    args = parser.parse_args(argv)

    golden = json.loads(Path(args.golden).read_text(encoding="utf-8"))
    scores = score_all(load_policies(Path(args.policies)), golden)
    print(render(scores))
    return 0 if all(s.passed for s in scores) else 1


if __name__ == "__main__":
    force_utf8_console()
    sys.exit(main())
