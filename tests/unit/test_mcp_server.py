"""MCP 서버 — 네트워크 없이 검사할 수 있는 부분: 지역 코드 조회와 프로필 조립.

도구 호출 자체는 API 서버가 있어야 하므로 여기서는 안 한다. 실제 호출은
`python tools/mcp_server.py` 를 Claude Code 에 붙여 확인한다 (README).
"""

from __future__ import annotations

import json
import pathlib

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
    p = _profile(
        "2002-03-01",
        "41465",
        "2026-05-15",
        None,
        "student",
        None,
        None,
        1,
        120,
        {"similar_program_participation_2y": "없어요", "x": 1},
    )
    assert p["core"]["household_size"] == 1 and p["core"]["household_income_ratio_median"] == 120
    assert p["answers"] == {"similar_program_participation_2y": False}  # 모르는 필드 x 는 빠진다


def test_core_의_enum_도_사용자_말을_코드값으로_바꾼다():
    """도구 설명에 코드값을 적어 뒀어도 모델은 사용자 말을 그대로 넣을 수 있다.

    core 는 Literal 로 타입이 잡혀 있어서 못 바꾼 값은 422 로 판정이 아예 죽는다.
    바꿀 수 있는 건 바꿔 두는 쪽이 사용자가 보는 화면에서 차이가 크다.
    """
    p = _profile(
        "2002-03-01", "41465", None, "대학 졸업했어요", "취준생이에요", None, "미혼", None, None, None
    )
    assert p["core"]["education"] == "university_graduated"
    assert p["core"]["employment_status"] == "job_seeking"
    assert p["core"]["marital_status"] == "single"


def test_바꿀_수_없는_enum_값은_지우지_않고_그대로_둔다():
    """조용히 지우면 그 조건을 본 적도 없이 판정이 나간다. 422 로 막히는 게 낫다."""
    p = _profile("2002-03-01", "41465", None, "잘 모르겠어요", None, None, None, None, None, None)
    assert p["core"]["education"] == "잘 모르겠어요"


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


# --- .mcp.json 이 어느 OS 에서나 떠야 한다 ---------------------------------


def test_mcp_설정이_OS별_인터프리터_경로를_박지_않는다():
    """`.venv/Scripts/python.exe` 로 적혀 있어 Linux·macOS 에서 ENOENT 로 죽었다.

    JSON 에는 분기가 없어서 한쪽 경로를 적으면 다른 쪽에서 서버가 **아예 안 뜬다.**
    도구 목록이 비는 게 아니라 연결 자체가 실패해서, 쓰는 사람은 서버가 있는 줄도
    모른다. 그래서 `python` 을 부르고, 스크립트가 필요할 때 .venv 로 옮겨간다.
    """
    root = pathlib.Path(__file__).resolve().parents[2]
    config = json.loads((root / ".mcp.json").read_text(encoding="utf-8"))
    command = config["mcpServers"]["ypc"]["command"]

    assert "\\" not in command and "/" not in command, (
        f"인터프리터 경로가 박혀 있습니다: {command!r}"
    )
    assert ".exe" not in command


def test_의존성이_없으면_venv_로_다시_띄운다(monkeypatch):
    """재실행이 없으면 시스템 python 으로 불린 순간 ModuleNotFoundError 로 죽는다.

    `.venv` 가 실제로 있는지에 기대지 않는다 — CI 는 가상환경 없이 전역에 설치해서
    처음 쓴 테스트가 CI 에서만 깨졌다. 인터프리터를 찾는 일과 그걸로 다시 띄우는
    일을 나눠 두고, 여기서는 뒤쪽만 본다.
    """
    import subprocess

    import tools.mcp_server as server

    called: dict[str, object] = {}
    fake = pathlib.Path("/nowhere/.venv/bin/python")

    def fake_run(argv, **kw):
        called["argv"] = argv
        called["env"] = kw.get("env", {})
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.delenv(server._RELAUNCHED, raising=False)
    monkeypatch.setattr(server.importlib.util, "find_spec", lambda name: None)
    monkeypatch.setattr(server, "_venv_python", lambda: fake)
    monkeypatch.setattr(server.subprocess, "run", fake_run)

    with pytest.raises(SystemExit):
        server._relaunch_in_venv()

    assert called["argv"][0] == str(fake)
    # 표식이 없으면 재실행된 인터프리터에도 mcp 가 없을 때 무한히 다시 띄운다
    assert called["env"][server._RELAUNCHED] == "1"


def test_venv_가_없으면_그냥_넘어간다(monkeypatch):
    """CI 처럼 가상환경 없이 전역 설치한 환경. 여기서 죽으면 서버가 안 뜬다."""
    import tools.mcp_server as server

    monkeypatch.delenv(server._RELAUNCHED, raising=False)
    monkeypatch.setattr(server.importlib.util, "find_spec", lambda name: None)
    monkeypatch.setattr(server, "_venv_python", lambda: None)
    monkeypatch.setattr(
        server.subprocess, "run", lambda *a, **k: pytest.fail("띄울 인터프리터가 없다")
    )

    server._relaunch_in_venv()  # 조용히 돌아오고, import 단계에서 안내가 나간다


def test_표식이_있으면_다시_띄우지_않는다(monkeypatch):
    import tools.mcp_server as server

    monkeypatch.setenv(server._RELAUNCHED, "1")
    monkeypatch.setattr(server.importlib.util, "find_spec", lambda name: None)
    monkeypatch.setattr(
        server.subprocess, "run", lambda *a, **k: pytest.fail("다시 띄우면 안 된다")
    )
    server._relaunch_in_venv()  # 아무 일도 없어야 한다
