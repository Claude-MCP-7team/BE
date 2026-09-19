"""에러 응답 — RFC 9457 problem+json (ARCHITECTURE.md §5).

여기서 지키려는 것은 형식보다 **구분**이다. FastAPI 기본형은 FE 에게 상태 코드밖에
주지 않는데, 같은 503 이 "아직 아무것도 안 된다"와 "저장만 안 된다, 판정은 정상"
두 가지 뜻으로 쓰인다. 뒤쪽에 "잠시 후 다시 시도하세요"를 띄우면 멀쩡히 쓸 수 있는
기능을 앞에 두고 사용자를 돌려보내게 된다.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.core.problem import MEDIA_TYPE
from app.engine.snapshot import SnapshotHolder, load_from_json
from app.engine.snapshot import holder as global_holder

BODY = {"core": {"birth_date": "2001-03-14", "region_code": "41465"}}

POLICIES = [
    {
        "policy_id": "P1",
        "status": "published",
        "meta": {"title": "정책", "category": "job", "authority_level": "local",
                 "region_code": ["00"], "dept": {"name": "과", "tel": "031-000-0000"}},
        "source": {"origin_url": "https://example.kr/p1"},
        "eligibility": [
            {"rule_id": "AGE", "field": "age", "op": ">=", "value": 19,
             "source_quote": "만 19세 이상"},
        ],
    }
]


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("YPC_FIXED_TODAY", "2026-09-19")
    load_from_json(global_holder, json.dumps(POLICIES).encode(), version="problem-v1")
    from app.main import app

    with TestClient(app) as c:
        yield c


def problem(response) -> dict:
    assert response.headers["content-type"].startswith(MEDIA_TYPE), response.headers
    body = response.json()
    # RFC 9457 필수 멤버
    assert body["status"] == response.status_code
    assert body["type"].startswith("/problems/")
    assert body["title"] and body["detail"]
    return body


# --- 구분이 되는가 ----------------------------------------------------------


def test_같은_503_이라도_원인이_다르면_type_이_다르다(client, monkeypatch):
    """'아직 아무것도 안 된다'와 '저장만 안 된다'는 화면이 달라야 한다."""
    monkeypatch.setattr("app.engine.snapshot.holder", SnapshotHolder())
    from app.main import app

    with TestClient(app) as fresh:
        스냅샷없음 = problem(fresh.post("/v1/judge", json=BODY))

    저장소없음 = problem(client.post("/v1/sessions", json={"profile": BODY}))

    assert 스냅샷없음["status"] == 저장소없음["status"] == 503
    assert 스냅샷없음["type"] == "/problems/snapshot-not-ready"
    assert 저장소없음["type"] == "/problems/session-store-unavailable"
    # 저장이 막혀도 판정은 된다는 사실이 문장에 남아 있어야 한다
    assert "판정" in 저장소없음["detail"]


def test_404_도_무엇이_없는지_구분된다(client):
    정책 = problem(client.get("/v1/policies/NOPE"))
    세션 = problem(client.get("/v1/sessions/8b1b0a4e-0000-4000-8000-000000000000"))

    assert 정책["type"] == "/problems/policy-not-found"
    assert 세션["type"] in ("/problems/session-not-found", "/problems/session-store-unavailable")
    # 무엇을 찾지 못했는지 식별자를 함께 준다 (RFC 9457 확장 멤버)
    assert 정책["policy_id"] == "NOPE"


# --- 기존 FE 를 깨지 않는가 --------------------------------------------------


def test_detail_은_그대로_남는다(client):
    """RFC 9457 의 detail 은 FastAPI 기본형과 같은 자리다.

    규격을 바꾸면서도 `body.detail` 을 읽던 코드가 계속 동작한다 — 이게 이 형식을
    고른 이유 중 하나다.
    """
    body = problem(client.get("/v1/policies/NOPE"))
    assert isinstance(body["detail"], str)
    assert "NOPE" in body["detail"]


def test_instance_에_요청_경로가_남는다(client):
    assert problem(client.get("/v1/policies/NOPE"))["instance"] == "/v1/policies/NOPE"


# --- 검증 오류 --------------------------------------------------------------


def test_쿼리_검증_오류는_항목별로_내려준다(client):
    """기본형은 detail 이 배열이라 사람이 읽을 문장이 없다. 문장과 항목을 나눠 준다."""
    body = problem(client.get("/v1/policies", params={"limit": 0}))

    assert body["type"] == "/problems/invalid-request"
    assert isinstance(body["detail"], str)  # 배열이 아니라 문장
    assert any("limit" in e["field"] for e in body["errors"])


def test_프로필_검증_오류는_다른_유형이다(client):
    """쿼리 오타와 조건 입력 오류는 화면에서 다른 곳을 가리켜야 한다."""
    body = problem(client.post("/v1/judge", json={"core": {"birth_date": "아무거나"}}))
    assert body["type"] == "/problems/invalid-profile"


# --- 처리되지 않은 예외 ------------------------------------------------------


def test_예상못한_예외는_내부를_노출하지_않는다(monkeypatch):
    """예외 문자열에 경로·쿼리·내부 상태가 섞여 나올 수 있다. 로그에만 남긴다."""
    load_from_json(global_holder, json.dumps(POLICIES).encode(), version="boom")

    def boom(*args, **kwargs):
        raise RuntimeError("비밀번호=hunter2 인 내부 상태")

    monkeypatch.setattr("app.api.v1.judge.judge_all", boom)
    from app.main import app

    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.post("/v1/judge", json=BODY)

    body = problem(r)
    assert r.status_code == 500
    assert body["type"] == "/problems/internal-error"
    assert "hunter2" not in json.dumps(body, ensure_ascii=False)
