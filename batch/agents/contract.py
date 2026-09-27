"""A2 출력 계약 — 모델이 돌려줘야 하는 JSON 의 형태.

PolicySchema 를 그대로 쓰지 않고 별도 스키마를 두는 이유:
  1. 룰의 value 가 숫자·문자열·불리언·배열 중 하나라 JSON Schema 로는 union 이 되는데,
     구조화 출력은 단일 타입일수록 안정적이다. 타입별 칸(value_number ...)으로 나눈다.
  2. 모델에게는 rule_id, basis, source_offset 같은 BE 내부 필드를 보여줄 이유가 없다.
  3. 규칙으로 옮길 수 없는 조건을 버리지 않고 받을 자리(unrepresentable_conditions)가 필요하다.

변환은 structure.py 가 한다. 스키마의 enum 은 app.schemas.enums 와 같은 값이어야 하며,
테스트가 드리프트를 잡는다.
"""

from __future__ import annotations

import typing
from typing import Any

from app.schemas.enums import KNOWN_FIELDS, BenefitType, Category

# 모델이 만들 수 있는 필드. region_code 와 received_policy_ids 는 제외한다 —
# 지역은 API 코드가 권위이고, 정책 ID 는 모델이 알 수 없다 (프롬프트와 일치).
STRUCTURABLE_FIELDS: tuple[str, ...] = tuple(
    sorted(KNOWN_FIELDS - {"region_code", "received_policy_ids"})
)

_NULLABLE_STR = {"type": ["string", "null"]}
_NULLABLE_INT = {"type": ["integer", "null"]}


def _obj(properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


CONDITION_SCHEMA = _obj(
    {
        "kind": {"type": "string", "enum": ["eligibility", "exclusion"]},
        "field": {"type": "string", "enum": list(STRUCTURABLE_FIELDS)},
        "op": {"type": "string", "enum": [">=", "<=", "between", "in", "not_in", "=="]},
        "value_number": {"type": ["number", "null"]},
        "value_text": _NULLABLE_STR,
        "value_bool": {"type": ["boolean", "null"]},
        "value_list": {
            "type": ["array", "null"],
            "items": {"type": ["string", "number"]},
        },
        "unit": _NULLABLE_STR,
        "source_quote": {"type": "string"},
        "time_satisfiable": {"type": "boolean"},
        "askable": {"type": "boolean"},
        "question_template": _NULLABLE_STR,
        "ambiguous": {"type": "boolean"},
        "confidence": {"type": "string", "enum": ["CONFIRMED", "ESTIMATED", "NEEDS_REVIEW"]},
        "note": _NULLABLE_STR,
    }
)

A2_OUTPUT_SCHEMA: dict[str, Any] = _obj(
    {
        "conditions": {"type": "array", "items": CONDITION_SCHEMA},
        "unrepresentable_conditions": {
            "type": "array",
            "items": _obj(
                {
                    "summary": {"type": "string"},
                    "source_quote": {"type": "string"},
                    "reason": {"type": "string"},
                }
            ),
        },
        "benefit": _obj(
            {
                "type": {
                    "type": ["string", "null"],
                    "enum": [*typing.get_args(BenefitType), None],
                },
                "amount_krw": _NULLABLE_INT,
                "duration_months": _NULLABLE_INT,
                "estimated_total_krw": _NULLABLE_INT,
                "amount_confidence": {"type": "string", "enum": ["CONFIRMED", "ESTIMATED"]},
                "source_quote": _NULLABLE_STR,
            }
        ),
        "period": _obj(
            {
                "apply_start": _NULLABLE_STR,
                "apply_end": _NULLABLE_STR,
                "is_rolling": {"type": "boolean"},
                "source_quote": _NULLABLE_STR,
            }
        ),
        "documents": {
            "type": "array",
            "items": _obj(
                {
                    "name": {"type": "string"},
                    "issuer": _NULLABLE_STR,
                    # [서류 마스터 목록] 중 하나. 같은 서류가 확실할 때만, 아니면 null
                    "canonical_name": _NULLABLE_STR,
                    "source_quote": {"type": "string"},
                }
            ),
        },
        "conflicts": {
            "type": "array",
            "items": _obj(
                {
                    "type": {
                        "type": "string",
                        "enum": ["explicit_policy", "category_overlap", "same_authority"],
                    },
                    "target_policy_name": _NULLABLE_STR,
                    # 솔버(app/solver/graph.py)는 target_category 를 meta.category 와 등호로
                    # 비교한다. 자유 문구를 넣으면 간선이 조용히 하나도 안 생긴다.
                    "target_category": {
                        "type": ["string", "null"],
                        "enum": [*typing.get_args(Category), None],
                    },
                    "target_benefit_type": {
                        "type": ["string", "null"],
                        "enum": [*typing.get_args(BenefitType), None],
                    },
                    "target_authority": _NULLABLE_STR,
                    "source_quote": {"type": "string"},
                    "confidence": {"type": "string", "enum": ["CONFIRMED", "ESTIMATED"]},
                }
            ),
        },
        "dept": _obj(
            {
                "name": _NULLABLE_STR,
                "tel": _NULLABLE_STR,
                "source_quote": _NULLABLE_STR,
            }
        ),
    }
)
