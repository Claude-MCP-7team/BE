"""데모 데이터로 실제 API 를 호출해 응답을 녹화한다 (FE Mock 용).

FE 가 BE 없이 화면을 만들려면 **실제 응답 한 벌**이 필요하다. `docs/contracts/`
는 스키마지 인스턴스가 아니라서, 필드가 실제로 어떤 값으로 채워지는지
(빈 배열인지 null 인지, 날짜 형식이 무엇인지)를 알 수 없다.

손으로 쓴 예시를 두지 않는 이유: 응답 모양이 바뀌어도 손으로 쓴 파일은 그대로라
FE 가 없는 필드를 믿고 화면을 만든다. 그래서 **앱을 실제로 호출해서** 받아 적고,
테스트가 매번 다시 호출해 커밋된 것과 같은지 비교한다. 달라지면 CI 가 실패하고,
`python tools/record_mock_responses.py` 로 갱신한 뒤 함께 커밋하면 된다.

날짜가 섞이면 매일 달라지므로 `YPC_FIXED_TODAY` 로 기준일을 고정한다.

seed 를 그대로 적재하지 않고 **빌더를 통과시킨다.** 운영은 반드시 빌더를 지나므로,
녹화본도 같은 문을 지나야 FE 가 보는 것과 배포된 것이 같아진다. 빼먹으면 마감된
공고까지 목록에 실려, FE 는 실제로는 오지 않는 데이터로 화면을 만들게 된다.
"""

from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path
from typing import Any

import msgspec

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "data" / "demo"
OUT = DEMO / "responses"

# 기준일을 고정하지 않으면 나이·충족예상일·일정이 매일 바뀌어 diff 가 무의미해진다.
# E2E 시나리오(tests/e2e/test_demo_scenarios.py)와 같은 날짜여야 한다 — 두 곳이
# 갈라지면 FE 가 보는 Mock 과 발표에서 도는 화면이 달라진다.
FIXED_TODAY = "2026-09-24"

# 어떤 사용자로 부를지.
#
#   main      커밋된 데모 사용자. 소득 미기재라 네 가지 배지가 한 응답에 다 나온다
#   answered  역질문에 답한 뒤 → 조합·서류가 의미 있게 채워진다
MAIN, ANSWERED = "main", "answered"

# (파일명, 메서드, 경로, 어떤 프로필로 / GET 이면 None)
CALLS: list[tuple[str, str, str, str | None]] = [
    ("judge.json", "POST", "/v1/judge", MAIN),
    ("judge.all.json", "POST", "/v1/judge?include=all", MAIN),
    ("questions.json", "POST", "/v1/questions", MAIN),
    # 조합·일정은 역질문에 답한 뒤라야 볼 게 생긴다. 미확인 상태로 부르면
    # 적격이 1건뿐이라 조합에 담길 게 없고, 서류 배열이 통째로 비어 FE 가
    # 서류 화면을 한 번도 못 그려 본 채 제출하게 된다.
    ("combinations.json", "POST", "/v1/combinations", ANSWERED),
    ("plan.json", "POST", "/v1/plan", ANSWERED),
    ("policies.json", "GET", "/v1/policies", None),
    ("policy.detail.json", "GET", "/v1/policies/GG-12010", None),
    ("meta.snapshot.json", "GET", "/v1/meta/snapshot", None),
]

# 역질문에 사용자가 답한 값. 화면의 '답변 후 재판정'이 이 값으로 돈다.
ANSWER = {"household_income_ratio_median": 85}

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
    "latency_ms": 0,  # 실제로는 측정값. 공고 23건이면 0~1ms 다
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
    from batch.build_snapshot import build, load_policies

    # 운영과 같은 문을 지난다. 이걸 건너뛰면 마감된 공고까지 목록에 실린다.
    accepted, report = build(
        load_policies(DEMO / "policies.demo.json"),
        today=date.fromisoformat(FIXED_TODAY),
    )
    if report.rejected:
        raise SystemExit(f"데모 seed 가 빌더 검증에 걸렸습니다: {report.rejected}")
    if not accepted:
        raise SystemExit(
            f"기준일 {FIXED_TODAY} 에 게시 중인 공고가 없습니다 — "
            "신청기간이 지났습니다. 공고를 새로 수집할 것"
        )
    load_from_json(holder, msgspec.json.encode(accepted), version=f"demo-{FIXED_TODAY}")

    main_profile = json.loads((DEMO / "profile.demo.json").read_text(encoding="utf-8"))
    profiles = {MAIN: main_profile, ANSWERED: {**main_profile, "answers": ANSWER}}

    from app.main import app

    out: dict[str, Any] = {}
    with TestClient(app) as client:
        for name, method, path, who in CALLS:
            r = (
                client.post(path, json=profiles[who])
                if method == "POST" and who
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
