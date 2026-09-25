"""E2E 공통 설비 — 커밋된 데모 seed 로 스냅샷을 세운 앱.

`test_demo_scenarios.py` 안에 있던 것을 옮겨 왔다. 시나리오 테스트 말고도 데모
스냅샷을 밟아야 하는 테스트(답변 정규화 등)가 생겼는데, fixture 를 모듈에서
import 하면 같은 이름이 테스트 인자와 겹쳐 ruff F811 이 난다. conftest 는 pytest 가
자동으로 찾으므로 import 자체가 필요 없다.

기준일을 고정하는 이유
  나이·마감·영업일 역산이 전부 오늘에 의존한다. 고정하지 않으면 같은 코드가 날짜가
  바뀌었다는 이유로 어느 날 갑자기 실패한다.

빌더를 통과시키는 이유
  커밋된 seed 를 그대로 컴파일하면 빌더의 검증 관문을 건너뛴다. 실제 운영은 반드시
  빌더를 지나므로, 데모도 같은 문을 지나야 '빌드되는 데이터'임이 보장된다.
"""

from __future__ import annotations

import json
import pathlib
from datetime import date

import msgspec
import pytest
from fastapi.testclient import TestClient

from app.engine.snapshot import holder as global_holder
from app.engine.snapshot import load_from_json
from app.schemas.user import UserProfile
from batch.build_snapshot import build, load_policies

DEMO = pathlib.Path(__file__).resolve().parents[2] / "data" / "demo"
# 데모 기준일. seed 를 수집한 날로 고정한다. 앞뒤로 옮기면 게시 건수가 바뀐다 —
# 실공고는 신청기간이 짧아서 하루만 밀려도 목록이 줄어든다 (data/demo/README.md).
DEMO_TODAY = "2026-09-24"


@pytest.fixture
def profile() -> dict:
    """커밋된 데모 사용자. 읽어서 쓰므로 seed 가 바뀌면 시나리오도 함께 바뀐다."""
    raw = (DEMO / "profile.demo.json").read_bytes()
    msgspec.json.decode(raw, type=UserProfile)  # 스키마 위반이면 여기서 터진다
    return json.loads(raw.decode("utf-8"))


@pytest.fixture
def client(monkeypatch):
    """커밋된 seed 를 빌더에 통과시킨 뒤 적재한 앱."""
    monkeypatch.setenv("YPC_FIXED_TODAY", DEMO_TODAY)

    policies = load_policies(DEMO / "policies.demo.json")
    # 빌더 기준일도 고정한다. 실제 오늘로 빌드하면 공고 마감일이 지나는 순간
    # 걸러져서, 시나리오가 "정책이 없다"로 깨진다.
    accepted, report = build(policies, today=date.fromisoformat(DEMO_TODAY))
    assert not report.rejected, f"데모 seed 가 빌더 검증에 걸렸습니다: {report.rejected}"
    # 31건 중 23건이 게시된다. '거부'(검증 위반)가 아니라 '미게시'(기간 경과)다 —
    # 둘을 같은 수로 세면 데이터가 상한 것과 공고가 끝난 것을 구별할 수 없다.
    assert len(accepted) == 23
    assert dict(report.skipped_by_status) == {"expired": 8}

    load_from_json(global_holder, msgspec.json.encode(accepted), version="demo-e2e")

    from app.main import app

    with TestClient(app) as c:
        yield c


def results_of(client, profile, **params) -> dict[str, dict]:
    r = client.post("/v1/judge", json=profile, params={"include": "all"} | params)
    assert r.status_code == 200, r.text
    return {item["policy_id"]: item for item in r.json()["results"]}
