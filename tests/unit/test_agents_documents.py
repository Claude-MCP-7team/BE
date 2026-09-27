"""서류명 정규화 보조 — 공고 표기가 마스터 doc_code 로 이어지는지, 그리고 이어지면 안 되는 것은 안 이어지는지."""

from __future__ import annotations

from batch.agents.documents import clean_document_name, master_names, resolve_doc_code
from batch.agents.structure import merge
from batch.collect.normalize import record_to_policy
from tests.unit.test_agents_structure import assemble_text, output, rec


def test_표기_꼬리를_걷어낸다():
    assert clean_document_name("주민등록등본 1부") == "주민등록등본"
    assert clean_document_name("주민등록등본 1부(필수)") == "주민등록등본"
    assert clean_document_name("가족관계증명서(상세) 각 1통") == "가족관계증명서(상세)"
    assert clean_document_name("통장 사본 등") == "통장 사본"
    assert clean_document_name("재직증명서 : 회사 발급") == "재직증명서"


def test_꼬리를_걷어내면_별칭으로_이어진다():
    assert resolve_doc_code("주민등록등본 1부", None) == "D001"
    assert resolve_doc_code("건축물대장", None) == "D024"
    assert resolve_doc_code("재학증명서(필수)", None) == "D022"


def test_모델의_정식_명칭은_마스터에_글자_그대로_있을_때만_쓴다():
    assert resolve_doc_code("주택 임대차계약서 사본", "임대차계약서 사본") == "D035"
    # 별칭('임대차계약서')은 canonical 로 인정하지 않는다 — 하지만 표기 청소 경로에서는 잡힌다
    assert resolve_doc_code("임대차계약서", "임대차계약서") == "D035"
    # 목록 밖 이름을 정식 명칭이라고 우기면 무시한다
    assert resolve_doc_code("지방세(재산세)미과세증명서", "지방세 납세증명서(전국)") is None
    assert resolve_doc_code("본인신용정보조회서", None) is None


def test_수식어만_다른_표기는_별칭으로_흡수한다():
    # 실공고에 나온 표기들 (이슈 #3). 발급처가 같은 서류에 수식어만 붙은 경우다.
    assert resolve_doc_code("등본", None) == "D001"
    assert resolve_doc_code("초본", None) == "D002"
    assert resolve_doc_code("확정일자가 날인된 임대차계약서 사본", None) == "D035"
    assert resolve_doc_code("주택 임대차계약서 사본", None) == "D035"


def test_마스터에_없는_서류는_매핑하지_않는다():
    """소요일을 모르는 서류를 마스터에 넣으면 '0일 확정'이 되어 계획이 틀린다.

    미매핑은 DEFAULT_UNKNOWN_LEAD_BUSINESS_DAYS(2일) + estimated 로 나가므로
    모르는 상태를 모른다고 말하는 쪽이 보수적이다 (이슈 #3의 5번 항목).
    """
    for name in [
        "경력증명서",  # 회사 발급. 재직증명서(D032)와 다른 서류다
        "사업자등록증",  # 본인 보관 증서. 홈택스 발급 '사업자등록증명'(D010)과 다르다
        "건강보험 확인서류",  # 자격득실(D014)인지 납부확인서(D015)인지 공고가 밝히지 않는다
        "주택 임대차 계약 신고필증",  # 부동산거래관리시스템 발급. 마스터에 없다
        "지방세 세목별 과세증명서(전국)",  # 납세증명서(D012)와 다른 서식이다
    ]:
        assert resolve_doc_code(name, None) is None, name


def test_비슷한_이름의_다른_서류로_넘어가지_않는다():
    # 마스터에 '지방세 납세증명서'가 있어도 '미과세증명서'는 다른 서류다. 유사도로 잇지 않는다.
    assert resolve_doc_code("지방세 미과세증명서", None) is None
    assert resolve_doc_code("퇴직증명서", None) == "D033"
    assert resolve_doc_code("재직증명서", None) == "D032"


def test_마스터_목록은_사용자_메시지에_붙고_인용_대상은_아니다():
    names = master_names()
    assert "주민등록표 등본" in names and len(names) >= 30
    # 마스터 이름을 인용문으로 쓰면 원문에 없으므로 버려진다
    text = assemble_text(rec())
    doc = {
        "name": "주민등록표 등본",
        "issuer": None,
        "canonical_name": "주민등록표 등본",
        "source_quote": "주민등록표 등본",
    }
    merged, report = merge(record_to_policy(rec()), output(documents=[doc]), text)
    assert merged.documents == []
    assert report.rejected[0].code == "QUOTE_NOT_VERBATIM"


def test_병합_시_doc_code_가_채워진다():
    text = assemble_text(rec())
    docs = [
        {
            "name": "주민등록등본",
            "issuer": None,
            "canonical_name": None,
            "source_quote": "주민등록등본 1부",
        },
        {
            "name": "건강보험료 납부확인서",
            "issuer": None,
            "canonical_name": "건강보험료 납부확인서",
            "source_quote": "건강보험료 납부확인서",
        },
    ]
    merged, _ = merge(record_to_policy(rec()), output(documents=docs), text)
    assert [(d.name, d.doc_code) for d in merged.documents] == [
        ("주민등록등본", "D001"),
        ("건강보험료 납부확인서", "D015"),
    ]
