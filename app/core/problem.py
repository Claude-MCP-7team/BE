"""에러 응답 — RFC 9457 `application/problem+json` (ARCHITECTURE.md §5).

FastAPI 기본 에러는 `{"detail": "..."}` 뿐이라, FE 가 구분할 수 있는 것이 **상태 코드밖에
없다.** 그런데 같은 상태 코드가 전혀 다른 뜻인 경우가 있다:

  503 스냅샷 미적재        → 아직 아무것도 안 된다. 잠시 후 다시.
  503 세션 저장소 사용 불가 → 저장만 안 된다. **판정은 정상이다.**

앞쪽에 "잠시 후 다시 시도하세요"를 띄우는 건 맞지만 뒤쪽에 같은 걸 띄우면, 멀쩡히
쓸 수 있는 기능을 앞에 두고 사용자를 돌려보내는 것이다. 그래서 상태 코드 옆에
**기계가 읽는 식별자**를 준다.

RFC 9457 을 고른 이유는 표준이라는 것 말고도 `detail` 멤버가 있기 때문이다 —
FastAPI 기본형에서 `detail` 을 읽던 코드는 그대로 동작한다. 규격을 바꾸면서
FE 를 깨뜨리지 않는 드문 경우다.

`type` 은 상대 URI(`/problems/<code>`)다. RFC 9457 이 허용하고, 도메인이 정해지지
않은 상태에서 절대 URI 를 박으면 배포 주소가 바뀔 때 계약이 따라 깨진다.
"""

from __future__ import annotations

import http
import logging
from typing import Any, NamedTuple

import msgspec
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import Response

# 라우터가 올리는 404·405 는 starlette 쪽 예외다. FastAPI 의 HTTPException 은
# 그것의 하위 클래스라, 하위 클래스에만 핸들러를 걸면 경로 없음·메서드 불일치가
# 핸들러를 타지 않고 FastAPI 기본형으로 나간다 — 에러 모양이 두 가지가 된다.
from starlette.exceptions import HTTPException

log = logging.getLogger("ypc.problem")

MEDIA_TYPE = "application/problem+json"


class ProblemType(NamedTuple):
    """문제 유형 하나. `title` 은 유형의 설명이라 인스턴스마다 바뀌지 않는다."""

    code: str
    status: int
    title: str

    @property
    def uri(self) -> str:
        return f"/problems/{self.code}"


# --- 유형 표 — FE 가 분기 기준으로 삼는 값이다. 코드 문자열은 계약이다 ---------
SNAPSHOT_NOT_READY = ProblemType(
    "snapshot-not-ready", 503, "정책 스냅샷이 아직 적재되지 않았습니다"
)
POLICY_NOT_FOUND = ProblemType("policy-not-found", 404, "그런 정책이 없습니다")
ROUTE_NOT_FOUND = ProblemType("route-not-found", 404, "그런 경로가 없습니다")
METHOD_NOT_ALLOWED = ProblemType(
    "method-not-allowed", 405, "이 경로에서 허용되지 않는 메서드입니다"
)
SESSION_NOT_FOUND = ProblemType("session-not-found", 404, "세션이 없거나 만료되었습니다")
SESSION_STORE_UNAVAILABLE = ProblemType(
    "session-store-unavailable", 503, "세션 저장소를 사용할 수 없습니다"
)
SESSION_NOT_READABLE = ProblemType(
    "session-not-readable", 500, "저장된 프로필을 읽을 수 없습니다"
)
INVALID_PROFILE = ProblemType("invalid-profile", 422, "조건 입력이 올바르지 않습니다")
INVALID_REQUEST = ProblemType("invalid-request", 422, "요청이 올바르지 않습니다")
INTERNAL_ERROR = ProblemType("internal-error", 500, "서버에서 처리하지 못했습니다")

ALL_TYPES: tuple[ProblemType, ...] = (
    SNAPSHOT_NOT_READY,
    POLICY_NOT_FOUND,
    ROUTE_NOT_FOUND,
    METHOD_NOT_ALLOWED,
    SESSION_NOT_FOUND,
    SESSION_STORE_UNAVAILABLE,
    SESSION_NOT_READABLE,
    INVALID_PROFILE,
    INVALID_REQUEST,
    INTERNAL_ERROR,
)

