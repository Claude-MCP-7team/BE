"""판정 API 테스트 (계약면 C2 — FE 가 이 스펙으로 개발한다)."""

import json

import msgspec
import pytest
from fastapi.testclient import TestClient

from app.engine.snapshot import SnapshotHolder, load_from_json
from app.engine.snapshot import holder as global_holder

BODY = {
    "core": {
        "birth_date": "2001-03-14",
        "region_code": "41465",
        "residence_start_date": "2026-05-15",
    }
}

POLICIES = [
    {
        "policy_id": "YONGIN-RENT",
        "status": "published",
        "meta": {
            "title": "용인시 청년 월세 지원",
            "category": "housing",
            "authority_level": "local",
            "region_code": ["41465"],
            "dept": {"name": "청년정책과", "tel": "031-324-0000"},
        },
        "source": {"origin_url": "https://example.kr/yongin"},
        "eligibility": [
            {"rule_id": "AGE", "field": "age", "op": "between", "value": [19, 34],
             "source_quote": "만 19세 이상 34세 이하 청년"},
            {"rule_id": "RES", "field": "residence_months_continuous", "op": ">=", "value": 6,
             "unit": "months", "source_quote": "6개월 이상 계속하여 거주"},
        ],
    },
    {
        "policy_id": "MOLIT-RENT",
        "status": "published",
        "meta": {"title": "청년월세 한시 특별지원", "category": "housing",
                 "authority_level": "central", "region_code": ["00"],
                 "dept": {"name": "국토교통부", "tel": "1599-0000"}},
        "source": {"origin_url": "https://example.kr/molit"},
        "eligibility": [
            {"rule_id": "AGE", "field": "age", "op": "between", "value": [19, 34],
             "source_quote": "만 19~34세"},
            {"rule_id": "REG", "field": "region_code", "op": "in", "value": ["00"],
             "source_quote": "전국"},
        ],
    },
    {
        "policy_id": "ASK-ME",
        "status": "published",
        "meta": {"title": "확인 필요 정책", "category": "welfare", "authority_level": "local",
                 "region_code": ["41465"], "dept": {"name": "복지과", "tel": "031-111-1111"}},
        "source": {"origin_url": "https://example.kr/ask"},
        "eligibility": [
            {"rule_id": "INC", "field": "household_income_ratio_median", "op": "<=", "value": 150,
             "confidence": "ESTIMATED", "askable": True,
             "question_template": "가구 소득이 기준 중위소득 150% 이하인가요?",
             "source_quote": "기준 중위소득 150% 이하"},
        ],
    },
    {
        # 기다려도 안 되는 부적격 — 기본 응답에서 빠지는지 확인하는 대조군
        "policy_id": "TOO-OLD",
        "status": "published",
        "meta": {"title": "중장년 정책", "category": "job", "authority_level": "local",
                 "region_code": ["41465"], "dept": {"name": "일자리과", "tel": "031-222-2222"}},
        "source": {"origin_url": "https://example.kr/too-old"},
        "eligibility": [
            {"rule_id": "AGE", "field": "age", "op": "between", "value": [50, 64],
             "source_quote": "만 50세 이상 64세 이하"},
        ],
    },
]


@pytest.fixture
def client(monkeypatch):
    """매 테스트마다 깨끗한 스냅샷을 적재한 앱."""
    monkeypatch.setenv("YPC_FIXED_TODAY", "2026-09-13")
    load_from_json(global_holder, json.dumps(POLICIES).encode(), version="test-v1")

    from app.main import app

    with TestClient(app) as c:
        yield c


def post(client, body=None, **params):
    return client.post("/v1/judge", json=body or BODY, params=params)


# --- 기본 동작 --------------------------------------------------------------


def test_판정은_3분류_건수를_요약으로_준다(client):
    d = post(client).json()
    s = d["summary"]
    assert s["eligible"] + s["ineligible"] + s["needs_info"] == len(POLICIES)
    assert s["eligible"] == 1  # 전국 정책만
    assert s["ineligible"] == 2  # 용인(거주 미달) + 중장년(연령 상한)
    assert s["needs_info"] == 1  # 소득 미입력


