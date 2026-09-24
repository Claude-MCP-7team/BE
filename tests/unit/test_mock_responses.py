"""커밋된 FE Mock 응답이 실제 응답과 같은지.

녹화본의 유일한 실패 모드는 **낡는 것**이다. 응답 모양이 바뀌어도 파일은 그대로라
FE 가 없어진 필드를 믿고 화면을 만든다. 손으로 쓴 예시였다면 아무도 못 잡는다.

그래서 매번 앱을 실제로 호출해 커밋된 것과 비교한다. 달라지면 여기서 실패하고,
`python tools/record_mock_responses.py` 로 갱신해 함께 커밋하면 된다 —
`docs/contracts/` 를 다루는 방식과 같다.
"""

from __future__ import annotations

import json

import pytest

from tools.record_mock_responses import OUT, VOLATILE, record, stabilize


@pytest.fixture(scope="module")
def fresh() -> dict[str, object]:
    return record()


def test_녹화본이_하나라도_있다() -> None:
    """디렉터리가 비면 아래 테스트들이 조용히 0건을 검사한다."""
    assert list(OUT.glob("*.json")), "녹화본이 없다 — tools/record_mock_responses.py"


def test_커밋된_응답이_실제_응답과_같다(fresh: dict[str, object]) -> None:
    for name, payload in fresh.items():
        path = OUT / name
        assert path.exists(), f"{name} 이 커밋되지 않았다 — 녹화기를 돌릴 것"
        committed = json.loads(path.read_text(encoding="utf-8"))
        assert committed == payload, (
            f"{name} 이 실제 응답과 다르다. "
            f"python tools/record_mock_responses.py 를 돌리고 함께 커밋할 것"
        )


def test_남은_파일이_없다(fresh: dict[str, object]) -> None:
    """엔드포인트가 사라졌는데 녹화본만 남으면 FE 가 없는 API 를 부른다."""
    orphans = {p.name for p in OUT.glob("*.json")} - set(fresh)
    assert not orphans, f"대응하는 호출이 없는 녹화본: {sorted(orphans)}"


def _volatile_offenders(node: object, where: str) -> list[str]:
    """고정값이 아닌 시계 필드를 모은다."""
    out: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key in VOLATILE and value != VOLATILE[key]:
                out.append(f"{where}:{key} = {value!r}")
            out += _volatile_offenders(value, where)
    elif isinstance(node, list):
        for item in node:
            out += _volatile_offenders(item, where)
    return out


def test_시계값은_고정되어_있다() -> None:
    """고정하지 않으면 매 커밋에 diff 가 나고, 아무도 diff 를 읽지 않게 된다."""
    offenders: list[str] = []
    for path in OUT.glob("*.json"):
        raw = json.loads(path.read_text(encoding="utf-8"))
        offenders += _volatile_offenders(raw, path.name)
    assert not offenders, offenders


def test_네_가지_판정_상태가_모두_담겨_있다(fresh: dict[str, object]) -> None:
    """FE 는 PASS/FAIL/UNKNOWN/FUTURE_PASS 배지를 만든다.

    Mock 에 한 종류만 있으면 나머지 배지는 화면에서 한 번도 안 그려진 채 제출된다.
    """
    summary = fresh["judge.all.json"]["summary"]  # type: ignore[index]
    for key in ("eligible", "ineligible", "needs_info", "future_eligible"):
        assert summary[key] >= 1, f"{key} 예시가 없다: {summary}"

    results = fresh["judge.all.json"]["results"]  # type: ignore[index]
    assert any(r["future_eligible_from"] for r in results), "충족 예상일 예시가 없다"
    assert any(r["unmatched"] for r in results), "부적격 사유 예시가 없다"
    assert any(r["unknown"] for r in results), "미확인 조건 예시가 없다"


def test_연락처가_빈_카드도_녹화되어_있다(fresh: dict[str, object]) -> None:
    """`dept_tel` 은 A2 를 거친 공고만 가지고 있다.

    Mock 이 전부 연락처를 들고 있으면 FE 는 그 필드를 필수로 그리고, 실제 목록에서
    카드 대부분이 깨진다. 빈 것과 있는 것이 둘 다 들어 있어야 한다.
    """
    items = fresh["policies.json"]["items"]  # type: ignore[index]
    tels = [i["dept_tel"] for i in items]
    assert any(t for t in tels) and any(not t for t in tels), tels


def test_조합과_일정에_볼_것이_들어_있다(fresh: dict[str, object]) -> None:
    """빈 배열만 녹화되면 FE 가 그 화면을 한 번도 안 그려 본 채 제출한다."""
    combos = fresh["combinations.json"]
    assert combos["eligible_count"] >= 2, combos["eligible_count"]  # type: ignore[index]
    for scenario in combos["scenarios"]:  # type: ignore[index]
        assert scenario["combinations"], scenario["kind"]

    plans = fresh["plan.json"]["plans"]  # type: ignore[index]
    assert any(p["documents"] for p in plans), "서류가 붙은 일정이 없다"
    assert any(p["recommended_start_date"] for p in plans), "역산된 착수일이 없다"


def test_에러_응답도_녹화되어_있다(fresh: dict[str, object]) -> None:
    """에러에도 화면이 있다. 문구와 모양을 미리 맞춰 볼 수 있어야 한다."""
    errors = {k: v for k, v in fresh.items() if k.startswith("error.")}
    assert len(errors) >= 4, sorted(errors)
    for name, payload in errors.items():
        assert payload["type"].startswith("/problems/"), name  # type: ignore[index]
        assert payload["title"], name  # type: ignore[index]


def test_녹화본이_실공고에서_나왔다(fresh: dict[str, object]) -> None:
    """합성 데이터로 되돌아가면 여기서 잡는다 (data/demo/README.md).

    합성 공고를 쓰는 것 자체는 선택이지만, 실데이터인 줄 알고 쓰는 건 아니다.
    """
    blob = json.dumps(fresh, ensure_ascii=False)
    assert "demo.invalid" not in blob, "합성 seed 의 흔적이 남아 있다"
    assert "[데모]" not in blob, "합성 seed 의 흔적이 남아 있다"

    for item in fresh["policies.json"]["items"]:  # type: ignore[index]
        # 스킴까지 확인한다. 공고 원본에는 `www.pdschool.kr` 처럼 스킴 없는 주소가
        # 섞여 있는데, 그대로 내보내면 브라우저가 상대 경로로 읽어 FE 도메인 안으로
        # 이동한다 — 깨진 링크가 아니라 **엉뚱한 페이지**라 더 늦게 발견된다.
        # http 도 허용한다. 실제 정부 사이트 중에 아직 http 인 곳이 있다.
        assert item["origin_url"].startswith(("http://", "https://")), item["policy_id"]


def test_stabilize_는_다른_값을_건드리지_않는다() -> None:
    """고정 대상만 바꿔야 한다. 넓게 잡으면 진짜 응답 변화를 덮어 버린다."""
    before = {"latency_ms": 42, "keep": {"latency_ms": 7, "other": [1, {"x": "y"}]}}
    after = stabilize(before)
    assert after == {"latency_ms": 0, "keep": {"latency_ms": 0, "other": [1, {"x": "y"}]}}
