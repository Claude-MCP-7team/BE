"""교차검증 (AI-M4) 을 실공고로 검증한다 — 이슈 #3 7번.

무엇을 확인하는가
  같은 공고를 두 번 읽어 결과가 갈렸을 때, 갈린 필드가 **NEEDS_REVIEW 로 내려오고
  룰이 사라지지 않는지.** `cross_check` 에는 단위 테스트가 있었지만 합성 룰로만
  돌았고, CLI 가 `<plcyNo>.b.json` 을 집어 두 번째 읽기를 태우는 배선은 사람이 손으로
  돌릴 때만 실행됐다.

두 번째 읽기를 어디 두는가
  CLI 문서대로면 `data/manual/a2/GG-12010.b.json` 이다. 그런데 거기 두면 `--responses`
  실행이 자동으로 교차검증을 타서 **`data/manual/policies.json` 과 데모 seed 가 함께
  바뀐다** — 데모 시나리오가 기대하는 가평 월세의 판정이 달라진다. 그래서 fixture 로
  두고, 테스트가 임시 디렉터리에 두 파일을 놓아 CLI 를 실제로 부른다. 배선은 그대로
  지나가고 커밋된 데이터는 건드리지 않는다.

무엇을 갈라 놓았는가 (tests/e2e/fixtures/crosscheck/GG-12010.b.json)
  이 공고는 소득 기준을 두 군데에 적어 뒀다 — "1인 가구 기준 중위소득 150% 이하"와
  "취업준비생·학생 등 무직자: 원 가구 소득 반영". A 는 앞을, B 는 뒤를 근거로 읽었다.
  인용문은 양쪽 다 원문 그대로라 대조를 통과한다 — 인용이 틀려서가 아니라 **문장이
  애매해서** 갈리는 경우를 본다. B 는 1인 가구 조건도 보지 못했다.
"""

from __future__ import annotations

import pathlib
import shutil
from datetime import date

import msgspec
import pytest

from app.schemas.policy import PolicySchema
from app.schemas.validate import validate_policy
from batch.agents import cli
from batch.agents.structure import BASIS
from batch.build_snapshot import build

ROOT = pathlib.Path(__file__).resolve().parents[2]
RAW = ROOT / "data" / "manual" / "raw"
PRIMARY = ROOT / "data" / "manual" / "a2" / "GG-12010.json"
SECOND = pathlib.Path(__file__).resolve().parent / "fixtures" / "crosscheck" / "GG-12010.b.json"

POLICY_ID = "GG-12010"


def _run_cli(tmp: pathlib.Path, *, second_reading: bool) -> PolicySchema:
    """CLI 를 실제로 돌려 구조화된 정책을 돌려준다. API 키는 쓰지 않는다."""
    responses = tmp / "a2"
    responses.mkdir()
    shutil.copy(PRIMARY, responses / f"{POLICY_ID}.json")
    if second_reading:
        shutil.copy(SECOND, responses / f"{POLICY_ID}.b.json")

    out = tmp / "policies.json"
    # 리포트 경로는 저장소(docs/a2) 상대라 그냥 돌리면 테스트가 파일을 남긴다.
    # 원래 값으로 되돌려 둬야 이어서 도는 테스트가 임시 디렉터리를 물려받지 않는다.
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(cli, "REPORT_DIR", tmp / "report")
        code = cli.main(["structure", str(RAW), "--responses", str(responses), "-o", str(out)])
    assert code == 0

    policies = msgspec.json.decode(out.read_bytes(), type=list[PolicySchema])
    return next(p for p in policies if p.policy_id == POLICY_ID)


@pytest.fixture(scope="module")
def cross_checked(tmp_path_factory) -> PolicySchema:
    return _run_cli(tmp_path_factory.mktemp("crosscheck"), second_reading=True)


def test_두_번_읽어_갈린_것이_품질에_기록된다(cross_checked):
    assert cross_checked.quality.cross_check == "DISAGREE"
    # 갈린 필드가 목록에 있어야 담당부서 확인 안내가 붙는다
    assert "household_income_ratio_median" in cross_checked.quality.needs_review_fields
    assert "household_size" in cross_checked.quality.needs_review_fields


def test_값이_갈린_룰은_A를_유지하되_확신을_내린다(cross_checked):
    """B 의 값으로 덮어쓰면 근거 없이 기준이 바뀐다. 지우면 조용한 누락이다.

    그래서 A(150%)를 그대로 두고 NEEDS_REVIEW + ambiguous 로 내린다 — 판정은 계속
    나가고, 화면에는 '확인 필요'가 붙는다.
    """
    소득 = _rule(cross_checked, "household_income_ratio_median")
    assert (소득.op, 소득.value) == ("<=", 150)  # A 쪽 값이 남는다
    assert 소득.confidence == "NEEDS_REVIEW"
    assert 소득.ambiguous is True
    assert 소득.source_quote == "소득기준 : 1인 가구 기준 중위소득 150% 이하"


