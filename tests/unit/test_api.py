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
    assert s["ineligible"] == 1  # 용인 (거주 3개월 < 6)
    assert s["needs_info"] == 1  # 소득 미입력


def test_기본_응답은_부적격_상세를_싣지_않는다(client):
    """부적격이 다수인데 매번 근거까지 실어보내면 응답이 몇 배로 커진다."""
    ids = {r["policy_id"] for r in post(client).json()["results"]}
    assert "YONGIN-RENT" not in ids
    assert ids == {"MOLIT-RENT", "ASK-ME"}


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
