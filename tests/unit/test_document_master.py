"""배포되는 서류 마스터(`data/documents/master_v2.csv`) 자체를 검사한다.

로더는 값의 앞뒤가 맞는지 보지만(중복·음수·역전), **지금 커밋된 표가 실제로
쓸 만한지**는 아무도 보지 않았다. 이 표는 화면에 "언제까지 준비하세요"를 만드는
근거라, 틀리면 사용자가 마감을 놓친다.

여기서 확인하는 것은 사실 자체가 아니라 **사실이 확인 가능한 형태인가**다.
소요일이 맞는지는 발급기관 페이지를 봐야 알 수 있고 그건 사람의 일이지만,
"확인했다고 표시하려면 무엇을 봤는지가 남아 있어야 한다"는 규칙은 코드가 지킬 수 있다.
"""

from __future__ import annotations

import json
import pathlib
import re

import pytest

from app.planner.documents import (
    ISSUE_KINDS,
    NAME_ALIASES,
    load_master,
    master,
    resolve,
)

ROOT = pathlib.Path(__file__).resolve().parents[2]
MANUAL_A2 = ROOT / "data" / "manual" / "a2"

# 발급 주체가 따로 없는 유형 — 출처 링크가 없는 것이 정상이다.
#   THIRD_PARTY 회사가 발급한다 (재직·퇴직증명서)
#   SELF_HELD   본인이 이미 갖고 있다 (계약서·통장 사본)
NO_ISSUER_PAGE = frozenset({"THIRD_PARTY", "SELF_HELD"})

# 도메인 루트가 아니라 특정 서비스를 가리키는 링크인가.
_DEEP_LINK = re.compile(r"https?://[^/]+/.+")


@pytest.fixture(scope="module")
def specs():
    return load_master()


def test_커밋된_표가_그대로_읽힌다(specs):
    assert len(specs) == 36
    assert all(s.issue_kind in ISSUE_KINDS for s in specs.values())


def test_발급기관이_있는_서류에는_출처가_있다(specs):
    """확인하려면 어디를 봐야 하는지가 있어야 한다."""
    없음 = [
        f"{s.doc_code} {s.name}"
        for s in specs.values()
        if s.issue_kind not in NO_ISSUER_PAGE and not s.source_url
    ]
    assert not 없음, f"출처 없는 정부발급 서류: {없음}"


def test_발급기관이_없는_서류는_출처가_없어도_된다(specs):
    """회사가 써주는 재직증명서에는 조회할 페이지가 없다. 빈 값이 정답이다."""
    대상 = [s for s in specs.values() if s.issue_kind in NO_ISSUER_PAGE]
    assert 대상, "THIRD_PARTY/SELF_HELD 가 하나도 없습니다 — 유형 값이 바뀌었나?"
    assert all(not s.source_url for s in 대상)


def test_확인완료로_올리려면_무엇을_봤는지_남아야_한다(specs):
    """`확인완료` 는 화면에서 '추정치' 표시를 없앤다 — 근거 없이 올리면 안 된다.

    도메인 루트(`https://www.gov.kr`)로는 그 서류의 소요일·수수료를 확인할 수 없다.
    확인한 사람이 본 페이지가 남아 있어야 다음 사람이 재확인할 수 있다.

    지금은 36종 전부 `확인필요` 라 이 테스트는 비어 있는 채로 통과한다.
    표시를 올리는 순간부터 규칙이 된다.
    """
    근거없이_확인됨 = [
        f"{s.doc_code} {s.name} (출처={s.source_url!r})"
        for s in specs.values()
        if s.verified and not (s.source_url and _DEEP_LINK.match(s.source_url))
    ]
    assert not 근거없이_확인됨, (
        "확인완료로 표시했지만 무엇을 확인했는지 가리키는 링크가 없습니다: "
        f"{근거없이_확인됨}"
    )


def test_별칭은_존재하는_서류를_가리킨다():
    table = master()
    끊긴것 = {alias: code for alias, code in NAME_ALIASES.items() if code not in table}
    assert not 끊긴것, f"없는 doc_id 를 가리키는 별칭: {끊긴것}"


# --- 실공고 서류명과의 매칭 -------------------------------------------------


def _announced_names() -> set[str]:
    names: set[str] = set()
    for f in MANUAL_A2.glob("*.json"):
        for d in json.loads(f.read_text(encoding="utf-8")).get("documents") or []:
            names.add(d["name"])
    return names


# 마스터에 없는 서류 — 별칭으로 흡수할 수 없고, 소요일·수수료를 확인해야 추가된다.
#   지방세(재산세)미과세증명서 · 본인신용정보조회서  → 실재하는 서류지만 표에 없다
#   월세 납부 증명자료 · 월세 이체 증빙서류          → 정형 서류가 아닌 증빙 자료
#   월세지원 신청서 · 소득·재산 신고서              → 사업이 주는 서식
KNOWN_UNMATCHED = {
    "지방세(재산세)미과세증명서",
    "본인신용정보조회서",
    "월세 납부 증명자료",
    "월세 이체 증빙서류",
    "월세지원 신청서",
    "소득·재산 신고서",
}


def test_실공고_서류명이_마스터에_붙는다():
    """붙지 않으면 계획이 그 서류의 소요일을 추정치로 돌린다 — 화면이 흐려진다."""
    unmatched = {n for n in _announced_names() if resolve(None, n) is None}

    새로_깨진것 = unmatched - KNOWN_UNMATCHED
    assert not 새로_깨진것, f"매칭이 깨진 서류명: {새로_깨진것}"

    # 목록이 낡지 않도록: 해결된 항목은 목록에서 빼야 한다
    이미_해결됨 = KNOWN_UNMATCHED - unmatched
    assert not 이미_해결됨, f"이제 매칭되는데 목록에 남아 있습니다: {이미_해결됨}"


def test_수식어가_붙은_표기도_같은_서류로_본다():
    """'주택 임대차계약서 사본' 과 '임대차계약서' 는 같은 서류다."""
    for 표기 in ("임대차계약서", "주택 임대차계약서 사본", "주택임대차계약서"):
        spec = resolve(None, 표기)
        assert spec is not None and spec.doc_code == "D035", 표기
