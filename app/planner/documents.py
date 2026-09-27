"""서류 마스터 — 발급 소요일·수수료·유효기간의 권위 (BE-M5-1).

공고문은 "주민등록등본 1부"까지만 적고 며칠 걸리는지, 얼마인지, 언제까지 유효한지는
쓰지 않는다. 그 값들은 여기 있고, 공고문이 아니라 이 표가 이긴다.

표가 답하는 것 중 계획을 바꾸는 세 가지:

**유효기간** — 이것 때문에 계획이 '마감'이 아니라 '구간'이 된다.
  납세증명서는 30일이면 만료된다. 마감 60일 전에 떼어두면 제출일에는 종잇조각이다.
  그래서 서류마다 "이 날 이후에 떼야 하고, 이 날까지는 떼야 한다"가 생긴다.
  늦게 떼는 것만 위험한 게 아니라 일찍 떼는 것도 위험하다.

**발급 채널에 따라 값이 갈린다** — 온라인 0원/즉시 vs 방문 1,000원/3일.
  대학 졸업증명서는 정부24로 신청하면 주민센터에서 수령해야 해서 1~3일이 걸리지만,
  대학 홈페이지에서 직접 받으면 즉시다. 사용자가 고를 수 있는 선택지를 하나로
  뭉개면 "3일 걸린다"는 틀린 안내가 된다.

**소요일은 범위다** — 재직증명서는 회사 규정에 따라 1~5일이다.
  일정을 세울 때는 최댓값을 쓴다. 평균으로 잡으면 절반의 사용자가 마감을 놓친다.
  화면에는 범위를 그대로 보여주되, 역산은 최악값으로 한다.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

# 발급 유형. 계획에서 다르게 취급해야 하는 것만 구분한다.
#   ONLINE_INSTANT  온라인 즉시 — 사실상 대기 0
#   CONDITIONAL     조건에 따라 온라인/방문이 갈린다 (용도·연도 등)
#   OFFLINE_ONLY    방문해야만 발급된다
#   ANYWHERE_VISIT  온라인 신청 + 창구 수령 (또는 발급기관에서 직접 받으면 즉시)
#   THIRD_PARTY     회사 등 제3자가 발급 — 우리가 통제할 수 없고 편차가 가장 크다
#   SELF_HELD       본인이 이미 갖고 있다 — 대기 0, 다만 분실 시 재발급 필요
ISSUE_KINDS = frozenset(
    {
        "ONLINE_INSTANT",
        "CONDITIONAL",
        "OFFLINE_ONLY",
        "ANYWHERE_VISIT",
        "THIRD_PARTY",
        "SELF_HELD",
    }
)

# 제3자 발급은 우리가 재촉할 수 없다. 최댓값을 그대로 쓰되, 값이 비어 있으면
# 낙관적으로 0을 넣지 않고 이 값을 쓴다.
THIRD_PARTY_FALLBACK_DAYS = 5

MASTER_PATH = Path(__file__).resolve().parents[2] / "data" / "documents" / "master_v2.csv"


class DocumentMasterError(ValueError):
    """마스터 표를 읽을 수 없거나 값이 앞뒤가 맞지 않는다."""


@dataclass(frozen=True, slots=True)
class DocumentSpec:
    """서류 1종의 사실. 공고문보다 이 값이 우선한다."""

    doc_code: str
    name: str
    issue_kind: str
    channel: str

    lead_min_business_days: int
    lead_max_business_days: int

    # 채널별 수수료. None 은 '그 채널로는 발급되지 않는다'는 뜻이다 (0원과 다르다).
    fee_online_krw: int | None
    fee_offline_krw: int | None

    auth_strength: str  # NONE | SIMPLE | STRONG
    validity_days: int | None  # None 이면 만료 개념이 없다 (계약서 사본 등)

    condition: str | None
    source_url: str | None

    # --- 검증 (축이 둘이다) -------------------------------------------------
    #
    # 2026-09-20 검증에서 드러난 것: 정부24 민원안내 페이지는 **유효기간을 적지
    # 않는다.** 소요일·수수료만 확인할 수 있다. 그런데 유효기간은 "너무 일찍 떼면
    # 제출일에 만료된다"는 하한을 정하므로, 그게 틀리면 소요일이 맞아도 계획이
    # 어긋난다 (backplan.py §유효기간).
    #
    # 한 칸으로 뭉치면 소요일만 확인하고 유효기간까지 보증한 것처럼 보인다.
    # 그래서 축을 나눠 두고, 화면에 나가는 `is_fully_grounded` 는 둘 다 요구한다.
    verification_kind: str  # 공적출처 | 기관자율 | 본인보관
    verified: bool  # 소요일·수수료 축. '확인필요' 는 False
    evidence_quote: str | None  # 그 페이지가 실제로 뭐라고 적었는가
    verified_on: str | None
    validity_source: str | None  # 유효기간의 근거 (법령 조문). 정부24에는 없다
    notes: str | None

    @property
    def validity_grounded(self) -> bool:
        """유효기간에 근거가 있는가. 만료 개념이 없으면 근거도 필요 없다."""
        return self.validity_days is None or bool(self.validity_source)

    @property
    def is_fully_grounded(self) -> bool:
        """화면에서 '추정치' 표시를 뗄 수 있는가.

        두 축을 모두 요구한다. 소요일만 확인하고 딱지를 떼면, 근거 없는
        유효기간이 검증된 값처럼 보인다 — 그게 가장 조용한 종류의 오류다.
        """
        return self.verified and self.validity_grounded

    # --- 계획이 실제로 쓰는 값 ---------------------------------------------

    @property
    def planning_lead_days(self) -> int:
        """일정 역산에 쓸 소요일. 범위의 최댓값.

        평균을 쓰면 편차가 큰 서류(재직증명서 1~5일)에서 절반이 마감을 놓친다.
        """
        return self.lead_max_business_days

    @property
    def has_lead_variance(self) -> bool:
        """소요일이 범위인가. 화면이 '1~5영업일'로 보여줘야 하는 경우."""
        return self.lead_max_business_days != self.lead_min_business_days

    @property
    def cheapest_fee_krw(self) -> int | None:
        """발급 가능한 채널 중 가장 싼 값. **금액을 모르면 None 이다.**

        예전에는 0 을 돌려줬는데, 그러면 '무료'와 '금액 미상'이 같은 값이 된다.
        대학 졸업·재학·성적증명서(D020·D022·D023)가 실제로 그렇다 — 유료지만
        수수료가 국립대는 규칙, 사립대는 학칙에 따라 달라 단일 금액이 없다.
        0 으로 내보내면 화면이 "무료"라고 말하고, 사용자는 창구에서 돈을 낸다.

        모르는 것은 모른다고 해야 화면이 "학교마다 다름"으로 쓸 수 있다.
        """
        fees = [f for f in (self.fee_online_krw, self.fee_offline_krw) if f is not None]
        return min(fees) if fees else None

    @property
    def online_available(self) -> bool:
        return self.fee_online_krw is not None and self.issue_kind != "OFFLINE_ONLY"

    @property
    def requires_visit(self) -> bool:
        """반드시 창구에 가야 하는가. 하루를 통째로 쓰는 일이라 화면이 따로 알려야 한다."""
        return self.issue_kind in ("OFFLINE_ONLY", "ANYWHERE_VISIT")


def _int_or_none(raw: str) -> int | None:
    text = (raw or "").strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError as e:
        raise DocumentMasterError(f"숫자가 아닌 값: {raw!r}") from e


def _int_or(raw: str, default: int) -> int:
    value = _int_or_none(raw)
    return default if value is None else value


def _clean(raw: str) -> str | None:
    text = (raw or "").strip()
    return text or None


def parse_master(rows: list[dict[str, str]]) -> dict[str, DocumentSpec]:
    """CSV 행 → doc_code 로 찾는 표. 값이 앞뒤가 안 맞으면 거부한다.

    조용히 넘기면 "발급이 마감보다 오래 걸리는데 여유 있다고 나오는" 계획이 된다.
    """
    out: dict[str, DocumentSpec] = {}
    for i, row in enumerate(rows, start=2):  # 2 = 헤더 다음 줄
        code = (row.get("doc_id") or "").strip()
        if not code:
            continue
        if code in out:
            raise DocumentMasterError(f"{i}행: doc_id 가 중복입니다: {code}")

        kind = (row.get("발급유형") or "").strip()
        if kind not in ISSUE_KINDS:
            raise DocumentMasterError(f"{i}행({code}): 모르는 발급유형 {kind!r}")

        # 빈 칸을 0 으로 메우지 않는다. 메우면 '즉시 발급'이 **확정값으로** 나가고
        # (마스터 값은 lead_time_estimated=False 다), 사용자는 마감 전날 떼도 된다고
        # 읽는다. 소요일을 모르는 서류는 마스터에 넣지 않는 편이 낫다 — 미매핑은
        # 추정 표시가 붙은 채로 나가서 화면에서 구별된다.
        #
        # 진짜 즉시 발급이면 `0` 을 적는다. 적는 사람이 한 글자를 더 쓰는 대신,
        # 읽는 사람이 '빈 칸인가 0인가'를 추측하지 않아도 된다.
        if not (row.get("소요영업일_최소") or "").strip():
            raise DocumentMasterError(
                f"{i}행({code}): 소요영업일_최소가 비어 있습니다. "
                "즉시 발급이면 0 을 적고, 모르면 이 행을 넣지 마세요"
            )
        lead_min = _int_or(row.get("소요영업일_최소", ""), 0)
        lead_max = _int_or(
            row.get("소요영업일_최대", ""),
            THIRD_PARTY_FALLBACK_DAYS if kind == "THIRD_PARTY" else lead_min,
        )
        if lead_max < lead_min:
            raise DocumentMasterError(
                f"{i}행({code}): 소요일 최대({lead_max})가 최소({lead_min})보다 작습니다"
            )
        if lead_min < 0:
            raise DocumentMasterError(f"{i}행({code}): 소요일이 음수입니다")

        validity = _int_or_none(row.get("유효기간_일", ""))
        if validity is not None and validity <= 0:
            raise DocumentMasterError(f"{i}행({code}): 유효기간이 0 이하입니다")

        out[code] = DocumentSpec(
            doc_code=code,
            name=(row.get("서류명") or "").strip(),
            issue_kind=kind,
            channel=(row.get("발급채널") or "").strip(),
            lead_min_business_days=lead_min,
            lead_max_business_days=lead_max,
            fee_online_krw=_int_or_none(row.get("수수료_온라인", "")),
            fee_offline_krw=_int_or_none(row.get("수수료_방문", "")),
            auth_strength=(row.get("인증강도") or "NONE").strip() or "NONE",
            validity_days=validity,
            condition=_clean(row.get("조건분기", "")),
            source_url=_clean(row.get("출처링크", "")),
            verification_kind=(row.get("검증유형") or "공적출처").strip() or "공적출처",
            verified=(row.get("검증상태") or "").strip() == "확인완료",
            evidence_quote=_clean(row.get("근거문구", "")),
            verified_on=_clean(row.get("검증일", "")),
            validity_source=_clean(row.get("유효기간_근거", "")),
            notes=_clean(row.get("비고", "")),
        )

    if not out:
        raise DocumentMasterError("서류 마스터가 비어 있습니다")
    return out


def load_master(path: Path | None = None) -> dict[str, DocumentSpec]:
    """CSV 파일에서 마스터를 읽는다."""
    target = path or MASTER_PATH
    if not target.exists():
        raise DocumentMasterError(f"서류 마스터 파일이 없습니다: {target}")
    with target.open(encoding="utf-8-sig", newline="") as fh:
        return parse_master(list(csv.DictReader(fh)))


@lru_cache(maxsize=1)
def master() -> dict[str, DocumentSpec]:
    """기본 마스터. 요청마다 CSV 를 다시 파싱하지 않는다."""
    return load_master()


# 공고문 표기 → doc_code. 정식 명칭과 실제로 쓰이는 말이 다른 것만 적는다.
#
# 이 표는 추측이 아니라 확인된 동의어다. 문자열 유사도로 맞추는 방법도 있지만,
# '재직증명서'와 '퇴직증명서'는 한 글자 차이인데 뜻이 정반대다. 비슷하다는 이유로
# 엉뚱한 서류의 소요일을 가져오면 계획이 조용히 틀린다. 그래서 자동 추론 대신
# 명시적으로 적고, 여기 없으면 '모름'으로 둔다 (관리자 큐로 간다).
#
# 새 표기가 발견되면 여기 추가한다 — 나중에 CSV 의 별칭 컬럼으로 옮긴다.
NAME_ALIASES: dict[str, str] = {
    "주민등록등본": "D001",
    "주민등록표등본": "D001",
    "주민등록초본": "D002",
    "주민등록표초본": "D002",
    "가족관계증명서": "D003",
    "기본증명서": "D005",
    "소득금액증명원": "D008",
    "소득금액증명서": "D008",
    "사실증명": "D009",
    "국세완납증명서": "D011",
    "납세증명서": "D011",
    "지방세완납증명서": "D012",
    "원천징수영수증": "D013",
    "건강보험자격득실확인서": "D014",
    "자격득실확인서": "D014",
    "건강보험납부확인서": "D015",
    "고용보험피보험자격이력내역서": "D017",
    "고용보험이력내역서": "D017",
    "국민연금가입증명서": "D018",
    "4대보험가입내역확인서": "D019",
    "졸업증명서": "D020",
    "재학증명서": "D022",
    "성적증명서": "D023",
    "건축물대장": "D024",
    "등기부등본": "D030",
    "부동산등기부등본": "D030",
    "임대차계약서": "D035",
    # 가평군 월세 공고가 이렇게 적는다. 같은 서류에 수식어가 붙은 것뿐이라
    # 별칭으로 흡수한다 — 매칭에 실패하면 소요일을 추정치로 돌려 계획이 흐려진다.
    "주택임대차계약서사본": "D035",
    "주택임대차계약서": "D035",
    # 확정일자·신고 여부는 계약서에 붙는 수식어다. 발급처는 여전히 본인 보관 계약서다.
    "확정일자가날인된임대차계약서사본": "D035",
    "확정일자부임대차계약서사본": "D035",
    "확정일자부임대차계약서": "D035",
    "통장사본": "D036",
    # 제출서류 목록에서 수식어 없는 '등본'/'초본'은 주민등록등·초본이다. 다른 등본
    # (가족관계·법인)은 공고가 그 이름을 그대로 적는다 — 실데이터 2,819건에서
    # 단독 '등본'이 다른 서류를 가리킨 예는 없었다.
    "등본": "D001",
    "초본": "D002",
}


@lru_cache(maxsize=1)
def _name_index() -> dict[str, str]:
    """정규화한 서류명 → doc_code.

    공고문은 같은 서류를 제각각 적는다("주민등록등본" / "주민등록표 등본").
    공백과 괄호 주기를 떼고 맞춰 보고, 그래도 안 되면 별칭표를 본다.
    둘 다 실패하면 추측하지 않는다 — 엉뚱한 서류의 소요일로 역산하느니
    '모름'이 낫다 (관리자 큐로 가서 사람이 매핑한다).
    """
    index: dict[str, str] = {}
    for spec in master().values():
        for key in _name_keys(spec.name):
            index.setdefault(key, spec.doc_code)
    # 별칭이 정식 명칭을 덮어쓰지 않도록 뒤에 넣는다
    table = master()
    for alias, code in NAME_ALIASES.items():
        if code not in table:
            raise DocumentMasterError(f"별칭 {alias!r} 이 없는 doc_id {code} 를 가리킵니다")
        index.setdefault(alias.replace(" ", ""), code)
    return index


def _name_keys(name: str) -> list[str]:
    base = name.replace(" ", "")
    keys = [base]
    if "(" in base:
        keys.append(base.split("(", 1)[0])  # '가족관계증명서(상세)' → '가족관계증명서'
    return [k for k in keys if k]


def resolve(doc_code: str | None, name: str) -> DocumentSpec | None:
    """공고의 서류 항목을 마스터 항목에 맞춘다. 못 찾으면 None (관리자 큐 대상).

    doc_code 가 있으면 그것이 우선이다. 이름 매칭은 실패할 수 있는 추측이고,
    코드는 배치가 이미 확인한 결과이기 때문이다.
    """
    table = master()
    if doc_code and doc_code in table:
        return table[doc_code]

    normalized = (name or "").replace(" ", "")
    if not normalized:
        return None
    index = _name_index()
    if normalized in index:
        return table[index[normalized]]
    if "(" in normalized and normalized.split("(", 1)[0] in index:
        return table[index[normalized.split("(", 1)[0]]]
    return None