def test_한쪽만_본_룰도_지우지_않는다(cross_checked):
    """한 번만 본 조건은 있을 수도 없을 수도 있다. 지우면 조건을 본 적도 없이
    '적격'이 나가고, CONFIRMED 로 두면 근거 없는 확정이다.
    """
    가구원 = _rule(cross_checked, "household_size")
    assert (가구원.op, 가구원.value) == ("==", 1)
    assert 가구원.confidence == "NEEDS_REVIEW"


def test_API_코드_룰은_교차검증이_건드리지_않는다(cross_checked):
    """전부 NEEDS_REVIEW 로 내려가면 배지가 무의미해진다 — 갈린 것만 내려가야 한다.

    이 공고는 나이·지역·혼인을 API 코드로도 준다. 두 번 읽든 한 번 읽든 같은 값이라
    비교 대상이 아니다. 실제로 A2 가 낸 나이·혼인 조건은 코드 룰과 겹쳐서 merge 에서
    이미 걸러졌다 (제안 4 → 채택 2). 그래서 여기 남은 A2 룰은 두 건뿐이고, 둘 다
    갈렸다 — '양쪽이 같게 읽은 A2 룰'은 이 공고로는 만들 수 없어서
    `tests/unit/test_agents_crosscheck.py` 가 맡는다.
    """
    코드룰 = [
        r
        for r in [*cross_checked.eligibility, *cross_checked.exclusions]
        if r.basis != BASIS
    ]
    assert {r.field for r in 코드룰} == {"region_code", "age", "marital_status"}
    assert all(r.confidence == "CONFIRMED" for r in 코드룰)
    assert all(r.ambiguous is False for r in 코드룰)


def test_한쪽만_본_서류와_상충은_합집합으로_남는다(cross_checked):
    """B 는 서류·상충을 하나도 내지 않았다. 교집합으로 묶었다면 A 가 본 10건이
    통째로 사라져 서류 준비 일정이 빈다 — 그게 이 저장소가 가장 싫어하는 실패다.
    """
    assert len(cross_checked.documents) == 10
    assert len(cross_checked.conflicts) == 3


def test_교차검증을_지난_정책도_계약과_빌더를_통과한다(cross_checked):
    """확신을 내리는 과정에서 스키마가 깨지면 스냅샷 빌드 전체가 멈춘다."""
    assert not validate_policy(cross_checked)
    accepted, report = build([cross_checked], today=date(2026, 9, 24))
    assert not report.rejected, report.rejected
    assert len(accepted) == 1


def test_두_번째_읽기가_없으면_교차검증을_건너뛴다(tmp_path):
    """`.b.json` 이 없는 정책까지 DISAGREE 로 물들면 배지를 아무도 안 믿는다.

    커밋된 `data/manual/a2/` 에는 `.b.json` 이 없으므로 이쪽이 지금의 기본 경로다.
    """
    policy = _run_cli(tmp_path, second_reading=False)
    assert policy.quality.cross_check == "SKIPPED"
    assert _rule(policy, "household_size").confidence == "CONFIRMED"
    assert _rule(policy, "household_income_ratio_median").confidence == "ESTIMATED"


def test_fixture_의_인용문은_전부_원문에_있다():
    """fixture 가 공고문을 다듬어 두면 '해석이 갈린 경우'를 보는 게 아니라
    '인용이 틀린 경우'를 보게 된다 — 검증 단계가 달라서 시험하는 것이 바뀐다.
    """
    import json

    from batch.agents.text import assemble_text
    from batch.collect.client import load_raw

    record = next(r for r in load_raw(RAW) if r["plcyNo"] == POLICY_ID)
    text = assemble_text(record)
    second = json.loads(SECOND.read_text(encoding="utf-8"))

    quotes = [c["source_quote"] for c in second["conditions"]]
    quotes += [second[k]["source_quote"] for k in ("benefit", "period", "dept")]
    for quote in quotes:
        assert quote in text, quote


def _rule(policy: PolicySchema, field: str):
    """A2 가 만든 룰 하나. API 코드 룰은 양쪽이 같아서 교차검증 대상이 아니다."""
    rules = [
        r for r in [*policy.eligibility, *policy.exclusions] if r.field == field and r.basis == BASIS
    ]
    assert len(rules) == 1, f"{field}: {len(rules)}건"
    return rules[0]
