"""데모 5건이 골든 정답표를 재현하는가 — 프롬프트·병합·서류 매핑의 회귀 테스트.

data/manual/a2 의 응답을 FileLLM 으로 같은 파이프라인에 태워 채점한다. 이 테스트가 깨지면
(a) 정답표를 바꿀 만한 근거가 생겼거나 (b) 병합 코드가 뭔가를 조용히 떨어뜨리기 시작한 것이다.
둘 중 어느 쪽인지 리포트가 말해준다.
"""

from __future__ import annotations

import json
from pathlib import Path

from batch.agents.cli import FileLLM
from batch.agents.golden import render, score_all, score_policy
from batch.agents.structure import resolve_conflict_targets, structure_policy
from batch.collect.client import load_raw
from batch.collect.normalize import record_to_policy

ROOT = Path(__file__).resolve().parents[2]
MANUAL = ROOT / "data" / "manual"
GOLDEN = json.loads((Path(__file__).parent / "a2_expected.json").read_text(encoding="utf-8"))


def build_manual_policies():
    records = load_raw(MANUAL / "raw")
    policies = []
    for rec in records:
        base = record_to_policy(rec, crawled_at="20260919T000000Z")
        llm = FileLLM(MANUAL / "a2" / f"{base.policy_id}.json")
        merged, report = structure_policy(base, rec, llm, system_prompt="golden")
        assert report.rejected == [], (base.policy_id, [r.__dict__ for r in report.rejected])
        policies.append(merged)
    resolve_conflict_targets(policies)
    return policies


def test_데모_11건이_골든_정답표를_전부_재현한다():
    scores = score_all(build_manual_policies(), GOLDEN)
    assert len(scores) == 11
    assert all(s.passed for s in scores), "\n" + render(scores)


def test_채점기는_누락과_금지_필드를_잡는다():
    policies = {p.policy_id: p for p in build_manual_policies()}
    gapyeong = policies["GG-12010"]
    expected = {
        "rules": [
            {"field": "household_size", "op": "==", "value": 1},
            {"field": "age", "op": ">=", "value": 99},
        ],
        "forbidden_fields": ["household_income_ratio_median"],
        "doc_codes": {"통장 사본": "D999"},
        "min_conflicts": 10,
        "unrepresentable": False,
    }
    s = score_policy(gapyeong, expected)
    assert not s.passed
    assert s.found == 1 and s.missing == ["age >= 99"]
    assert s.forbidden_hits == ["household_income_ratio_median"]
    assert s.doc_code_missing == ["통장 사본: D036 (정답 D999)"]
    assert s.conflict_shortfall == 7 and s.marker_mismatch
    assert "❌ GG-12010" in render([s])
