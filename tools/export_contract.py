"""PolicySchema 를 JSON Schema 로 내보낸다 — AI 역할과의 계약서(C1).

AI 역할은 A2 에이전트 출력이 이 스키마를 만족하는지 자체 검증할 수 있고,
BE 는 같은 정의로 디코딩한다. 한쪽만 바뀌면 CI 가 diff 로 잡는다.

사용법:  python tools/export_contract.py
출력:    docs/contracts/policy_schema.json
"""

from __future__ import annotations

import inspect
import json
import pathlib
import sys

import msgspec

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.core.console import force_utf8_console  # noqa: E402
from app.core.problem import ALL_TYPES  # noqa: E402
from app.schemas.enums import KNOWN_FIELDS, TIME_SATISFIABLE_FIELDS  # noqa: E402
from app.schemas.judgement import JudgementResult  # noqa: E402
from app.schemas.policy import PolicySchema  # noqa: E402
from app.schemas.user import UserProfile  # noqa: E402

OUT = pathlib.Path(__file__).resolve().parent.parent / "docs" / "contracts"


def normalize_descriptions(node: object) -> object:
    """description 을 인터프리터 버전과 무관한 형태로 맞춘다.

    msgspec 은 구조체의 docstring 을 그대로 description 에 싣는데, 그 문자열은
    파이썬 버전마다 다르다. 3.13 부터 컴파일러가 docstring 의 공통 들여쓰기를
    제거하고, 3.11 은 두 번째 줄부터의 들여쓰기를 그대로 남긴다.

    그래서 같은 커밋을 3.11 에서 내보내면 들여쓰기가 있는 JSON 이, 3.14 에서
    내보내면 없는 JSON 이 나온다. 어느 쪽을 커밋해도 반대쪽 기계에서 CI 의
    드리프트 검사가 실패하는데, 그 실패는 계약이 바뀌었다는 뜻이 아니라
    인터프리터가 다르다는 뜻이라서 읽는 사람을 잘못된 곳으로 보낸다.
    (실제로 한 번 그렇게 됐다 — 2069740 이 3.14 출력을 커밋해 CI 가 깨졌다.)

    inspect.cleandoc 은 이미 dedent 된 문자열에 다시 적용해도 결과가 같으므로
    (멱등), 두 버전 모두에서 같은 출력을 만든다. 계약면의 의미는 바뀌지 않는다:
    JSON Schema 의 description 은 사람이 읽는 설명이고 들여쓰기는 파이썬
    소스의 사정일 뿐이다.
    """
    if isinstance(node, dict):
        return {
            key: inspect.cleandoc(value)
            if key == "description" and isinstance(value, str)
            else normalize_descriptions(value)
            for key, value in node.items()
        }
    if isinstance(node, list):
        return [normalize_descriptions(item) for item in node]
    return node


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)

    for name, typ in [
        ("policy_schema", PolicySchema),
        ("user_profile", UserProfile),
        ("judgement_result", JudgementResult),
    ]:
        schema = normalize_descriptions(msgspec.json.schema(typ))
        path = OUT / f"{name}.json"
        path.write_text(
            json.dumps(schema, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"  {path.relative_to(OUT.parent.parent)}")

    # 룰이 참조 가능한 필드 목록도 계약의 일부다.
    # AI 가 여기 없는 field 를 만들면 BE 밸리데이터가 거부한다.
    fields = {
        "known_fields": sorted(KNOWN_FIELDS),
        "time_satisfiable_fields": sorted(TIME_SATISFIABLE_FIELDS),
        "note": (
            "룰의 field 는 known_fields 안에 있어야 한다. "
            "time_satisfiable=true 는 time_satisfiable_fields 에만 허용된다."
        ),
    }
    # 에러 유형도 계약이다. FE 는 상태 코드가 아니라 이 code 로 분기한다.
    problems = {
        "media_type": "application/problem+json",
        "note": (
            "RFC 9457. type 은 상대 URI(/problems/<code>) 이며 code 가 계약이다. "
            "detail 은 FastAPI 기본형과 같은 자리라 기존 코드가 계속 동작한다."
        ),
        "types": [
            {"code": t.code, "type": t.uri, "status": t.status, "title": t.title}
            for t in ALL_TYPES
        ],
    }
    path = OUT / "problems.json"
    path.write_text(
        json.dumps(problems, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"  {path.relative_to(OUT.parent.parent)}")

    path = OUT / "rule_fields.json"
    path.write_text(
        json.dumps(fields, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"  {path.relative_to(OUT.parent.parent)}")


if __name__ == "__main__":
    force_utf8_console()
    print("계약 스키마 내보내기:")
    main()
