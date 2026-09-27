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
    DocumentMasterError,
    load_master,
    master,
    parse_master,
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
    """
    근거없이_확인됨 = [
        f"{s.doc_code} {s.name} (출처={s.source_url!r})"
        for s in specs.values()
        if s.verified
        and s.verification_kind == "공적출처"
        and not (s.source_url and _DEEP_LINK.match(s.source_url))
    ]
    assert not 근거없이_확인됨, (
        f"확인완료로 표시했지만 무엇을 확인했는지 가리키는 링크가 없습니다: {근거없이_확인됨}"
    )


def test_링크만으로는_확인완료가_되지_않는다(specs):
    """2026-09-20 검증이 찾은 구멍: 링크가 있어도 그 페이지가 CSV 값을 말해주지
    않을 수 있다.

    실제 사례가 D020(대학 졸업증명서)이다. 원본에서 유일하게 딥링크를 갖고 있던
    행인데, 그 페이지는 처리기간을 '즉시'라고 적어 CSV 의 1~3일과 어긋났고
    수수료는 금액 자체를 적지 않았다. 링크 존재만 요구하면 이 행이 통과한다.

    그래서 **그 페이지가 뭐라고 적었는지**를 한 줄로 남기게 한다. 옮겨 적는 순간
    CSV 값과 다르면 눈에 띈다.
    """
    빈칸 = [f"{s.doc_code} {s.name}" for s in specs.values() if s.verified and not s.evidence_quote]
    assert not 빈칸, f"확인완료인데 근거문구가 없습니다: {빈칸}"

    날짜없음 = [
        f"{s.doc_code} {s.name}" for s in specs.values() if s.verified and not s.verified_on
    ]
    assert not 날짜없음, (
        f"확인완료인데 검증일이 없습니다 — 언제 본 것인지 알 수 없습니다: {날짜없음}"
    )


def test_검증할_출처가_원래_없는_서류는_따로_분류한다(specs):
    """'아직 검증 안 함'과 '검증할 출처가 원래 없음'이 같은 값이면 안 된다.

    뭉개 두면 다음 사람이 재직증명서의 정부 페이지를 찾으러 다닌다. 그런 페이지는
    없다 — 회사가 써주는 서류다.
    """
    kinds = {s.verification_kind for s in specs.values()}
    assert kinds <= {"공적출처", "기관자율", "본인보관"}, kinds

    for s in specs.values():
        expected = {"THIRD_PARTY": "기관자율", "SELF_HELD": "본인보관"}.get(
            s.issue_kind, "공적출처"
        )
        assert s.verification_kind == expected, (
            f"{s.doc_code} {s.name}: 발급유형 {s.issue_kind} 인데 검증유형이 {s.verification_kind}"
        )


def test_기관자율_서류는_소요일이_범위여야_한다(specs):
    """재직증명서를 '정확히 N일'로 적으면 회사 사정을 안다고 주장하는 것이다.

    링크를 면제받는 대신 이쪽을 지킨다. 범위로 두면 화면이 '1~5영업일'로 보여주고,
    역산은 최댓값을 써서 보수적으로 잡는다.
    """
    위반 = [
        f"{s.doc_code} {s.name} ({s.lead_min_business_days}~{s.lead_max_business_days})"
        for s in specs.values()
        if s.verification_kind == "기관자율"
        and s.lead_min_business_days == s.lead_max_business_days
    ]
    assert not 위반, f"기관자율인데 소요일이 단일값입니다: {위반}"


def test_본인보관_서류는_소요일이_0이다(specs):
    """이미 갖고 있는 것에 발급 소요일을 붙이면 착수일이 근거 없이 당겨진다."""
    위반 = [
        f"{s.doc_code} {s.name} ({s.lead_max_business_days}일)"
        for s in specs.values()
        if s.verification_kind == "본인보관" and s.lead_max_business_days != 0
    ]
    assert not 위반, f"본인보관인데 소요일이 0 이 아닙니다: {위반}"


def test_유효기간은_소요일과_별개로_검증해야_한다(specs):
    """정부24 민원안내 페이지는 **유효기간을 적지 않는다** (2026-09-20 검증).

    소요일·수수료만 확인하고 화면의 '추정치' 딱지를 떼면, 근거 없는 유효기간이
    검증된 값처럼 보인다. 유효기간은 '너무 일찍 떼면 만료된다'는 하한을 정하므로
    틀리면 준비 시작일이 반대로 어긋난다.

    그래서 축을 나눴다. 근거가 생긴 행만 딱지가 떨어진다.
    """
    for s in specs.values():
        if s.validity_days is None:
            assert s.validity_grounded, f"{s.doc_code}: 만료 개념이 없으면 근거도 불필요"
        elif not s.validity_source:
            assert not s.is_fully_grounded, (
                f"{s.doc_code} {s.name}: 유효기간 {s.validity_days}일에 근거가 없는데 "
                f"화면에서 검증된 것처럼 보입니다"
            )

    # 근거를 적었다면 그것도 구체적이어야 한다 (법령 조문 등).
    부실 = [
        f"{s.doc_code} ({s.validity_source!r})"
        for s in specs.values()
        if s.validity_source and len(s.validity_source.strip()) < 5
    ]
    assert not 부실, f"유효기간 근거가 너무 짧습니다: {부실}"


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
    # Additional notices added by the AI rehearsal corpus. These names need
    # master CSV entries before they can carry confirmed issue/fee metadata.
    # 발급처가 마스터 항목과 같은 '등본'·'확정일자가 날인된 임대차계약서 사본'은
    # 별칭으로 해소했다. 아래는 마스터에 행 자체가 없는 서류라서, 소요일을 모르는
    # 채로 넣으면 0일(=즉시 발급)로 굳는다 — 미매핑(2일 추정)이 더 보수적이다.
    "경력증명서",  # 회사 발급. 재직증명서(D032)와 다른 서류
    "사업자등록증",  # 본인 보관 증서. 홈택스 '사업자등록증명'(D010)과 다름
    "건강보험 확인서류",  # 자격득실(D014)인지 납부확인서(D015)인지 공고가 안 밝힌다
    "주택 임대차 계약 신고필증",  # 부동산거래관리시스템 발급
    "지방세 세목별 과세증명서(전국)",  # 납세증명서(D012)와 다른 서식
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


# --- 빈 칸이 확정값으로 새어나가지 않게 ------------------------------------


def _row(**over) -> dict[str, str]:
    """로더를 통과하는 최소 행. 검사하려는 칸만 바꿔 쓴다."""
    base = {
        "doc_id": "D900",
        "서류명": "테스트 증명서",
        "발급유형": "ONLINE_INSTANT",
        "발급채널": "정부24",
        "소요영업일_최소": "0",
        "소요영업일_최대": "0",
        "수수료_온라인": "0",
        "수수료_방문": "0",
        "인증강도": "SIMPLE",
        "유효기간_일": "",
        "조건분기": "",
        "출처링크": "",
        "검증유형": "출처없음",
        "근거문구": "",
        "검증일": "",
        "유효기간_근거": "",
        "검증상태": "확인필요",
        "비고": "",
    }
    base.update(over)
    return base


def test_소요일이_비면_거부한다():
    """빈 칸을 0 으로 메우면 '즉시 발급'이 **확정값으로** 나간다.

    마스터 값은 `lead_time_estimated=False` 라 화면에서 추정 표시가 붙지 않는다.
    사용자는 마감 전날 떼도 된다고 읽고, 실제로는 3일 걸리는 서류였으면 마감을
    놓친다. 미매핑으로 두면 추정 표시가 붙은 채 나가므로 그쪽이 낫다.

    진짜 즉시 발급이면 `0` 을 적으면 된다 — 적는 사람이 한 글자를 더 쓰는 대신,
    읽는 사람이 '빈 칸인가 0인가'를 추측하지 않아도 된다.
    """
    with pytest.raises(DocumentMasterError, match="소요영업일_최소"):
        parse_master([_row(소요영업일_최소="")])

    # 0 이라고 적으면 통과한다
    assert parse_master([_row(소요영업일_최소="0")])["D900"].lead_min_business_days == 0


def test_커밋된_표에는_빈_소요일이_없다(specs):
    """위 검증이 실제 표를 막고 있지 않은지. 36행 전부 값이 있어야 한다."""
    assert all(s.lead_min_business_days is not None for s in specs.values())
