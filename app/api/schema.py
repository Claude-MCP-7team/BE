"""OpenAPI 요청 본문 — msgspec 스키마를 FastAPI 문서에 직접 붙인다.

엔드포인트들이 본문을 Pydantic 파라미터가 아니라 `await request.body()` +
msgspec 으로 읽는다. 빠르고(내부 모델이 msgspec 이라 변환이 한 번 준다) 오류
메시지도 필드 경로까지 나오지만, 대가가 하나 있었다 — **FastAPI 가 본문을 모른다.**

그래서 `/docs` 에 POST 엔드포인트가 '본문 없는 엔드포인트'로 그려졌다. 스키마에서
클라이언트를 생성하면 빈 요청을 보내게 된다. 에러가 아니라 문서가 틀린 것이라,
연동하는 쪽이 한참 헤맨 뒤에야 알게 된다.

디코드 방식을 바꾸지 않고 스키마만 바로잡는다. `openapi_extra` 로 라우트에 본문을
선언하고, msgspec 이 만든 컴포넌트를 OpenAPI 문서에 합친다. 런타임 동작은 그대로다
— 이 모듈은 문서 생성에만 관여한다.

`schema_components` 를 쓰는 이유: `msgspec.json.schema()` 는 `$defs` 를 쓰는데
OpenAPI 는 `#/components/schemas/` 를 본다. ref 위치를 맞춰 주지 않으면 `/docs` 가
참조를 못 찾아 본문이 빈 객체로 보인다 — 고치려던 증상이 그대로 남는다.
"""

from __future__ import annotations

from typing import Any

import msgspec
from fastapi import FastAPI

from app.schemas.user import UserProfile

_REF_TEMPLATE = "#/components/schemas/{name}"

(_PROFILE_REF,), _COMPONENTS = msgspec.json.schema_components(
    [UserProfile], ref_template=_REF_TEMPLATE
)


def profile_body(*, required: bool = True, description: str = "") -> dict[str, Any]:
    """`openapi_extra` 에 넣을 requestBody. 런타임에는 영향이 없다.

    `required` 를 라우트마다 정하는 이유: `POST /v1/sessions` 는 본문이 없어도
    된다(프로필 없는 익명 세션 발급). 전부 필수로 적으면 문서가 또 틀린다 —
    방향만 반대일 뿐 같은 종류의 거짓말이다.
    """
    return {
        "requestBody": {
            "required": required,
            "description": description or "판정 입력 프로필 (UserProfile)",
            "content": {"application/json": {"schema": _PROFILE_REF}},
        }
    }


def install_request_schemas(app: FastAPI) -> None:
    """생성된 OpenAPI 문서에 msgspec 컴포넌트를 합친다.

    FastAPI 는 만든 문서를 `app.openapi_schema` 에 캐시하므로, 합친 결과를 도로
    캐시에 넣어 다음 호출부터 그대로 나가게 한다.
    """
    build = app.openapi

    def openapi() -> dict[str, Any]:
        schema = build()
        components = schema.setdefault("components", {}).setdefault("schemas", {})
        # 이름이 겹치면 Pydantic 쪽(응답 모델)이 먼저다 — 그쪽은 FastAPI 가
        # 실제로 검증에 쓰는 스키마라 덮어쓰면 문서와 동작이 어긋난다.
        for name, definition in _COMPONENTS.items():
            components.setdefault(name, definition)
        app.openapi_schema = schema
        return schema

    app.openapi = openapi  # type: ignore[method-assign]