def test_요약의_future_eligible_은_부적격의_부분집합이다(client):
    """네 번째 배지가 쓰는 값. 총계에 다시 더하면 정책 수가 부풀려진다."""
    s = post(client).json()["summary"]
    assert s["future_eligible"] == 1  # 용인만 — 중장년은 기다려도 안 된다
    assert s["future_eligible"] <= s["ineligible"]
    assert s["eligible"] + s["ineligible"] + s["needs_info"] == len(POLICIES)


def test_기본_응답은_영영_안_되는_부적격만_뺀다(client):
    """부적격 수백 건의 근거를 매번 실으면 응답이 몇 배가 되고 대부분 안 읽힌다.

    다만 '시간이 지나면 가능한' 부적격은 다르다. 사용자가 지금 무엇을 할지
    정하는 데 쓰는 정보라, 빼면 기본 화면에서 "언제부터 가능한가"가 사라진다.
    """
    ids = {r["policy_id"] for r in post(client).json()["results"]}
    assert "TOO-OLD" not in ids  # 연령 상한 초과 — 기다려도 안 된다
    assert "YONGIN-RENT" in ids  # 거주기간 미달 — 2026-11-15 부터 가능
    assert ids == {"MOLIT-RENT", "ASK-ME", "YONGIN-RENT"}


def test_include_all_이면_부적격_근거까지_준다(client):
    results = {r["policy_id"]: r for r in post(client, include="all").json()["results"]}
    assert len(results) == len(POLICIES)
    unmatched = results["YONGIN-RENT"]["unmatched"]
    assert unmatched[0]["rule_id"] == "RES"
    assert unmatched[0]["satisfiable_from"] == "2026-11-15"


def test_모든_결과에_원문_인용과_링크가_붙는다(client):
    """차별점 ②·⑥ — 근거 없는 판정은 내보내지 않는다."""
    for r in post(client, include="all").json()["results"]:
        assert r["origin_url"]
        for item in [*r["matched"], *r["unmatched"], *r["unknown"]]:
            assert item["source_quote"].strip()
            assert item["source_url"]


def test_추정_판정에는_담당부서_연락처가_붙는다(client):
    results = {r["policy_id"]: r for r in post(client, include="all").json()["results"]}
    ask = results["ASK-ME"]
    assert ask["confidence"] == "ESTIMATED"
    assert ask["dept_tel"] == "031-111-1111"


def test_확인필요에는_역질문이_담긴다(client):
    results = {r["policy_id"]: r for r in post(client).json()["results"]}
    ask = results["ASK-ME"]
    assert ask["verdict"] == "NEEDS_INFO"
    assert ask["unknown"][0]["question_template"] == "가구 소득이 기준 중위소득 150% 이하인가요?"


def test_면책_고지가_항상_실린다(client):
    assert "법적 효력이 없습니다" in post(client).json()["disclaimer"]


# --- 설명문 (C2) ---------------------------------------------------------------


def test_기본으로_모든_결과에_템플릿_설명문이_붙는다(client):
    results = {r["policy_id"]: r for r in post(client, include="all").json()["results"]}
    yongin = results["YONGIN-RENT"]["explanation"]
    assert "내 값 3개월, 공고 기준 6개월" in yongin
    assert "2026-11-15부터 충족돼요" in yongin
    assert '"6개월 이상 계속하여 거주"' in yongin
    assert "1가지 정보가 더 필요해요" in results["ASK-ME"]["explanation"]
    assert "모두 충족해요" in results["MOLIT-RENT"]["explanation"]


def test_explain_none_이면_설명문을_생략한다(client):
    for r in post(client, explain="none").json()["results"]:
        assert r["explanation"] is None


