"""CORS — 브라우저가 이 API 를 부를 수 있는지.

여기서 제일 중요한 것은 허용 출처가 아니라 **노출 헤더**다. 판정·목록 응답은
ETag + If-None-Match 로 304 를 내도록 만들어져 있는데, 브라우저는 노출 목록에 없는
응답 헤더를 자바스크립트에 넘기지 않는다. 빠뜨리면 FE 는 ETag 를 읽지 못하고 서버는
매번 전체 응답을 다시 만든다 — 에러가 아니라서 발견이 늦는다.

설정은 모듈 적재 시점에 미들웨어로 굳으므로, 환경변수를 바꾼 뒤 다시 적재해서 본다.
"""

from __future__ import annotations

import dataclasses
import importlib
import json

import pytest
from fastapi.testclient import TestClient

import app.core.config
import app.main
from app.engine.snapshot import holder as global_holder
from app.engine.snapshot import load_from_json

ORIGIN = "https://ypc-fe.example"

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


def _reload(monkeypatch, origins: tuple[str, ...]):
    """설정만 갈아끼우고 app 모듈을 다시 적재한다.

    config 모듈을 reload 하지 않는 이유: Settings 클래스 객체가 새로 만들어져
    이미 그 클래스를 import 해 둔 다른 모듈·테스트와 타입이 어긋난다.
    """
    patched = dataclasses.replace(app.core.config.settings, cors_origins=origins)
    monkeypatch.setattr(app.core.config, "settings", patched)
    importlib.reload(app.main)
    return app.main.app


@pytest.fixture(autouse=True)
def _restore():
    """다른 테스트가 쓰는 전역 app 을 원래대로 돌려놓는다."""
    yield
    importlib.reload(app.main)


@pytest.fixture
def snapshot():
    load_from_json(global_holder, json.dumps(POLICIES).encode(), version="cors-v1")


def test_허용된_출처에는_ETag_를_읽게_해준다(monkeypatch, snapshot):
    fresh = _reload(monkeypatch, (ORIGIN,))

    with TestClient(fresh) as c:
        r = c.get("/v1/policies", headers={"Origin": ORIGIN})

    assert r.headers["access-control-allow-origin"] == ORIGIN
    exposed = {h.strip().lower() for h in r.headers["access-control-expose-headers"].split(",")}
    assert "etag" in exposed, "ETag 를 노출하지 않으면 FE 가 304 캐시를 쓸 수 없다"
    assert "x-snapshot-version" in exposed


def test_preflight_가_세션_헤더를_허용한다(monkeypatch, snapshot):
    """세션은 쿠키가 아니라 X-Session-Id 헤더로 식별된다."""
    fresh = _reload(monkeypatch, (ORIGIN,))

    with TestClient(fresh) as c:
        r = c.options(
            "/v1/judge",
            headers={
                "Origin": ORIGIN,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type,x-session-id,if-none-match",
            },
        )

    assert r.status_code == 200
    allowed = {h.strip().lower() for h in r.headers["access-control-allow-headers"].split(",")}
    assert {"content-type", "x-session-id", "if-none-match"} <= allowed
    # 쿠키를 쓰지 않으므로 자격증명을 허용하지 않는다
    assert "access-control-allow-credentials" not in r.headers


def test_허용하지_않은_출처에는_열어주지_않는다(monkeypatch, snapshot):
    fresh = _reload(monkeypatch, (ORIGIN,))

    with TestClient(fresh) as c:
        r = c.get("/v1/policies", headers={"Origin": "https://evil.example"})

    assert "access-control-allow-origin" not in r.headers


def test_설정이_없으면_CORS_를_아예_열지_않는다(monkeypatch, snapshot):
    """전부 열어두는 것은 증상이 없어서 그대로 남는다. 닫힌 쪽이 기본값이다."""
    fresh = _reload(monkeypatch, ())

    with TestClient(fresh) as c:
        r = c.get("/v1/policies", headers={"Origin": ORIGIN})

    assert r.status_code == 200  # 서버끼리의 호출은 계속 된다
    assert "access-control-allow-origin" not in r.headers
