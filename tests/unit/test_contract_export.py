"""계약 JSON 이 인터프리터 버전에 따라 달라지지 않는지 본다.

CI 는 `python tools/export_contract.py` 를 돌린 뒤 `git diff --exit-code` 로
드리프트를 잡는다. 그 검사는 '계약이 코드와 달라졌다'는 뜻이어야 하는데,
한 번은 그렇지 않은 이유로 깨졌다: Python 3.13 부터 컴파일러가 docstring 의
공통 들여쓰기를 제거하는데 CI 는 3.11 이라, 3.14 기계에서 내보낸 JSON 을
커밋하자 CI 가 실패했다 (2069740). 사람이 바꾼 게 없는데 빨간불이 뜨면
다음 사람은 계약을 의심하며 엉뚱한 곳을 뒤진다.

그래서 내보내기가 description 을 정규화하고, 여기서 그걸 확인한다.
테스트 자체는 어느 버전에서 돌아도 같은 결론을 내야 하므로, 3.11 의 출력을
합성해서 넣는다 — 3.14 에서 돌리면 진짜 docstring 은 이미 dedent 되어 있어
정규화가 빠져도 통과해버리기 때문이다.
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import pathlib

CONTRACTS = pathlib.Path(__file__).resolve().parents[2] / "docs" / "contracts"
TOOL = pathlib.Path(__file__).resolve().parents[2] / "tools" / "export_contract.py"


def _tool_module():
    spec = importlib.util.spec_from_file_location("export_contract", TOOL)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_indented_description_is_dedented() -> None:
    """3.11 이 내놓는 모양을 그대로 넣어 본다."""
    normalize = _tool_module().normalize_descriptions

    schema = {
        "$defs": {
            "Rule": {
                "description": "첫 줄.\n\n    둘째 줄.\n    셋째 줄.",
                "properties": {"field": {"description": "한 줄짜리는 그대로."}},
                "anyOf": [{"description": "배열 안도 본다.\n\n    들여쓰인 줄."}],
            }
        }
    }

    out = normalize(schema)
    assert out["$defs"]["Rule"]["description"] == "첫 줄.\n\n둘째 줄.\n셋째 줄."
    assert out["$defs"]["Rule"]["properties"]["field"]["description"] == "한 줄짜리는 그대로."
    assert out["$defs"]["Rule"]["anyOf"][0]["description"] == "배열 안도 본다.\n\n들여쓰인 줄."


def test_normalization_is_idempotent() -> None:
    """3.13+ 의 이미 dedent 된 입력에도 같은 결과여야 한 파일이 두 버전을 만족한다."""
    normalize = _tool_module().normalize_descriptions

    once = normalize({"description": "첫 줄.\n\n    둘째 줄."})
    assert normalize(once) == once


def test_committed_contracts_carry_no_source_indentation() -> None:
    """커밋된 계약 파일이 어느 기계에서 내보낸 것이든 같은 모양이어야 한다."""
    for path in sorted(CONTRACTS.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        for description in _descriptions(payload):
            assert description == inspect.cleandoc(description), (
                f"{path.name} 의 description 에 소스 들여쓰기가 남아 있습니다. "
                "python tools/export_contract.py 를 다시 돌리세요."
            )


def _descriptions(node: object):
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "description" and isinstance(value, str):
                yield value
            else:
                yield from _descriptions(value)
    elif isinstance(node, list):
        for item in node:
            yield from _descriptions(item)
