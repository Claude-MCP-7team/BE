"""API 응답을 형식에 관계없이 레코드 리스트로 편다.

온통청년 API 는 XML 로 제공된다고 안내되어 있으나 버전에 따라 JSON 도 반환한다.
어느 쪽인지 확정하기 전에 수집을 시작할 수 있도록 둘 다 받는다.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from typing import Any

Record = dict[str, Any]


def parse_payload(raw: bytes | str) -> tuple[list[Record], str]:
    """(레코드 리스트, 감지된 포맷) 을 돌려준다. 포맷은 'json' | 'xml'."""
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
    stripped = text.lstrip()

    if stripped.startswith(("{", "[")):
        return _flatten_json(json.loads(text)), "json"
    if stripped.startswith("<"):
        return _flatten_xml(text), "xml"

    raise ValueError(f"JSON 도 XML 도 아닌 응답입니다: {stripped[:80]!r}")


def _flatten_json(doc: Any) -> list[Record]:
    """응답 봉투(envelope)를 벗겨 실제 레코드 배열을 찾는다.

    공공 API 는 레코드를 여러 겹으로 감싸는 경우가 많고 그 이름이 제각각이라
    (result / data / youthPolicyList / items ...) 키 이름을 가정하지 않는다.
    대신 '딕셔너리들의 배열' 중 가장 큰 것을 레코드 배열로 본다.
    """
    if isinstance(doc, list):
        return [d for d in doc if isinstance(d, dict)]

    best: list[Record] = []

    def walk(node: Any) -> None:
        nonlocal best
        if isinstance(node, list):
            dicts = [d for d in node if isinstance(d, dict)]
            if len(dicts) > len(best):
                best = dicts
            for item in node:
                walk(item)
        elif isinstance(node, dict):
            for value in node.values():
                walk(value)

    walk(doc)
    # 배열이 전혀 없으면 응답 자체가 단일 레코드일 수 있다
    return best or ([doc] if isinstance(doc, dict) else [])


def _flatten_xml(text: str) -> list[Record]:
    """가장 많이 반복되는 태그를 레코드 단위로 본다.

    루트 바로 아래를 레코드로 가정하면 <response><body><items><item> 같은
    중첩 봉투에서 레코드를 1건으로 잘못 세게 된다.
    """
    root = ET.fromstring(text)

    parents: dict[ET.Element, list[ET.Element]] = {}
    for parent in root.iter():
        children = list(parent)
        if children:
            parents[parent] = children

    best_children: list[ET.Element] = []
    for children in parents.values():
        # 같은 태그가 반복되는 묶음이 레코드 배열이다
        tags = {c.tag for c in children}
        if len(tags) == 1 and len(children) > len(best_children):
            best_children = children

    if not best_children:
        best_children = [root]

    return [_element_to_dict(el) for el in best_children]


def _element_to_dict(el: ET.Element) -> Record:
    out: Record = dict(el.attrib)
    for child in el:
        value: Any = _element_to_dict(child) if len(child) else (child.text or "")
        if child.tag in out:  # 반복 태그는 리스트로 모은다
            prev = out[child.tag]
            out[child.tag] = [*prev, value] if isinstance(prev, list) else [prev, value]
        else:
            out[child.tag] = value
    if not out and el.text:
        return {el.tag: el.text}
    return out
