"""서류명 정규화 보조 (AI-M4) — 공고 표기를 서류 마스터의 doc_code 에 잇는다.

플래너(app/planner/documents.py)는 '확인된 동의어'만 받고 유사도 추론을 하지 않는다.
재직증명서와 퇴직증명서가 한 글자 차이라서다. 여기서도 그 원칙을 지킨다:

  1. 표기 꼬리("1부", "(필수)", "각 1부", "등")만 결정론적으로 걷어낸다.
  2. 모델이 마스터 목록 중 하나를 canonical_name 으로 골라 줬으면 그것으로 다시 찾는다.
     목록 밖 이름은 무시한다 — 모델이 "비슷한" 서류를 고르면 틀린 소요일로 계획이 틀린다.
  3. 둘 다 실패하면 doc_code 는 None 이다. 관리자 큐가 사람에게 묻는다.

서류명 자체는 공고 표기를 그대로 둔다. 사용자에게 보이는 건 공고가 부른 이름이어야 한다.
"""

from __future__ import annotations

import re

from app.planner.documents import master, resolve

# 서류명 뒤에 붙는 부수 표기. 서류의 정체와 무관하다.
_TAILS = re.compile(
    r"(\s*(각\s*)?\d+\s*(부|통|장)\b|\s*\((필수|선택|해당자|해당\s*시)\)|\s*등$|\s*[:：].*$)"
)


def clean_document_name(name: str) -> str:
    """'주민등록등본 1부(필수)' → '주민등록등본'. 괄호 안 부연('(상세)')은 남긴다."""
    text = name.strip()
    prev = None
    while prev != text:
        prev = text
        text = _TAILS.sub("", text).strip(" -–—·,")
    return text


def master_names() -> list[str]:
    """모델에게 보여줄 마스터 서류명 목록. 순서는 doc_id 순으로 고정한다 (프롬프트 캐시)."""
    return [spec.name for _, spec in sorted(master().items())]


def resolve_doc_code(name: str, canonical_name: str | None) -> str | None:
    """공고 표기(+모델이 고른 정식 명칭)로 doc_code 를 찾는다. 못 찾으면 None.

    canonical_name 은 마스터 정식 명칭과 **글자 그대로** 같을 때만 쓴다. 별칭까지 허용하면
    모델이 '납세증명서'를 '미과세증명서'의 정식 명칭이라고 우길 때 막을 방법이 없다.
    """
    if canonical_name and canonical_name.strip() in master_names():
        spec = resolve(None, canonical_name.strip())
        if spec is not None:
            return spec.doc_code
    for candidate in (clean_document_name(name), name):
        if candidate and (spec := resolve(None, candidate)) is not None:
            return spec.doc_code
    return None
