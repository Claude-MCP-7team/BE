"""공고문 텍스트 조립과 인용문 대조.

모델에게 주는 텍스트와 인용문을 대조하는 텍스트는 **같은 문자열**이어야 한다.
조립을 한 곳에서 하고 그 결과를 양쪽에 쓰는 이유다.
"""

from __future__ import annotations

import re
from typing import Any

from app.schemas.policy import PolicySchema

Record = dict[str, Any]

# 온통청년 API 의 자유 텍스트 필드. 순서는 모델이 읽기 좋은 순서다.
# 이름이 틀려도 빈 값으로 건너뛰며, 접미사 'Cn'(내용) 필드는 아래 목록에 없어도 자동으로 붙는다.
KNOWN_TEXT_FIELDS: tuple[tuple[str, str], ...] = (
    ("plcyNm", "정책명"),
    ("plcyExplnCn", "정책 설명"),
    ("plcySprtCn", "지원 내용"),
    ("sprtTrgtCn", "지원 대상"),
    ("addAplyQlfcCndCn", "추가 신청 자격"),
    ("ptcpPrpTrgtCn", "참여 제한 대상"),
    ("earnEtcCn", "소득 조건"),
    ("plcyAplyMthdCn", "신청 방법"),
    ("srngMthdCn", "심사 방법"),
    ("sbmsnDcmntCn", "제출 서류"),
    ("etcMttrCn", "기타 사항"),
    ("aplyYmd", "신청 기간"),
    ("bizPrdBgngYmd", "사업 시작일"),
    ("bizPrdEndYmd", "사업 종료일"),
    ("sprvsnInstCdNm", "주관 기관"),
    ("operInstCdNm", "운영 기관"),
)
# 이 중 조건이 적혀 있을 수 있는 '내용' 필드. 나머지(정책명·기간·기관)는 맥락일 뿐이라
# 이것만 있는 레코드는 LLM 을 부를 이유가 없다.
CONTENT_FIELDS: frozenset[str] = frozenset(k for k, _ in KNOWN_TEXT_FIELDS if k.endswith("Cn"))

_WS = re.compile(r"\s+")
_ZERO_WIDTH = re.compile(r"[​‌‍﻿]")


def _clean(value: Any) -> str:
    return "" if value is None else str(value).strip()


def assemble_text(rec: Record, *, announcement: str | None = None) -> str:
    """레코드의 자유 텍스트 필드를 `[항목명]` 블록으로 이어 붙인다.

    announcement 는 원문 공고문(크롤링 결과)이며 있으면 맨 뒤에 붙는다.
    조건이 적힐 수 있는 내용 필드가 하나도 없으면 빈 문자열이다 — 호출하지 말라는 뜻.
    """
    blocks: list[str] = []
    used: set[str] = set()
    has_content = False

    for key, label in KNOWN_TEXT_FIELDS:
        if text := _clean(rec.get(key)):
            blocks.append(f"[{label}]\n{text}")
            used.add(key)
            has_content |= key in CONTENT_FIELDS

    # 목록에 없는 *Cn 필드 — API 명세가 바뀌어도 내용 필드를 놓치지 않는다
    for key in sorted(rec):
        if key.endswith("Cn") and key not in used and (text := _clean(rec.get(key))):
            blocks.append(f"[{key}]\n{text}")
            has_content = True

    if announcement and (text := announcement.strip()):
        blocks.append(f"[공고문 원문]\n{text}")
        has_content = True

    return "\n\n".join(blocks) if has_content else ""


def describe_known_rules(base: PolicySchema) -> str:
    """API 코드로 이미 확정된 조건을 모델에게 알려주는 블록. 없으면 빈 문자열."""
    lines = [
        f"- {r.field} {r.op} {r.value!r}" for r in base.eligibility if r.field != "region_code"
    ]
    if not lines:
        return ""
    return "[이미 확정된 조건]\n" + "\n".join(lines)


def normalize_for_match(s: str) -> str:
    """공백 종류·개수 차이와 폭 0 문자만 지우고 나머지는 그대로 둔다.

    더 관대하게(구두점 제거 등) 만들면 '아닌 것'이 '맞는 것'으로 통과한다.
    """
    return _WS.sub(" ", _ZERO_WIDTH.sub("", s)).strip()


def quote_found(text: str, quote: str) -> bool:
    """인용문이 원문의 연속된 구간인가. 빈 인용문은 거짓이다."""
    q = normalize_for_match(quote)
    return bool(q) and q in normalize_for_match(text)