# 유형을 지정하지 않고 올라온 HTTPException 을 상태 코드로 되돌린다.
# 새 엔드포인트가 유형을 깜빡해도 응답 모양은 유지된다 — 구분만 거칠어진다.
#
# 404 는 POLICY_NOT_FOUND 가 아니라 ROUTE_NOT_FOUND 다. 유형 없이 올라온 404 는
# 대부분 라우터가 낸 '그런 경로 없음'이고, 거기에 '그런 정책이 없습니다'를 붙이면
# FE 는 있지도 않은 정책을 찾는 화면을 띄운다.
_BY_STATUS = {
    t.status: t
    for t in (ROUTE_NOT_FOUND, METHOD_NOT_ALLOWED, INVALID_REQUEST, INTERNAL_ERROR)
}


class Problem(HTTPException):
    """유형이 붙은 에러. HTTPException 을 상속해 FastAPI 경로에서 그대로 쓴다."""

    def __init__(
        self,
        kind: ProblemType,
        detail: str | None = None,
        **extensions: Any,
    ) -> None:
        super().__init__(status_code=kind.status, detail=detail or kind.title)
        self.kind = kind
        self.extensions = extensions


def render(
    kind: ProblemType, detail: str, instance: str | None = None, **extensions: Any
) -> Response:
    body: dict[str, Any] = {
        "type": kind.uri,
        "title": kind.title,
        "status": kind.status,
        "detail": detail,
    }
    if instance:
        body["instance"] = instance
    body.update(extensions)
    return Response(
        content=msgspec.json.encode(body),
        status_code=kind.status,
        media_type=MEDIA_TYPE,
    )


def _detail_of(exc: HTTPException, kind: ProblemType) -> str:
    """사람이 읽을 문장. 프레임워크 기본 문구면 우리 문장으로 바꾼다.

    detail 을 주지 않고 올라온 예외는 Starlette 이 상태 코드의 영문 관용구
    ("Not Found")를 채운다. 그대로 내보내면 다른 에러는 한국어인데 경로 오타만
    영어가 되어, 화면이 그걸 그대로 보여주면 사용자에게는 고장처럼 보인다.
    """
    detail = str(exc.detail or "")
    try:
        default_phrase = http.HTTPStatus(exc.status_code).phrase
    except ValueError:
        default_phrase = ""
    return kind.title if detail == default_phrase else detail


def install(app: FastAPI) -> None:
    """에러 응답을 전부 problem+json 으로 바꾼다."""

    @app.exception_handler(Problem)
    async def _problem(request: Request, exc: Problem) -> Response:
        return render(exc.kind, str(exc.detail), request.url.path, **exc.extensions)

    @app.exception_handler(HTTPException)
    async def _http(request: Request, exc: HTTPException) -> Response:
        kind = _BY_STATUS.get(exc.status_code)
        if kind is None:
            kind = ProblemType(f"http-{exc.status_code}", exc.status_code, str(exc.detail))
        return render(kind, _detail_of(exc, kind), request.url.path)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> Response:
        # FastAPI 기본형은 detail 이 배열이라 사람이 읽을 문장이 없다. 문장은
        # detail 에 두고 항목별 내용은 errors 확장으로 내린다 — RFC 9457 이
        # 허용하는 추가 멤버다.
        errors = [
            {"field": ".".join(str(p) for p in e.get("loc", ())), "message": e.get("msg", "")}
            for e in exc.errors()
        ]
        summary = "; ".join(f"{e['field']}: {e['message']}" for e in errors)
        return render(
            INVALID_REQUEST, summary or INVALID_REQUEST.title, request.url.path, errors=errors
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> Response:
        # 예외 문자열을 그대로 내보내지 않는다. 경로·쿼리·내부 상태가 섞여 나올 수
        # 있고, 그걸 읽는 사람이 사용자라는 보장이 없다. 로그에는 전부 남긴다.
        log.exception("처리되지 않은 예외: %s %s", request.method, request.url.path)
        return render(INTERNAL_ERROR, INTERNAL_ERROR.title, request.url.path)
