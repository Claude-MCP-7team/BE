"""수집·조사 도구 테스트.

공식 API 명세를 확인할 수 없는 상태이므로, 이 도구의 가치는
'어떤 응답 형태가 오더라도 조사가 돌아가는가'에 있다. 그것을 검증한다.
"""

import json

import pytest

from batch.collect.parse import parse_payload
from batch.collect.survey import (
    GATE_URL_COVERAGE,
    build_report,
    inventory,
    render_markdown,
)

# --- 응답 봉투를 가정하지 않는다 --------------------------------------------


def test_JSON_봉투가_몇_겹이든_레코드를_찾는다():
    doc = {"resultCode": 200, "result": {"pagging": {"totCount": 2}, "youthPolicyList": [{"a": 1}, {"a": 2}]}}
    records, fmt = parse_payload(json.dumps(doc))
    assert fmt == "json"
    assert records == [{"a": 1}, {"a": 2}]


def test_봉투_키_이름이_달라져도_동작한다():
    """API 버전이 바뀌어 키 이름이 바뀌어도 수집이 멈추지 않아야 한다."""
    doc = {"response": {"body": {"items": [{"a": 1}, {"a": 2}, {"a": 3}]}}}
    records, _ = parse_payload(json.dumps(doc))
    assert len(records) == 3


def test_최상위가_배열이어도_동작한다():
    records, _ = parse_payload('[{"a":1},{"a":2}]')
    assert len(records) == 2


def test_XML_중첩_봉투에서_레코드를_1건으로_세지_않는다():
    xml = (
        "<response><body><items>"
        "<item><plcyNm>A</plcyNm></item>"
        "<item><plcyNm>B</plcyNm></item>"
        "<item><plcyNm>C</plcyNm></item>"
        "</items></body></response>"
    )
    records, fmt = parse_payload(xml)
    assert fmt == "xml"
    assert len(records) == 3
    assert records[0]["plcyNm"] == "A"


def test_XML_반복_태그는_리스트가_된다():
    xml = "<root><item><tag>a</tag><tag>b</tag></item><item><tag>c</tag></item></root>"
    records, _ = parse_payload(xml)
    assert records[0]["tag"] == ["a", "b"]


def test_JSON도_XML도_아니면_명확히_실패한다():
    with pytest.raises(ValueError, match="JSON 도 XML 도 아닌"):
        parse_payload("정책이 없습니다")


# --- 필드 인벤토리 ----------------------------------------------------------


def test_중첩_필드는_점_경로로_펴진다():
    stats = inventory([{"meta": {"dept": {"tel": "031-1"}}}])
    assert "meta.dept.tel" in stats


def test_배열_안의_필드는_인덱스를_뭉갠다():
    """docs[0].name, docs[1].name 이 따로 잡히면 인벤토리가 쓸모없어진다."""
    stats = inventory([{"docs": [{"name": "초본"}, {"name": "등본"}]}])
    assert "docs[].name" in stats
    assert stats["docs[].name"].non_empty == 2


def test_빈_문자열은_값이_있는_것으로_세지_않는다():
    """제공률을 재는 게 목적이므로 빈 문자열은 미제공이다."""
    stats = inventory([{"url": "https://a.kr"}, {"url": ""}, {"url": "   "}])
    assert stats["url"].present == 3
    assert stats["url"].non_empty == 1
    assert stats["url"].coverage(3) == pytest.approx(1 / 3)


# --- 값의 생김새로 필드를 추정한다 -------------------------------------------


def _records(n_with_url: int, total: int) -> list[dict]:
    return [
        {
            "plcyNo": f"R{i:04d}",
            "refUrl": f"https://youth.kr/{i}" if i < n_with_url else "",
            "sprtSclCn": "월 20만원 지원",
            "zipCd": "41465",
            "aplyYmd": "20260901",
        }
        for i in range(total)
    ]


def test_URL_필드를_이름이_아니라_값으로_찾아낸다():
    report = build_report(_records(8, 10))
    assert [c.path for c in report.url_candidates] == ["refUrl"]
    assert report.url_candidates[0].coverage == pytest.approx(0.8)


def test_원문링크_게이트는_70퍼센트가_기준이다():
    assert build_report(_records(7, 10)).gates[0].passed is True
    assert build_report(_records(6, 10)).gates[0].passed is False
    assert GATE_URL_COVERAGE == 0.70


def test_URL_필드가_아예_없으면_NO_GO_와_사유를_남긴다():
    """No-Go 자체보다 '왜'가 중요하다 — 크롤러 추가 판단의 근거가 된다."""
    gate = build_report([{"plcyNo": "R1"}] * 10).gates[0]
    assert gate.passed is False
    assert "찾지 못했습니다" in gate.note


def test_금액_필드를_찾아낸다():
    report = build_report(_records(10, 10))
    assert "sprtSclCn" in [c.path for c in report.amount_candidates]


def test_정책_건수_게이트_경계():
    def count_gate(n):
        return build_report([{"plcyNo": str(i), "refUrl": "https://a.kr"} for i in range(n)]).gates[2]

    assert count_gate(149).passed is False
    assert count_gate(150).passed is True
    assert count_gate(600).passed is True
    assert count_gate(601).passed is False


def test_레코드가_0건이어도_터지지_않는다():
    """수집이 실패했을 때 조사 도구까지 죽으면 원인을 못 본다."""
    report = build_report([])
    assert report.total_records == 0
    assert report.passed is False
    assert render_markdown(report)


# --- 리포트 렌더링 ----------------------------------------------------------


def test_리포트에_게이트_판정과_후보표가_모두_담긴다():
    md = render_markdown(build_report(_records(8, 10)))
    assert "G0 게이트 판정" in md
    assert "원문 링크 후보" in md
    assert "`refUrl`" in md
    assert "전체 필드 인벤토리" in md
