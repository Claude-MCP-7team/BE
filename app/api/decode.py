"""요청 본문 디코드 — 한 군데에서만 한다.

엔드포인트마다 각자 try/except 를 쓰면 예외 하나를 빠뜨린 곳이 생기고, 그 엔드포인트만
조용히 500 을 낸다. 실제로 그랬다: 다섯 군데가 `msgspec.ValidationError` 만 잡았는데
**문법이 깨진 JSON 은 그 부모인 `msgspec.DecodeError`** 라서, POST 다섯 개가 전부
깨진 본문에 500 을 돌려줬다.

`ValidationError` 는 `DecodeError` 의 하위클래스다. 자식만 잡으면 부모는 빠져나간다.
이 프로젝트에서 같은 실수가 세 번 났다 (FastAPI↔Starlette 의 HTTPException,
LLMError↔SDK 예외, 그리고 이것). 그래서 여기 한 함수로 모으고, 새 엔드포인트가
이걸 쓰지 않으면 테스트가 잡도록 했다 (`tests/unit/test_endpoint_sweep.py`).

두 실패를 구분해서 내보낸다 — FE 가 어디를 고쳐야 하는지가 다르다:
  invalid-request  본문이 JSON 이 아니다      → 직렬화·전송을 본다
  invalid-profile  JSON 은 맞는데 값이 틀렸다  → 어느 필드인지 detail 에 있다
"""

from __future__ import annotations

import msgspec

from app.core.problem import INVALID_PROFILE, INVALID_REQUEST, Problem
from app.schemas.user import UserProfile


def decode_profile(body: bytes) -> UserProfile:
    """요청 본문 → UserProfile. 실패는 전부 4xx 다. 500 이 날 자리가 없다."""
    try:
        return msgspec.json.decode(body, type=UserProfile)
    # 순서가 중요하다: ValidationError 가 DecodeError 의 하위클래스라
    # DecodeError 를 먼저 쓰면 값 오류까지 '본문이 JSON 이 아니다'로 뭉개진다.
    except msgspec.ValidationError as e:
        raise Problem(INVALID_PROFILE, str(e)) from e
    except msgspec.DecodeError as e:
        raise Problem(
            INVALID_REQUEST, f"요청 본문이 올바른 JSON 이 아닙니다: {e}"
        ) from e
