"""MCP 서버 — 네트워크 없이 검사할 수 있는 부분: 지역 코드 조회와 프로필 조립.

도구 호출 자체는 API 서버가 있어야 하므로 여기서는 안 한다. 실제 호출은
`python tools/mcp_server.py` 를 Claude Code 에 붙여 확인한다 (README).
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("mcp")

from tools.mcp_server import GYEONGGI_CODES, _profile, find_region_code, server  # noqa: E402


def test_시군구_이름을_법정동_코드로_바꾼다():
    assert json.loads(find_region_code("가평군")) == {"name": "가평군", "region_code": "41820"}
    assert json.loads(find_region_code("용인 수지"))["region_code"] == "41465"
    assert json.loads(find_region_code("수지구"))["region_code"] == "41465"


def test_구가_있는_시는_시_이름만으로는_확정하지_않는다():
    out = json.loads(find_region_code("용인"))
    assert set(out["ambiguous"]) == {"용인시 처인구", "용인시 기흥구", "용인시 수지구"}


def test_경기도_밖은_모른다고_한다():
    assert "not_found" in json.loads(find_region_code("서울 강남구"))
    assert "not_found" in json.loads(find_region_code(""))


def test_코드표는_경기도_5자리_코드다():
    assert len(GYEONGGI_CODES) == 44
    assert all(len(c) == 5 and c.startswith("41") for c in GYEONGGI_CODES.values())
    assert len(set(GYEONGGI_CODES.values())) == len(GYEONGGI_CODES)


def test_프로필은_비운_값을_보내지_않는다():
    p = _profile("2002-03-01", "41465", None, "", None, None, None, None, None, None)
    assert p == {"core": {"birth_date": "2002-03-01", "region_code": "41465"}}
    p = _profile("2002-03-01", "41465", "2026-05-15", None, "student", None, None, 1, 120, {"x": 1})
    assert p["core"]["household_size"] == 1 and p["core"]["household_income_ratio_median"] == 120
    assert p["answers"] == {"x": 1}


def test_도구_여섯_개가_등록되어_있다():
    import asyncio

    tools = asyncio.run(server.list_tools())
    assert {t.name for t in tools} == {
        "find_region_code",
        "judge",
        "questions",
        "combinations",
        "plan",
        "policy",
    }
    judge = next(t for t in tools if t.name == "judge")
    assert {"birth_date", "region_code"} <= set(judge.input_schema["required"])
    assert "NEEDS_INFO" in (judge.description or "") and "answers" in (judge.description or "")
