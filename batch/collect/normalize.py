"""온통청년 API 레코드 → PolicySchema. LLM 없이 구조화된 필드만 옮기는 1차 변환.

API 가 코드로 준 것(나이·결혼·취업·학력·지역·신청기간·분류·출처)만 룰로 만든다.
소득 조건·필요서류·중복수혜는 자유 텍스트라 여기서 만들지 않는다 — 추측으로 룰을
만들면 '틀린 부적격'이 나오고, 그건 신고가 들어와도 원인을 찾기 어렵다.
표현 못 한 조건은 quality.needs_review_fields 에 남겨서 A2(LLM) 단계와 관리자 큐가
이어받게 한다.

코드값 의미는 **온통청년 공식 코드표**(`docs/ontong_codes.md`)가 원본이다. 처음에는
2026-09-14 전체 2,774건의 자유 텍스트와 대조해 추정했는데, 2026-09-24 에 코드표를
받아 전부 확인했다 — 추정이 틀린 건 없었다.

**코드 의미를 안다고 전부 매핑하지는 않는다.** 우리 enum 에 정확히 대응하는 것만
룰로 만들고, 나머지는 `needs_review` 로 넘긴다. 예를 들어 `0013005 일용근로자` 를
`employed` 로 적으면 "일용직 대상" 공고가 **정규직 사용자에게도 적격**으로 나간다.
확인 필요로 남기면 사용자가 한 번 더 확인하지만, 넓게 매핑하면 받을 수 없는 정책을
받을 수 있다고 알려주게 된다. 후자가 더 나쁘다.

  mrgSttsCd   0055001 기혼("부부")  0055002 미혼  0055003 제한없음
  jobCd       0013001 재직  0013002 자영업  0013003 미취업/구직  0013006 창업
              0013010 제한없음
              매핑 안 함: 0013004 프리랜서 · 0013005 일용근로자 · 0013007 단기근로자 ·
              0013008 영농종사자 · 0013009 기타 — 전부 '일하는 중'이지만 우리 enum 의
              `employed` 보다 좁다. 넓혀 적으면 위 문단의 오탐이 난다
  schoolCd    0049001 고졸미만  0049002 고교재학  0049003 고졸예정(= 재학 중)
              0049004 고교졸업  0049005 대학재학  0049006 대졸예정(= 재학 중)
              0049007 대학졸업  0049008 대학원  0049010 제한없음
              매핑 안 함: 0049009 기타
  aplyPrdSeCd 0057001 기간("YYYYMMDD ~ YYYYMMDD")  0057002 상시  0057003 마감
  pvsnInstGroupCd 0054001 중앙부처  0054002 지자체
  sprtTrgtAgeLmtYn 은 Y/N 양쪽에 나이가 들어 있어 신뢰할 수 없다 → 무시하고 min/max 만 본다
  zipCd       법정동 5자리 쉼표 목록. 시도 하나면 ≤47개, 전국이면 ≥189개 (그 사이는 없다)
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import date
from typing import Any

from app.core.clock import today_kst
from app.schemas.enums import Category
from app.schemas.policy import Dept, Meta, Period, PolicySchema, Quality, Rule, Source

Record = dict[str, Any]

NATIONWIDE_MIN_CODES = 100  # 실측: 시도 단위 최대 47, 전국 최소 189


def code_universe(records: Iterable[Record]) -> frozenset[str]:
    """이 묶음에 등장한 모든 **시군구** 코드. '전국'이 몇 개인지의 기준이 된다.

    레코드 하나만 보면 zipCd 238개가 전국인지, 전국에서 18개를 뺀 것인지 알 수
    없다. 묶음 전체의 합집합을 봐야 판단이 선다.

    5자리가 아닌 코드는 뺀다. 광역 단위 공고는 zipCd 에 `41`(경기) 처럼 2자리를
    적는데, 그게 합집합에 섞이면 **어떤 전국 공고도 합집합을 덮지 못한다** —
    전국 목록은 시군구만 열거하기 때문이다. 그러면 전국 공고가 전부 '전국 아님'이
    되어 코드 250여 개짜리 룰로 나간다.
    """
    return frozenset(
        c for rec in records for c in _codes(rec, "zipCd") if len(c) == 5
    )


def _si_code(code: str) -> str | None:
    """구 코드면 그 구가 속한 시 코드. 시·군 코드면 None.

    법정동 코드는 시군구가 5자리고, 구 있는 시는 시가 끝자리 0, 구가 1~9 로
    같은 앞 4자리를 쓴다 (용인시 41460 · 처인구 41461 · 기흥구 41463).
    """
    if len(code) == 5 and code[4] != "0":
        return code[:4] + "0"
    return None


def resolve_region(
    codes: list[str], universe: frozenset[str] | None
) -> tuple[list[str], list[str], str]:
    """zipCd 목록을 룰 값과 근거 문구로 바꾼다.

    두 가지를 같이 처리한다. 둘 다 **조용히 틀리는** 쪽이라 신고가 안 들어온다.

    1. **전국으로 접으면 제외 지자체가 사라진다.**
       농식품 바우처는 zipCd 가 238개인데 전국은 256개고, 빠진 18곳이 공고문에
       "미추진"으로 적힌 부천·수원·안산 등이다. 개수만 보고 접으면 부천 사용자가
       받을 수 없는 정책을 적격으로 받는다. 그래서 묶음의 합집합(`universe`)을
       전부 덮을 때만 접는다.

    2. **시 단위로 입력한 사용자가 구 단위 공고를 못 본다.**
       API 는 용인을 41461·41463·41465(처인·기흥·수지)로 적는다. 사용자가
       41460(용인시)으로 입력하면 `region_chain` 이 ['00','41','41460'] 이라
       교집합이 비고, 정책이 **목록에서 사라진다.** 부적격도 확인필요도 아니라
       화면에 아무것도 안 남는다. 그래서 한 시의 구가 전부 있으면 시 코드도
       같이 넣는다.

       '전부'는 `universe` 로 판단한다. 없으면 구가 하나라도 있을 때 시 코드를
       넣는다 — 처인구 전용 공고에 기흥구 사용자가 딸려오는 과잉 포함이 생기지만,
       정책이 통째로 사라지는 것보다는 낫다 (CLAUDE.md).

    (판정용 룰 값, 표시용 meta 값, 근거 문구)를 돌려준다. **둘을 나누는 이유**:
    판정은 250여 개 목록이 필요하지만, 그걸 `meta.region_code` 에까지 넣으면
    목록 응답 한 페이지가 40KB 씩 불어난다. 목록은 접힌 값으로 넓게 걸러 보여주고
    (사용자는 카드를 본 뒤 판정에서 정확한 사유를 받는다), 판정만 명시 목록으로
    한다. `judge.py` 의 필터는 meta 를, 엔진은 룰을 본다.
    """
    if not codes:
        return [], [], ""

    given = set(codes)
    expanded = _with_si_codes(given, universe)

    if universe is not None:
        # 같은 장소를 시(41590)로 적은 공고와 구(41591·41593…)로 적은 공고가 섞여
        # 있다. 양쪽 다 시 코드까지 펼친 뒤에 비교해야 단위가 맞는다.
        if expanded >= _with_si_codes(set(universe), universe):
            return ["00"], ["00"], f"전국 (zipCd {len(codes)}개 — 수집 묶음의 전 시군구)"
    elif len(codes) >= NATIONWIDE_MIN_CODES:
        # 기준 집합 없이 개수로만 판단하는 경로. 정확하지 않으므로 문구에 남긴다.
        return ["00"], ["00"], f"전국으로 간주 (zipCd {len(codes)}개, 기준 집합 없음)"

    value = sorted(expanded)
    added = sorted(expanded - given)
    quote = f"시행 지역 zipCd: {', '.join(codes)}"
    if added:
        quote += f" (구 전체라 시 코드 {', '.join(added)} 포함)"
    # 표시용은 접는다. 목록에서 넓게 보여주고 정확한 사유는 판정이 낸다.
    display = ["00"] if len(value) >= NATIONWIDE_MIN_CODES else value
    return value, display, quote


def _with_si_codes(codes: set[str], universe: frozenset[str] | None) -> set[str]:
    """구 코드 묶음에 그 구들이 속한 시 코드를 더한다.

    `universe` 가 있으면 **그 시의 구가 전부 있을 때만** 더한다. 없으면 구가
    하나라도 있으면 더한다 — 처인구 전용 공고에 기흥구 사용자가 딸려오는 과잉
    포함이 생기지만, 정책이 목록에서 통째로 사라지는 것보다는 낫다.
    """
    out = set(codes)
    for code in codes:
        si = _si_code(code)
        if si is None or si in out:
            continue
        if universe is None:
            out.add(si)
            continue
        siblings = {c for c in universe if _si_code(c) == si}
        if siblings and siblings <= codes:
            out.add(si)
    return out

_CATEGORY: dict[str, Category] = {
    "일자리": "job",
    "주거": "housing",
    "교육": "education",
    "교육･직업훈련": "education",
    "복지문화": "welfare",
    "금융･복지･문화": "welfare",
    "참여권리": "participation",
    "참여･기반": "participation",
}
_MARITAL = {"0055001": "married", "0055002": "single"}
_JOB: dict[str, list[str]] = {
    "0013001": ["employed"],
    "0013002": ["founder"],
    "0013003": ["job_seeking", "neet"],
    "0013006": ["founder"],
}
# 공식 코드표(docs/ontong_codes.md)에 1:1 로 대응하는 것만 넣는다.
# '예정'은 아직 재학 중이라는 뜻이라 재학으로 접는다 — 졸업 예정자는 졸업자가 아니다.
_SCHOOL = {
    "0049001": "middle_or_below",
    "0049002": "high_school_enrolled",
    "0049003": "high_school_enrolled",
    "0049004": "high_school_graduated",
    "0049005": "university_enrolled",
    "0049006": "university_enrolled",
    "0049007": "university_graduated",
    "0049008": "graduate_school",
}
_NO_LIMIT = {"0055003", "0013010", "0049010"}
_DATE_RANGE = re.compile(r"(\d{8})\s*~\s*(\d{8})")


def _s(rec: Record, key: str) -> str:
    """빈 값이 "" 일 수도, 공백 8칸일 수도 있다. strip 없이 비교하면 틀린다."""
    return str(rec.get(key) or "").strip()


def _codes(rec: Record, key: str) -> list[str]:
    return [c.strip() for c in _s(rec, key).split(",") if c.strip()]


def _window_closed(period: Period, today: date) -> bool:
    """신청 기간이 끝났는가. 마감일 당일은 아직 열려 있다."""
    if period.is_rolling or not period.apply_end:
        return False
    try:
        return date.fromisoformat(period.apply_end) < today
    except ValueError:
        return False  # 못 읽는 날짜로 상태를 바꾸지 않는다. 빌더가 경고를 남긴다


def _iso(yyyymmdd: str) -> str:
    return f"{yyyymmdd[:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:]}"


def record_to_policy(
    rec: Record,
    *,
    crawled_at: str | None = None,
    today: date | None = None,
    universe: frozenset[str] | None = None,
) -> PolicySchema:
    plcy_no = _s(rec, "plcyNo")
    today = today or today_kst()
    review: list[str] = []
    rules: list[Rule] = []

    def rule(rule_id: str, field: str, op: str, value: Any, quote: str, **kw: Any) -> None:
        rules.append(
            Rule(
                rule_id=f"{plcy_no}:{rule_id}",
                field=field,
                op=op,  # type: ignore[arg-type]
                value=value,
                source_quote=quote,
                basis="온통청년 OPEN API 구조화 필드",
                **kw,
            )
        )

    # --- 지역: 엔진은 meta.region_code 로 거르지 않으므로 룰로도 넣는다 ---------
    region, region_display, quote = resolve_region(_codes(rec, "zipCd"), universe)
    if region:
        rule("region", "region_code", "in", region, quote)

    # --- 나이 -----------------------------------------------------------------
    lo, hi = _s(rec, "sprtTrgtMinAge"), _s(rec, "sprtTrgtMaxAge")
    lo_n, hi_n = int(lo) if lo.isdigit() else 0, int(hi) if hi.isdigit() else 0
    if lo_n and hi_n:
        rule("age", "age", "between", [lo_n, hi_n], f"지원 대상 연령 {lo_n}세 ~ {hi_n}세",
             unit="세", time_satisfiable=True)
    elif lo_n:
        rule("age", "age", ">=", lo_n, f"지원 대상 연령 {lo_n}세 이상", unit="세",
             time_satisfiable=True)
    elif hi_n:
        rule("age", "age", "<=", hi_n, f"지원 대상 연령 {hi_n}세 이하", unit="세")

    # --- 결혼 · 취업 · 학력: 확인된 코드만, 하나라도 모르면 룰을 만들지 않는다 ----
    mr = _s(rec, "mrgSttsCd")
    if mr in _MARITAL:
        rule("marital", "marital_status", "==", _MARITAL[mr],
             f"혼인 상태: {'기혼' if mr == '0055001' else '미혼'} (mrgSttsCd={mr})",
             askable=True, question_template="현재 혼인 상태를 알려주세요.")
    elif mr and mr not in _NO_LIMIT:
        review.append("marital_status")

    jobs = _codes(rec, "jobCd")
    if jobs and jobs != ["0013010"]:
        if all(j in _JOB for j in jobs):
            values = sorted({v for j in jobs for v in _JOB[j]})
            rule("job", "employment_status", "in", values,
                 f"취업 상태 요건 (jobCd={','.join(jobs)})",
                 askable=True, question_template="현재 취업 상태를 알려주세요.")
        else:
            review.append("employment_status")

    schools = _codes(rec, "schoolCd")
    if schools and schools != ["0049010"]:
        if all(s in _SCHOOL for s in schools):
            rule("school", "education", "in", sorted({_SCHOOL[s] for s in schools}),
                 f"학력 요건 (schoolCd={','.join(schools)})",
                 askable=True, question_template="최종 학력을 알려주세요.")
        else:
            review.append("education")

    # 소득 조건은 코드로는 '있다/없다'만 알 수 있고 기준은 텍스트뿐이다
    if _s(rec, "earnCndSeCd") not in ("", "0043001"):
        review.append("household_income_ratio_median")

    # --- 신청 기간 ------------------------------------------------------------
    period = Period()
    se = _s(rec, "aplyPrdSeCd")
    if se == "0057002":
        period.is_rolling = True
    elif m := _DATE_RANGE.search(_s(rec, "aplyYmd")):  # 여러 구간이면 첫 구간
        period.apply_start, period.apply_end = _iso(m.group(1)), _iso(m.group(2))

    # --- 출처 · 분류 · 기관 ---------------------------------------------------
    urls = [u for k in ("refUrlAddr1", "refUrlAddr2", "aplyUrlAddr") if (u := _s(rec, k))]
    origin = next((u for u in urls if u.startswith("http")), None)
    lclsf = _s(rec, "lclsfNm").split(",")[0]
    if lclsf not in _CATEGORY:
        review.append("category")  # 기본값으로 메우되 조용히 메우지는 않는다
    central = _s(rec, "pvsnInstGroupCd") == "0054001"
    # ponytail: 지자체는 상위기관 코드가 자기 자신이면 광역으로 본다. 시청이 자기
    # 자신을 상위로 등록한 경우 광역으로 잘못 잡히지만, 이 값은 same_authority 상충
    # 판정에만 쓰이고 지금 변환은 상충을 만들지 않는다. 기관 코드표를 얻으면 교체.
    level = (
        "central" if central
        else "province" if _s(rec, "rgtrInstCd") == _s(rec, "rgtrUpInstCd")
        else "local"
    )

    # 마감 코드(0057003)뿐 아니라 종료일이 지난 기간제 정책도 expired 다.
    # 코드만 보면 국토부 청년월세처럼 5/29 에 끝난 공고가 계속 published 로 남는다 —
    # 빌더가 한 번 더 거르지만, status 자체가 틀린 채로 리포트와 관리자 큐에
    # 흘러가면 "왜 빠졌는지"를 읽는 사람이 status 를 믿고 엉뚱한 곳을 본다.
    if se == "0057003" or _window_closed(period, today):
        status = "expired"
    elif origin and rules:
        status = "published"
    else:
        status = "draft"  # 원문 링크 없음 → 게시 불가 (PRD §7.6)

    return PolicySchema(
        policy_id=plcy_no,
        status=status,  # type: ignore[arg-type]
        meta=Meta(
            title=_s(rec, "plcyNm"),
            category=_CATEGORY.get(lclsf, "welfare"),
            authority_level=level,  # type: ignore[arg-type]
            region_code=region_display,
            dept=Dept(name=_s(rec, "sprvsnInstCdNm") or _s(rec, "rgtrInstCdNm") or None),
        ),
        source=Source(
            api="ontong",
            api_policy_no=plcy_no,
            origin_url=origin,
            announcement_url=_s(rec, "aplyUrlAddr") or None,
            crawled_at=crawled_at,
        ),
        period=period,
        eligibility=rules,
        quality=Quality(
            needs_review_fields=review,
            last_verified_at=_s(rec, "lastMdfcnDt") or None,
        ),
    )
