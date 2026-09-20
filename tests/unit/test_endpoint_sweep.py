"""전 엔드포인트 × 망가진 입력 — 500 이 나오면 실패.

마일스톤 M5 의 "API 누락·500 Error 점검"을 사람이 한 번 하고 끝내지 않기 위한 테스트다.
손으로 훑으면 그 시점의 엔드포인트만 보게 되고, 다음에 추가되는 POST 는 아무도 안 본다.
그래서 **라우트 목록을 앱에서 직접 읽는다** — 새 엔드포인트는 자동으로 대상이 된다.

실제로 이게 잡은 것: POST 다섯 개가 전부 깨진 JSON 에 500 을 냈다.
`msgspec.ValidationError` 만 잡았는데 문법 오류는 그 부모인 `DecodeError` 라서다.

5xx 가 나쁜 이유는 FE 가 자기 버그를 BE 장애로 오인하기 때문이다. 잘못된 요청은
"네가 보낸 게 틀렸다"(4xx)여야 고칠 사람이 고친다.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.engine.snapshot import holder as global_holder
from app.engine.snapshot import load_from_json

POLICY = {
    "policy_id": "SWEEP-1", "status": "published",
    "meta": {"title": "훑기용 정책", "category": "job", "authority_level": "local",
             "region_code": ["41190"],
             "dept": {"name": "청년정책과", "tel": "031-000-0000"}},
    "source": {"origin_url": "https://example.invalid/SWEEP-1"},
    "eligibility": [{"rule_id": "A", "field": "age", "op": ">=", "value": 19,
                     "source_quote": "만 19세 이상"}],
}

# 악의적인 입력이 아니라 '흔한 실수'다. FE 가 실제로 보내게 되는 것들.
BAD_BODIES: dict[str, bytes] = {
    "빈 본문": b"",
    "깨진 JSON": b"{oops",
    "닫히지 않은 객체": b'{"core": {',
    "JSON 이 아닌 텍스트": b"hello",
    "빈 객체": b"{}",
    "배열": b"[]",
    "널": b"null",
    "문자열": b'"profile"',
    "숫자": b"42",
    "타입 틀림": b'{"core": {"birth_date": 123, "region_code": []}}',
    "모르는 필드": b'{"core": {"birth_date": "1998-03-14", "region_code": "41190"},'
                   b' "nope": 1}',
    "깊게 중첩": b'{"core": ' + b"[" * 200 + b"]" * 200 + b"}",
}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("YPC_FIXED_TODAY", "2026-09-20")
    load_from_json(global_holder, json.dumps([POLICY]).encode(), version="sweep-v1")
    from app.main import app

    with TestClient(app) as c:
        yield c


def _post_paths() -> list[str]:
    """앱에 등록된 POST 경로. 경로 파라미터가 있는 것은 뺀다 (별도 테스트 대상).

    `app.routes` 가 아니라 OpenAPI 스키마에서 읽는다. 이 FastAPI 버전은
    include_router 한 라우터를 `_IncludedRouter` 로 감싸 두고 펼치지 않아서,
    `app.routes` 만 보면 /v1/* 이 하나도 안 보인다 — 훑기가 조용히 0건이 된다.
    스키마는 FE 가 보는 공개 API 면 그 자체라 기준으로도 이쪽이 맞다.
    """
    from app.main import app

    paths = app.openapi().get("paths", {})
    return sorted(
        path
        for path, ops in paths.items()
        if "post" in ops and "{" not in path and path.startswith("/v1")
    )


def test_훑을_대상이_실제로_있다() -> None:
    """라우트를 못 읽으면 이 파일 전체가 조용히 0건을 검사하게 된다."""
    paths = _post_paths()
    assert len(paths) >= 5, f"POST 엔드포인트를 못 찾았다: {paths}"
    assert "/v1/judge" in paths


def _problem_type(r) -> str | None:
    if not r.headers.get("content-type", "").startswith("application/problem+json"):
        return None
    return r.json().get("type")


def _store_unavailable(r) -> bool:
    """세션 저장소가 없어서 나온 503. 선언된 상태지 크래시가 아니다.

    이 테스트 환경에는 DB 가 없고, `/v1/sessions` 는 본문을 읽기 전에 저장소부터
    본다. DB 가 있는 CI 에서는 이 분기를 타지 않는다.
    """
    return r.status_code == 503 and _problem_type(r) == "/problems/session-store-unavailable"


# 본문 없이 불러도 되는 엔드포인트. `POST /v1/sessions` 는 프로필 없는 익명 세션
# 발급이 정상 동작이라 빈 본문이 201 이다. OpenAPI 스키마에서 읽지 못하는 이유는
# 이 엔드포인트들이 Pydantic 본문 파라미터 대신 `await request.body()` 를 직접
# 쓰기 때문이다 — 그래서 스키마에 requestBody 가 아예 없다.
EMPTY_BODY_OK = {"/v1/sessions"}


@pytest.mark.parametrize("path", _post_paths())
@pytest.mark.parametrize("label", sorted(BAD_BODIES))
def test_망가진_본문이_미처리_예외로_끝나지_않는다(client, path: str, label: str) -> None:
    """기준은 '5xx 금지'가 아니라 '미처리 예외 금지'다.

    503 session-store-unavailable 처럼 **선언된** 5xx 는 정상이다 — 무슨 일이
    일어났는지 FE 에 말해 주고 있으니 처리된 것이다. 반면 internal-error 는
    catch-all 이 마지막에 붙이는 딱지라, 그게 보이면 아무도 그 입력을 예상 못 했다는
    뜻이다. 그 구분이 이 테스트의 전부다.
    """
    r = client.post(
        path, content=BAD_BODIES[label], headers={"Content-Type": "application/json"}
    )
    assert _problem_type(r) != "/problems/internal-error", (
        f"{path} 가 {label} 에 미처리 예외를 냈다 — "
        f"FE 가 자기 버그를 BE 장애로 읽는다. 본문: {r.text[:200]}"
    )
    if _store_unavailable(r):
        return
    if label == "빈 본문" and path in EMPTY_BODY_OK:
        assert r.status_code == 201, f"{path} → {r.status_code}: {r.text[:200]}"
        return
    assert 400 <= r.status_code < 500, (
        f"{path} 가 {label} 에 {r.status_code} 를 냈다: {r.text[:200]}"
    )


@pytest.mark.parametrize("path", _post_paths())
def test_망가진_본문도_problem_json_으로_나간다(client, path: str) -> None:
    """에러 모양이 두 가지면 FE 가 두 가지를 처리해야 한다."""
    r = client.post(path, content=b"{oops", headers={"Content-Type": "application/json"})
    if _store_unavailable(r):
        pytest.skip("세션 저장소가 없어 본문까지 가지 않는다 (DB 있는 CI 가 검사한다)")
    assert r.headers["content-type"].startswith("application/problem+json")
    body = r.json()
    assert body["type"] == "/problems/invalid-request", body
    # 무엇이 잘못됐는지 말해 줘야 FE 가 직렬화를 고칠 수 있다
    assert "JSON" in body["detail"]


@pytest.mark.parametrize("path", _post_paths())
def test_값_오류와_문법_오류를_구분한다(client, path: str) -> None:
    """둘 다 422 지만 고칠 곳이 다르다 — 본문 직렬화냐, 필드 값이냐.

    하나로 뭉치면 FE 가 직렬화를 의심해야 할 때 필드 값을 뒤지게 된다.
    """
    syntax = client.post(path, content=b"{oops",
                         headers={"Content-Type": "application/json"})
    value = client.post(path, json={"core": {"birth_date": "1998-03-14",
                                             "region_code": "41190",
                                             "household_size": 0}})
    if _store_unavailable(syntax):
        pytest.skip("세션 저장소가 없어 본문까지 가지 않는다 (DB 있는 CI 가 검사한다)")
    assert syntax.json()["type"] == "/problems/invalid-request"
    assert value.json()["type"] == "/problems/invalid-profile"
    assert "household_size" in value.json()["detail"]


@pytest.mark.parametrize("path", _post_paths())
def test_정상_본문은_그대로_돈다(client, path: str) -> None:
    """4xx 를 늘리다 정상 경로를 막으면 본말전도다."""
    r = client.post(path, json={"core": {"birth_date": "1998-03-14",
                                         "region_code": "41190"}})
    if _store_unavailable(r):
        pytest.skip("세션 저장소 없음 (DB 있는 CI 가 검사한다)")
    assert r.status_code in (200, 201), f"{path} → {r.status_code}: {r.text[:200]}"


def test_빈_본문_허용_목록이_최신인지() -> None:
    """목록의 경로가 사라지면 그 항목은 아무것도 안 지키는 죽은 예외가 된다.

    더 나쁜 경우: 경로 이름이 바뀌면 새 경로가 훑기의 엄격한 쪽으로 넘어가는 게
    아니라, 예외 목록만 남아 아무 데도 안 걸린다.
    """
    unknown = EMPTY_BODY_OK.difference(_post_paths())
    assert not unknown, f"허용 목록에 없는 경로가 있다: {sorted(unknown)}"


def test_모든_POST_가_공용_디코더를_쓴다() -> None:
    """각자 try/except 를 쓰면 다음 사람이 또 예외 하나를 빠뜨린다.

    이 파일의 훑기가 잡아 주긴 하지만, 잡히는 것보다 안 생기는 게 낫다.
    """
    from pathlib import Path

    for name in ("app/api/v1/judge.py", "app/api/v1/sessions.py"):
        src = Path(name).read_text(encoding="utf-8")
        assert "msgspec.json.decode(body" not in src, (
            f"{name} 이 직접 디코드한다 — app.api.decode.decode_profile 를 쓸 것"
        )


def test_본문을_받는_엔드포인트는_스키마에도_본문이_있다() -> None:
    """`/docs` 와 생성된 클라이언트가 빈 요청을 보내지 않게 한다.

    본문을 Pydantic 파라미터가 아니라 msgspec 으로 직접 읽기 때문에 FastAPI 가
    스스로는 본문을 모른다. `openapi_extra` 로 채워 넣는데, 새 엔드포인트에서
    그걸 빠뜨리면 문서만 조용히 틀린다 — 에러가 안 나는 고장이라 연동하는 쪽이
    한참 헤맨 뒤에야 안다.
    """
    from app.main import app

    schema = app.openapi()
    missing = [
        f"{verb.upper()} {path}"
        for path, ops in schema["paths"].items()
        for verb in ("post", "put")
        if verb in ops and not ops[verb].get("requestBody")
    ]
    assert not missing, (
        f"요청 본문이 문서에 없다: {missing} — app.api.schema.profile_body() 를 "
        f"openapi_extra 로 붙일 것"
    )


def test_요청_본문_스키마가_실제_모델을_가리킨다() -> None:
    """ref 만 있고 컴포넌트가 없으면 /docs 는 본문을 빈 객체로 그린다.

    증상이 '본문이 없다'에서 '본문이 비어 있다'로 바뀔 뿐 고쳐진 게 아니다.
    """
    from app.main import app

    schema = app.openapi()
    components = schema["components"]["schemas"]
    assert "UserProfile" in components

    ref = schema["paths"]["/v1/judge"]["post"]["requestBody"]["content"]
    name = ref["application/json"]["schema"]["$ref"].rsplit("/", 1)[-1]
    body = components[name]
    assert "core" in body.get("properties", {}), body
    assert body.get("required") == ["core"], body

    # core 안까지 실제로 풀려야 FE 가 어떤 필드를 보낼지 알 수 있다
    core_name = body["properties"]["core"]["$ref"].rsplit("/", 1)[-1]
    core = components[core_name]["properties"]
    assert {"birth_date", "region_code", "household_income_ratio_median"} <= set(core)


def test_본문이_선택인_곳은_선택으로_적힌다() -> None:
    """전부 required=true 로 적으면 문서가 또 틀린다 — 방향만 반대다.

    `POST /v1/sessions` 는 본문 없이 부르면 프로필 없는 익명 세션이 발급된다.
    """
    from app.main import app

    paths = app.openapi()["paths"]
    assert paths["/v1/sessions"]["post"]["requestBody"]["required"] is False
    assert paths["/v1/judge"]["post"]["requestBody"]["required"] is True


def test_문서에_적힌_대로_보내면_실제로_통과한다(client) -> None:
    """문서와 서버가 갈라지는 것을 막는 진짜 검사.

    스키마가 붙어 있다는 것만으로는 부족하다 — 엉뚱한 모델을 가리켜도 붙어는 있다.
    커밋된 데모 프로필은 `Core` 의 모든 필드를 채우고 있으므로, 이게 200 이면
    문서가 선언한 필드를 서버가 전부 받아들인다는 뜻이다.
    """
    import json as _json
    from pathlib import Path

    profile = _json.loads(
        Path("data/demo/profile.demo.json").read_text(encoding="utf-8")
    )
    r = client.post("/v1/judge", json=profile)
    assert r.status_code == 200, r.text


def test_문서의_필드_목록이_실제_모델과_같다() -> None:
    """한쪽만 바뀌면 FE 는 보내도 되는 필드를 못 보내거나, 없는 필드를 보낸다.

    `forbid_unknown_fields` 때문에 후자는 프로필 전체가 422 로 튕긴다 — 문서를
    믿고 만든 클라이언트가 아무것도 못 하게 된다.
    """
    import msgspec

    from app.main import app
    from app.schemas.user import Core

    documented = set(
        app.openapi()["components"]["schemas"]["Core"]["properties"]
    )
    actual = {f.encode_name for f in msgspec.inspect.type_info(Core).fields}
    assert documented == actual, (
        f"문서에만: {sorted(documented - actual)} / 모델에만: {sorted(actual - documented)}"
    )


def test_문서가_필수라고_한_것은_실제로_필수다(client) -> None:
    """required 가 거짓이면 FE 는 선택 필드로 알고 안 보낸다."""
    from app.main import app

    components = app.openapi()["components"]["schemas"]
    for field in components["Core"]["required"]:
        body = {"core": {"birth_date": "1998-03-14", "region_code": "41190"}}
        del body["core"][field]
        r = client.post("/v1/judge", json=body)
        assert r.status_code == 422, f"{field} 를 빼도 통과한다 ({r.status_code})"
        assert field in r.json()["detail"], r.json()
