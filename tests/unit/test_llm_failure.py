"""LLM 이 죽어도 판정은 나간다.

이 파일이 지키는 것: 설명문 생성 실패가 판정 응답을 죽이지 않는 것.

한동안 `explain_all` 은 `LLMError` 만 잡았다. 그건 우리가 직접 던지는 것(거부·토큰
상한·JSON 파싱 실패)뿐이고, 정작 흔한 실패인 SDK 의 타임아웃·연결 끊김은 그대로
빠져나가 이미 계산이 끝난 판정 응답 전체를 500 으로 만들었다.
"""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

import app.llm.explain as explain_mod
from app.llm.client import REQUEST_MAX_RETRIES, REQUEST_TIMEOUT_S, LLMError
from app.llm.explain import explain_all
from app.schemas.judgement import JudgementResult


def _result() -> JudgementResult:
    return JudgementResult(
        policy_id="P1", verdict="ELIGIBLE", confidence="CONFIRMED",
        origin_url="https://example.invalid/P1",
    )


class _Raises:
    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    def complete_json(self, **_: object) -> dict[str, object]:
        raise self._exc


@pytest.mark.parametrize(
    "exc",
    [
        TimeoutError("Request timed out."),      # APITimeoutError 가 상속하는 종류
        ConnectionError("Connection reset."),     # APIConnectionError
        RuntimeError("rate limited"),             # RateLimitError 등 그 밖의 전부
        LLMError("모델이 거부했습니다"),            # 원래도 잡히던 것 — 회귀 방지
        ValueError("unexpected schema"),
    ],
)
def test_어떤_예외든_템플릿으로_떨어진다(exc: Exception) -> None:
    out = explain_all([_result()], {"P1": "테스트 정책"}, _Raises(exc))
    assert out["P1"], "설명문이 비어 있으면 화면에 쓸 게 없다"
    assert "테스트 정책" in out["P1"]


def test_실패를_조용히_삼키지_않는다(caplog: pytest.LogCaptureFixture) -> None:
    """로그가 없으면 LLM 이 한 번도 성공하지 않는 배포를 아무도 모른다.

    템플릿이 그럴듯해서 화면은 멀쩡해 보인다.
    """
    with caplog.at_level(logging.WARNING, logger=explain_mod.__name__):
        explain_all([_result()], {"P1": "테스트 정책"}, _Raises(TimeoutError("timeout")))
    assert any(r.levelno >= logging.WARNING for r in caplog.records)
    assert any(r.exc_info for r in caplog.records), "스택트레이스가 있어야 원인을 안다"


def test_중단_신호는_삼키지_않는다() -> None:
    """KeyboardInterrupt·SystemExit 까지 먹으면 서버를 못 끈다.

    `except Exception` 이라 BaseException 은 통과한다. `except BaseException` 으로
    넓히면 이 테스트가 잡는다.
    """
    with pytest.raises(KeyboardInterrupt):
        explain_all([_result()], {"P1": "테스트"}, _Raises(KeyboardInterrupt()))


def test_요청_경로는_오래_기다리지_않는다() -> None:
    """SDK 기본값은 타임아웃 10분 · 재시도 2회라 최악 30분을 붙잡는다.

    워커 하나짜리 배포에서 그 하나가 LLM 을 기다리면 남의 판정까지 멈춘다.
    재시도가 0이어야 최악 wall-clock 이 타임아웃 그 자체가 된다.
    """
    assert REQUEST_TIMEOUT_S <= 30, "판정 응답 예산(p95 5초) 대비 너무 길다"
    assert REQUEST_MAX_RETRIES == 0


def test_요청경로_클라이언트에_실제로_설정이_걸린다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """상수만 두고 SDK 에 안 넘기면 아무 일도 안 일어난다.

    get_llm() 이 만드는 객체를 그대로 확인한다 — 상수와 실제 클라이언트가 따로 놀면
    타임아웃을 줄였다고 믿으면서 10분을 기다리게 된다.
    """
    import app.llm.client as client_mod

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-not-a-real-key")
    monkeypatch.setattr(client_mod, "_shared", None)

    llm = client_mod.get_llm()
    assert llm is not None
    assert llm._client.timeout == REQUEST_TIMEOUT_S
    assert llm._client.max_retries == REQUEST_MAX_RETRIES


def test_배치_경로는_기본값을_쓴다(monkeypatch: pytest.MonkeyPatch) -> None:
    """A2 구조화는 수백 건을 돌리므로 레이트리밋 재시도가 이득이다.

    요청 경로의 짧은 설정이 배치에까지 새면 긴 구조화 호출이 8초에 끊긴다.
    """
    import anthropic

    from app.llm.client import AnthropicLLM

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-not-a-real-key")
    batch = AnthropicLLM()
    assert batch._client.max_retries == anthropic.Anthropic(
        api_key="sk-ant-test-not-a-real-key"
    ).max_retries
    assert batch._client.timeout != REQUEST_TIMEOUT_S


def test_LLM_이_죽어도_judge_는_200_이다(monkeypatch: pytest.MonkeyPatch) -> None:
    """이게 진짜 회귀 테스트다. 판정은 LLM 호출 전에 이미 끝나 있다."""
    import json

    import app.api.v1.judge as judge_mod
    from app.engine.snapshot import holder as global_holder
    from app.engine.snapshot import load_from_json
    from app.main import app as fastapi_app

    policy = {
        "policy_id": "P1", "status": "published",
        "meta": {"title": "테스트 정책", "category": "job", "authority_level": "local",
                 "region_code": ["41465"],
                 "dept": {"name": "청년정책과", "tel": "031-000-0000"}},
        "source": {"origin_url": "https://example.invalid/P1"},
        "eligibility": [{"rule_id": "A", "field": "age", "op": ">=", "value": 19,
                         "source_quote": "만 19세 이상"}],
    }
    load_from_json(global_holder, json.dumps([policy]).encode(), version="llm-fail-v1")
    monkeypatch.setattr(judge_mod, "get_llm", lambda: _Raises(TimeoutError("timeout")))

    with TestClient(fastapi_app) as client:
        r = client.post(
            "/v1/judge?explain=llm",
            json={"core": {"birth_date": "1998-03-14", "region_code": "41465"}},
        )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["summary"]["eligible"] == 1
    assert body["results"][0]["explanation"], "설명문 자리가 비면 FE 가 빈 카드를 그린다"
