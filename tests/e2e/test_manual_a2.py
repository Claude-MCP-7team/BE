"""손으로 쓴 A2 응답(`data/manual/`)이 모델 응답과 같은 검증을 통과하는지.

`data/manual/README.md` 는 "손으로 썼다고 통과가 보장되지 않는다"고 적어 두었다.
그 말이 사실이려면 검증이 실제로 돌아야 하는데, 지금까지 이 데이터는 사람이 CLI 를
손으로 돌릴 때만 검증됐다. 인용문 한 글자를 고쳐 원문과 어긋나게 만들어도 아무것도
실패하지 않는 상태였다 — 그리고 그게 이 파이프라인이 막으려는 실패 그 자체다.

여기서는 네트워크도 API 키도 쓰지 않는다. 저장된 응답을 그대로 실제 검증·병합 경로에
태운다 (`FileLLM` 이 모델 자리에 들어가는 것 말고는 운영 경로와 같다).
"""

from __future__ import annotations

import pathlib

import pytest

from app.schemas.validate import validate_policy
from batch.agents.cli import FileLLM
from batch.agents.structure import structure_policy
from batch.collect.client import load_raw
from batch.collect.normalize import record_to_policy

MANUAL = pathlib.Path(__file__).resolve().parents[2] / "data" / "manual"
RAW, RESPONSES = MANUAL / "raw", MANUAL / "a2"


def _structured():
    for rec in load_raw(RAW):
        pid = rec["plcyNo"]
        response = RESPONSES / f"{pid}.json"
        if not response.exists():
            continue
        base = record_to_policy(rec)
        yield pid, structure_policy(base, rec, FileLLM(response))


@pytest.fixture(scope="module")
def structured():
    return dict(_structured())


def test_다섯_건이_모두_구조화된다(structured):
    assert len(structured) == 5, sorted(structured)


def test_인용문이_전부_원문에_있다(structured):
    """원문에서 글자 그대로 찾을 수 없는 인용문이 붙은 항목은 버려진다.

    버려진 게 있다는 건 손으로 쓴 응답이 공고문을 다듬었다는 뜻이고, 그러면 화면에
    보이는 '근거'가 공고문에 없는 문장이 된다.
    """
    dropped = {
        pid: [(r.path, r.code) for r in report.rejected]
        for pid, (_, report) in structured.items()
        if report.rejected
    }
    assert not dropped, f"검증에서 버려진 항목: {dropped}"


def test_병합_결과가_계약을_만족한다(structured):
    for pid, (policy, _) in structured.items():
        assert not validate_policy(policy), f"{pid}: {validate_policy(policy)}"


def test_근거_없는_룰은_하나도_없다(structured):
    for pid, (policy, _) in structured.items():
        for rule in [*policy.eligibility, *policy.exclusions]:
            assert rule.source_quote.strip(), f"{pid}/{rule.rule_id}"
        for conflict in policy.conflicts:
            assert conflict.source_quote.strip(), f"{pid}/{conflict.type}"


def test_옮기지_못한_조건은_버리지_않고_표시된다(structured):
    """무주택·자산처럼 룰이 될 수 없는 조건은 needs_review_fields 에 남아야 한다.

    조용히 사라지면 판정은 그 조건을 본 적도 없으면서 '확정'으로 나간다.
    """
    marked = {
        pid
        for pid, (policy, _) in structured.items()
        if "unrepresentable_conditions" in policy.quality.needs_review_fields
    }
    assert marked, "규칙화 불가 조건이 한 건도 표시되지 않았습니다"


def test_마감된_정책은_마감으로_표시된다(structured):
    """국토부 청년월세는 2026-05-29 에 끝났다 — 판정에 들어가면 안 된다."""
    policy, _ = structured["20260319005400112218"]
    assert policy.period.apply_end == "2026-05-29"
    assert policy.status == "expired"
