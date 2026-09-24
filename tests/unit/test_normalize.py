"""온통청년 레코드 → PolicySchema 변환.

실제 API 응답 형태(전부 문자열, 빈 값은 "" 또는 공백)를 흉내 낸 최소 레코드로 검사한다.
"""

from __future__ import annotations

from datetime import date

from app.schemas.validate import validate_policy
from batch.collect.normalize import NATIONWIDE_MIN_CODES, record_to_policy


def rec(**over: str) -> dict[str, str]:
    base = {
        "plcyNo": "P1",
        "plcyNm": "테스트 정책",
        "lclsfNm": "일자리",
        "pvsnInstGroupCd": "0054002",
        "rgtrInstCd": "3860000",
        "rgtrUpInstCd": "6410000",
        "sprvsnInstCdNm": "경기도 부천시",
        "zipCd": "41192,41194",
        "sprtTrgtMinAge": "19",
        "sprtTrgtMaxAge": "39",
        "sprtTrgtAgeLmtYn": "Y",  # 신뢰 불가 플래그 — 값이 있으면 무시돼야 한다
        "mrgSttsCd": "0055003",
        "jobCd": "0013010",
        "schoolCd": "0049010",
        "earnCndSeCd": "0043001",
        "aplyPrdSeCd": "0057001",
        "aplyYmd": "20260910 ~ 20261004",
        "refUrlAddr1": "https://example.kr/p1",
        "bizPrdBgngYmd": "        ",  # 공백 8칸이 실제 빈 값이다
    }
    base.update(over)
    return base


def rules_of(p):
    return {r.field: r for r in p.eligibility}


def test_구조화_필드가_룰과_기간으로_옮겨지고_검증을_통과한다():
    p = record_to_policy(rec(), crawled_at="20260914T000000Z")
    assert validate_policy(p) == []
    assert p.status == "published"
    r = rules_of(p)
    assert r["region_code"].op == "in" and r["region_code"].value == ["41192", "41194"]
    assert r["age"].op == "between" and r["age"].value == [19, 39]
    assert r["age"].time_satisfiable
    assert p.period.apply_start == "2026-09-10" and p.period.apply_end == "2026-10-04"
    assert p.meta.category == "job" and p.meta.authority_level == "local"
    assert p.source.origin_url == "https://example.kr/p1"
    assert p.quality.needs_review_fields == []


def test_전국_정책은_지역_코드가_00_하나로_접힌다():
    codes = ",".join(f"{11000 + i}" for i in range(NATIONWIDE_MIN_CODES))
    p = record_to_policy(rec(zipCd=codes))
    assert rules_of(p)["region_code"].value == ["00"]
    assert p.meta.region_code == ["00"]


def test_모르는_코드는_룰을_만들지_않고_검토_필드에_남긴다():
    """틀린 부적격보다 '확인 필요'가 낫다. 조용히 통과시키지도 않는다."""
    p = record_to_policy(rec(jobCd="0013003,0013009", schoolCd="0049009", earnCndSeCd="0043003"))
    assert "employment_status" not in rules_of(p)
    assert "education" not in rules_of(p)
    assert p.quality.needs_review_fields == [
        "employment_status",
        "education",
        "household_income_ratio_median",
    ]


def test_확인된_코드는_사용자_필드_값으로_매핑된다():
    p = record_to_policy(rec(jobCd="0013001,0013003", mrgSttsCd="0055001", schoolCd="0049005"))
    r = rules_of(p)
    assert r["employment_status"].value == ["employed", "job_seeking", "neet"]
    assert r["marital_status"].value == "married"
    assert r["education"].value == ["university_enrolled"]
    # 나이·지역은 프로필 필수값이라 묻지 않는다. 나머지는 역질문으로 해소 가능해야 한다
    for f in ("employment_status", "marital_status", "education"):
        assert r[f].askable and r[f].question_template


def test_졸업_예정은_재학으로_접는다():
    """'졸업 예정자'는 아직 졸업자가 아니다.

    코드표(docs/ontong_codes.md)에는 '고졸 예정'·'대졸 예정'이 따로 있지만, 우리
    Education enum 에는 없다. 졸업으로 접으면 아직 재학 중인 사용자가 '졸업자 대상'
    공고에 적격으로 나간다 — 반대 방향으로 접어야 안전하다.
    """
    assert rules_of(record_to_policy(rec(schoolCd="0049003")))["education"].value == [
        "high_school_enrolled"
    ]
    assert rules_of(record_to_policy(rec(schoolCd="0049006")))["education"].value == [
        "university_enrolled"
    ]


def test_일하는_형태를_employed_로_넓히지_않는다():
    """코드 의미를 안다고 매핑하면 안 되는 경우.

    프리랜서·일용근로자·단기근로자·영농종사자는 전부 '일하는 중'이지만 enum 의
    `employed` 보다 좁다. `employed` 로 적으면 '일용직 대상' 공고가 정규직
    사용자에게도 적격으로 나간다 — 확인 필요로 남기는 쪽이 낫다.

    코드표를 받았다고 이걸 채우고 싶어지는 자리라, 못 채우게 막아 둔다.
    """
    for code in ("0013004", "0013005", "0013007", "0013008"):
        p = record_to_policy(rec(jobCd=code))
        assert "employment_status" not in rules_of(p), code
        assert "employment_status" in p.quality.needs_review_fields, code


def test_원문_링크가_없으면_게시하지_않고_마감이면_expired():
    assert record_to_policy(rec(refUrlAddr1="")).status == "draft"
    assert record_to_policy(rec(aplyPrdSeCd="0057003", aplyYmd="")).status == "expired"
    p = record_to_policy(rec(aplyPrdSeCd="0057002", aplyYmd=""))
    assert p.period.is_rolling and p.period.apply_end is None


def test_나이가_0이면_나이_룰을_만들지_않는다():
    p = record_to_policy(rec(sprtTrgtMinAge="0", sprtTrgtMaxAge="0"))
    assert "age" not in rules_of(p)
    p = record_to_policy(rec(sprtTrgtMinAge="19", sprtTrgtMaxAge="0"))
    assert rules_of(p)["age"].op == ">=" and rules_of(p)["age"].value == 19


# --- 신청 기간이 끝난 정책 -------------------------------------------------


def test_종료일이_지나면_코드와_무관하게_마감이다():
    """API 는 마감 코드(0057003)를 늦게 붙인다. 날짜가 먼저 말해준다.

    국토부 청년월세가 5/29 에 끝났는데 9월까지 기간 코드(0057001)로 남아 있었고,
    그래서 published 로 판정에 들어가 조합 추천에 480만원으로 잡혔다.
    """
    p = record_to_policy(
        rec(aplyPrdSeCd="0057001", aplyYmd="20260201 ~ 20260529"),
        today=date(2026, 9, 19),
    )
    assert p.period.apply_end == "2026-05-29"
    assert p.status == "expired"


def test_마감일_당일은_아직_열려_있다():
    p = record_to_policy(
        rec(aplyPrdSeCd="0057001", aplyYmd="20260201 ~ 20260929"),
        today=date(2026, 9, 29),
    )
    assert p.status == "published"


def test_상시모집은_종료일이_없어_영향받지_않는다():
    p = record_to_policy(rec(aplyPrdSeCd="0057002"), today=date(2099, 1, 1))
    assert p.period.is_rolling is True
    assert p.status == "published"