def test_explain_llm_은_키가_없으면_템플릿과_같다(client, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    a = post(client, include="all", explain="template").json()["results"]
    b = post(client, include="all", explain="llm").json()["results"]
    assert [r["explanation"] for r in a] == [r["explanation"] for r in b]


def test_설명문_방식이_다르면_ETag_도_다르다(client):
    assert post(client).headers["etag"] != post(client, explain="none").headers["etag"]


def test_역질문에_답하면_분류가_바뀐다(client):
    body = {**BODY, "answers": {"household_income_ratio_median": 120}}
    before = post(client).json()["summary"]
    after = post(client, body).json()["summary"]
    assert before["needs_info"] == 1
    assert after["needs_info"] == 0
    assert after["eligible"] == before["eligible"] + 1


# --- 캐시 · 재계산 방지 ------------------------------------------------------


def test_같은_조건_재요청은_304(client):
    etag = post(client).headers["etag"]
    again = client.post("/v1/judge", json=BODY, headers={"If-None-Match": etag})
    assert again.status_code == 304


def test_조건이_바뀌면_304가_아니다(client):
    etag = post(client).headers["etag"]
    other = {"core": {"birth_date": "1990-01-01", "region_code": "41465"}}
    assert client.post("/v1/judge", json=other, headers={"If-None-Match": etag}).status_code == 200


def test_include_가_다르면_다른_ETag(client):
    """응답 내용이 다른데 같은 ETag 를 주면 FE 가 빈 목록을 캐시한다."""
    assert post(client).headers["etag"] != post(client, include="all").headers["etag"]


def test_판정_응답은_공용_캐시에_남지_않는다(client):
    """조건값이 섞인 응답이라 프록시에 남으면 개인정보 유출이다."""
    cache = post(client).headers["cache-control"]
    assert "private" in cache and "no-store" in cache


def test_응답에_스냅샷_버전이_실린다(client):
    r = post(client)
    assert r.headers["x-snapshot-version"] == "test-v1"
    assert r.json()["snapshot_version"] == "test-v1"


# --- 에러 경로 --------------------------------------------------------------


def test_잘못된_입력은_422(client):
    assert client.post("/v1/judge", json={"core": {"birth_date": "이천일년"}}).status_code == 422


def test_없는_정책은_404(client):
    assert client.get("/v1/policies/NOPE").status_code == 404


def test_정책_상세는_공고_원문을_그대로_준다(client):
    d = client.get("/v1/policies/YONGIN-RENT").json()
    assert d["policy_id"] == "YONGIN-RENT"
    assert d["eligibility"][0]["source_quote"]


# --- 헬스체크는 '살아있음'과 '서비스 가능'을 구분한다 ------------------------


def test_healthz_는_스냅샷과_무관하게_200(client):
    assert client.get("/healthz").json() == {"status": "ok"}


def test_readyz_는_스냅샷_상태를_반영한다(client):
    d = client.get("/readyz").json()
    assert d["ready"] is True
    assert d["policy_count"] == len(POLICIES)


def test_스냅샷이_없으면_판정은_503():
    """빈 인스턴스가 트래픽을 받아 전부 실패하는 상황을 막는다."""
    import app.engine.snapshot as snap_mod
    from app.main import app

    original = snap_mod.holder
    snap_mod.holder = SnapshotHolder()  # 비어 있는 보관소로 교체
    try:
        with TestClient(app) as c:
            assert c.get("/readyz").status_code == 503
            assert c.post("/v1/judge", json=BODY).status_code == 503
            assert c.get("/healthz").status_code == 200  # 프로세스는 살아 있다
    finally:
        snap_mod.holder = original


# --- 스냅샷 핫스왑 안전성 ----------------------------------------------------


def test_깨진_스냅샷으로_교체해도_기존_것이_살아남는다():
    """배치가 망가진 산출물을 내놓아도 서비스는 계속 돌아야 한다."""
    h = SnapshotHolder()
    load_from_json(h, json.dumps(POLICIES).encode(), version="good")
    assert h.get().version == "good"

    for broken in (b"[]", b"{not json", json.dumps([POLICIES[0], POLICIES[0]]).encode()):
        with pytest.raises((ValueError, msgspec.ValidationError)):
            load_from_json(h, broken)

    assert h.get().version == "good"
    assert h.get().size == len(POLICIES)


# --- 역질문 큐 (S3) ---------------------------------------------------------


def test_역질문_큐를_돌려준다(client):
    d = client.post("/v1/questions", json=BODY).json()
    assert d["snapshot_version"] == "test-v1"
    assert d["needs_info_policies"] == 1
    assert len(d["questions"]) == 1
    q = d["questions"][0]
    assert q["field"] == "household_income_ratio_median"
    assert q["text"] == "가구 소득이 기준 중위소득 150% 이하인가요?"
    assert q["resolves"] == 1
    assert q["source_policy_ids"] == ["ASK-ME"]


def test_답을_채우면_큐가_비워진다(client):
    body = {**BODY, "answers": {"household_income_ratio_median": 120}}
    d = client.post("/v1/questions", json=body).json()
    assert d["questions"] == []
    assert d["needs_info_policies"] == 0


def test_역질문_응답도_공용_캐시에_남지_않는다(client):
    r = client.post("/v1/questions", json=BODY)
    assert "no-store" in r.headers["cache-control"]


def test_역질문도_잘못된_입력은_422(client):
    assert client.post("/v1/questions", json={"core": {"birth_date": "몰라"}}).status_code == 422


# --- 조합 추천 (S6) ---------------------------------------------------------


def test_조합_추천은_보수_최대_2안을_준다(client):
    d = client.post("/v1/combinations", json=BODY).json()
    assert [s["kind"] for s in d["scenarios"]] == ["conservative", "maximal"]
    assert d["snapshot_version"] == "test-v1"
    assert "법적 효력이 없습니다" in d["disclaimer"]


def test_조합에_적격_정책만_담긴다(client):
    d = client.post("/v1/combinations", json=BODY).json()
    assert d["eligible_count"] == 1
    members = {m["policy_id"] for m in d["scenarios"][0]["combinations"][0]["members"]}
    assert members == {"MOLIT-RENT"}


def test_조합_응답도_공용_캐시에_남지_않는다(client):
    r = client.post("/v1/combinations", json=BODY)
    assert "no-store" in r.headers["cache-control"]


def test_조합도_잘못된_입력은_422(client):
    assert client.post("/v1/combinations", json={"core": {"birth_date": "몰라"}}).status_code == 422


# --- 신청 계획 (S7) ---------------------------------------------------------

PLAN_POLICIES = [
    {
        "policy_id": "WITH-DEADLINE",
        "status": "published",
        "meta": {"title": "마감 있는 정책", "category": "housing", "authority_level": "central",
                 "region_code": ["00"], "dept": {"name": "국토교통부", "tel": "1599-0000"}},
        "source": {"origin_url": "https://example.kr/deadline"},
        "period": {"apply_end": "2026-10-30"},
        "eligibility": [
            {"rule_id": "AGE", "field": "age", "op": "between", "value": [19, 34],
             "source_quote": "만 19~34세"},
        ],
        "documents": [
            {"name": "주민등록등본", "doc_code": "RESIDENT_REG",
             "lead_time_business_days": 0, "cost_krw": 400},
            {"name": "소득금액증명", "doc_code": "INCOME_CERT",
             "lead_time_business_days": 3, "cost_krw": 0},
        ],
    },
    {
        "policy_id": "ROLLING",
        "status": "published",
        "meta": {"title": "상시 모집 정책", "category": "job", "authority_level": "central",
                 "region_code": ["00"], "dept": {"name": "고용노동부", "tel": "1350"}},
        "source": {"origin_url": "https://example.kr/rolling"},
        "period": {"is_rolling": True},
        "eligibility": [
            {"rule_id": "AGE", "field": "age", "op": "between", "value": [19, 34],
             "source_quote": "만 19~34세"},
        ],
        "documents": [
            {"name": "주민등록등본", "doc_code": "RESIDENT_REG",
             "lead_time_business_days": 0, "cost_krw": 400},
        ],
    },
]


@pytest.fixture
def plan_client(monkeypatch):
    monkeypatch.setenv("YPC_FIXED_TODAY", "2026-09-14")  # 월요일
    load_from_json(global_holder, json.dumps(PLAN_POLICIES).encode(), version="plan-v1")

    from app.main import app

    with TestClient(app) as c:
        yield c


def test_계획은_착수일과_서류를_준다(plan_client):
    d = plan_client.post("/v1/plan", json=BODY).json()
    assert d["snapshot_version"] == "plan-v1"
    assert d["generated_for_date"] == "2026-09-14"
    assert d["summary"]["total"] == 2

    item = next(p for p in d["plans"] if p["policy_id"] == "WITH-DEADLINE")
    assert item["status"] == "ON_TRACK"
    # 공고가 소득금액증명을 3일로 적었지만 마스터(D008)는 온라인 즉시 0일이다.
    # 마스터가 이긴다 — 공고는 수백 건이 제각각 틀리고 마스터는 한 곳에서 고친다.
    # 준비 1영업일(max 0 + 버퍼 1) → 마감 10/30(금)에서 10/29(목)
    assert item["recommended_start_date"] == "2026-10-29"
    assert item["preparation_business_days"] == 1
    income = next(x for x in item["documents"] if x["doc_code"] == "D008")
    assert income["lead_time_business_days"] == 0
    assert income["channel"]  # 마스터가 채널을 알려준다


def test_상시모집은_착수일을_만들지_않는다(plan_client):
    d = plan_client.post("/v1/plan", json=BODY).json()
    item = next(p for p in d["plans"] if p["policy_id"] == "ROLLING")
    assert item["status"] == "ROLLING"
    assert item["recommended_start_date"] is None


def test_같은_서류는_한_번만_떼게_묶인다(plan_client):
    """두 정책이 등본을 요구해도 사용자는 한 번만 간다.

    공고의 doc_code 가 마스터 코드가 아니어도 서류명으로 마스터를 찾아
    같은 D001 로 합쳐진다 (별칭 '주민등록등본' → D001).
    """
    d = plan_client.post("/v1/plan", json=BODY).json()
    tasks = [t for t in d["documents"] if t["doc_code"] == "D001"]
    assert len(tasks) == 1
    assert sorted(tasks[0]["required_by"]) == ["ROLLING", "WITH-DEADLINE"]
    # 마스터 기준 등본은 온라인 0원 (방문하면 400원)
    assert tasks[0]["cost_krw"] == 0


def test_특정_정책만_계획할_수_있다(plan_client):
    r = plan_client.post("/v1/plan", json=BODY, headers={"X-Policy-Ids": "ROLLING"})
    assert [p["policy_id"] for p in r.json()["plans"]] == ["ROLLING"]


def test_계획_응답도_공용_캐시에_남지_않는다(plan_client):
    r = plan_client.post("/v1/plan", json=BODY)
    assert "no-store" in r.headers["cache-control"]
    assert r.headers["x-snapshot-version"] == "plan-v1"


def test_계획도_잘못된_입력은_422(plan_client):
    assert plan_client.post("/v1/plan", json={"core": {"birth_date": "몰라"}}).status_code == 422


def test_ics_를_내려받을_수_있다(plan_client):
    r = plan_client.post("/v1/plan.ics", json=BODY)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/calendar")
    assert "attachment" in r.headers["content-disposition"]
    text = r.content.decode("utf-8")
    assert text.startswith("BEGIN:VCALENDAR")
    assert "DTSTART;VALUE=DATE:20261029" in text  # 착수일
    assert "DTSTART;VALUE=DATE:20261030" in text  # 마감일


def test_ics_모든_줄이_75옥텟_이하(plan_client):
    """한글 제목은 UTF-8 에서 글자당 3바이트라 접지 않으면 캘린더가 줄을 버린다."""
    text = plan_client.post("/v1/plan.ics", json=BODY).content.decode("utf-8")
    for line in text.split("\r\n"):
        assert len(line.encode("utf-8")) <= 75


def test_스냅샷이_없으면_계획도_503(monkeypatch):
    monkeypatch.setattr("app.engine.snapshot.holder", SnapshotHolder())
    from app.main import app

    with TestClient(app) as c:
        assert c.post("/v1/plan", json=BODY).status_code == 503
        assert c.post("/v1/plan.ics", json=BODY).status_code == 503
