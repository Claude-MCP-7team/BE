"""PolicySchema 를 JSON Schema 로 내보낸다 — AI 역할과의 계약서(C1).

AI 역할은 A2 에이전트 출력이 이 스키마를 만족하는지 자체 검증할 수 있고,
BE 는 같은 정의로 디코딩한다. 한쪽만 바뀌면 CI 가 diff 로 잡는다.

사용법:  python tools/export_contract.py
출력:    docs/contracts/policy_schema.json
"""

from __future__ import annotations

import json
import pathlib
import sys

import msgspec

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.schemas.enums import KNOWN_FIELDS, TIME_SATISFIABLE_FIELDS  # noqa: E402
from app.schemas.judgement import JudgementResult  # noqa: E402
from app.schemas.policy import PolicySchema  # noqa: E402
from app.schemas.user import UserProfile  # noqa: E402

OUT = pathlib.Path(__file__).resolve().parent.parent / "docs" / "contracts"


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)

    for name, typ in [
        ("policy_schema", PolicySchema),
        ("user_profile", UserProfile),
        ("judgement_result", JudgementResult),
    ]:
        schema = msgspec.json.schema(typ)
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
    path = OUT / "rule_fields.json"
    path.write_text(
        json.dumps(fields, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"  {path.relative_to(OUT.parent.parent)}")


if __name__ == "__main__":
    print("계약 스키마 내보내기:")
    main()
