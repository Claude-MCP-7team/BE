"""데모 데이터로 실제 API 를 호출해 응답을 녹화한다 (FE Mock 용).

FE 가 BE 없이 화면을 만들려면 **실제 응답 한 벌**이 필요하다. `docs/contracts/`
는 스키마지 인스턴스가 아니라서, 필드가 실제로 어떤 값으로 채워지는지
(빈 배열인지 null 인지, 날짜 형식이 무엇인지)를 알 수 없다.

손으로 쓴 예시를 두지 않는 이유: 응답 모양이 바뀌어도 손으로 쓴 파일은 그대로라
FE 가 없는 필드를 믿고 화면을 만든다. 그래서 **앱을 실제로 호출해서** 받아 적고,
테스트가 매번 다시 호출해 커밋된 것과 같은지 비교한다. 달라지면 CI 가 실패하고,
`python tools/record_mock_responses.py` 로 갱신한 뒤 함께 커밋하면 된다.

날짜가 섞이면 매일 달라지므로 `YPC_FIXED_TODAY` 로 기준일을 고정한다.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "data" / "demo"
OUT = DEMO / "responses"

# 기준일을 고정하지 않으면 나이·충족예상일·일정이 매일 바뀌어 diff 가 무의미해진다.
FIXED_TODAY = "2026-10-01"

# (파일명, 메서드, 경로, 본문을 보낼지)
CALLS: list[tuple[str, str, str, bool]] = [
    ("judge.json", "POST", "/v1/judge", True),
    ("judge.all.json", "POST", "/v1/judge?include=all", True),
    ("questions.json", "POST", "/v1/questions", True),
    ("combinations.json", "POST", "/v1/combinations", True),
    ("plan.json", "POST", "/v1/plan", True),
    ("policies.json", "GET", "/v1/policies", False),
    ("policy.detail.json", "GET", "/v1/policies/DEMO-BUCHEON-2026-0003", False),
    ("meta.snapshot.json", "GET", "/v1/meta/snapshot", False),
]

# 에러 응답도 화면이 있다. FE 가 문구와 모양을 미리 맞춰 볼 수 있어야 한다.
ERROR_CALLS: list[tuple[str, str, str, bytes]] = [
    ("error.invalid-request.json", "POST", "/v1/judge", b"{oops"),
    (
        "error.invalid-profile.json",
        "POST",
        "/v1/judge",
        b'{"core": {"birth_date": "1998-03-14", "region_code": "41190",'
        b' "household_income_ratio_median": 3000000}}',
    ),
    ("error.policy-not-found.json", "GET", "/v1/policies/NO-SUCH-POLICY", b""),
    ("error.route-not-found.json", "GET", "/no-such-path", b""),
]


# 호출할 때마다 달라지는 값. 그대로 두면 녹화본이 매번 diff 가 나서, 진짜 응답
# 변화와 시계 차이를 구분할 수 없게 된다. 녹화기와 대조 테스트가 **같은 함수**를
# 쓰므로 둘이 갈라질 수 없다.
VOLATILE = {
    "latency_ms": 0,  # 실제로는 측정값. 데모 5건이면 0~1ms 다
    "loaded_at": f"{FIXED_TODAY}T00:00:00+00:00",
}


def stabilize(node: object) -> object:
    """매 호출 달라지는 값을 고정값으로 바꾼다. 다른 필드는 건드리지 않는다."""
    if isinstance(node, dict):
        return {
            key: VOLATILE[key] if key in VOLATILE else stabilize(value)
            for key, value in node.items()
        }
    if isinstance(node, list):
        return [stabilize(item) for item in node]
    return node


def record() -> dict[str, Any]:
    """앱을 인-프로세스로 띄워 호출하고 {파일명: 응답} 을 돌려준다."""
    os.environ["YPC_FIXED_TODAY"] = FIXED_TODAY
    os.environ.pop("CORS_ORIGINS", None)

    from fastapi.testclient import TestClient

    from app.engine.snapshot import holder, load_from_json

    policies = (DEMO / "policies.demo.json").read_bytes()
    load_from_json(holder, policies, version=f"demo-{FIXED_TODAY}")
    profile = json.loads((DEMO / "profile.demo.json").read_text(encoding="utf-8"))

    from app.main import app

    out: dict[str, Any] = {}
    with TestClient(app) as client:
        for name, method, path, send_body in CALLS:
            r = (
                client.post(path, json=profile)
                if method == "POST" and send_body
                else client.get(path)
            )
            r.raise_for_status()
            out[name] = stabilize(r.json())

        for name, method, path, body in ERROR_CALLS:
            r = (
                client.post(path, content=body,
                            headers={"Content-Type": "application/json"})
                if method == "POST"
                else client.get(path)
            )
            out[name] = stabilize(r.json())
    return out


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, payload in record().items():
        (OUT / name).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"  data/demo/responses/{name}")


if __name__ == "__main__":
    from app.core.console import force_utf8_console

    force_utf8_console()
    print(f"Mock 응답 녹화 (기준일 {FIXED_TODAY}):")
    main()
