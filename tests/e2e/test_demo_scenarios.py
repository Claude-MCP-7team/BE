"""제출용 E2E 시나리오 6종 (마일스톤 v3.0 §9, 9/28).

여기서 검증하는 것은 모듈이 아니라 **데모 그 자체**다. 발표에서 보여줄 경로를
그대로 HTTP 로 밟는다: `data/demo/` 의 고정 정책·사용자 → 스냅샷 빌더 →
목록 → 판정 → 역질문 → 재판정 → 조합 → 서류·일정.

왜 단위 테스트로 충분하지 않은가
  각 계층은 이미 단위 테스트가 있다. 그런데 데모가 깨지는 방식은 대개 계층
  안이 아니라 계층 **사이**다 — 필드 이름이 하나 바뀌거나, 빌더가 정책을
  걸러내거나, 정렬이 흔들려 시연 중 화면에서 정책이 사라진다. 그 누락은
  '부적격'도 '확인필요'도 아니라서 발표 직전까지 아무도 모른다.

왜 스냅샷을 빌더로 만드는가
  커밋된 seed 를 그대로 컴파일하면 빌더의 검증 관문을 건너뛴다. 실제 운영은
  반드시 빌더를 지나므로, 데모도 같은 문을 지나야 '빌드되는 데이터'임이
  보장된다. seed 가 관문에 걸리면 이 테스트가 먼저 실패한다.

기준일을 고정하는 이유
  나이·마감·영업일 역산이 전부 오늘에 의존한다. 고정하지 않으면 같은 코드가
  날짜가 바뀌었다는 이유로 어느 날 갑자기 실패한다.
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
from app.schemas.policy import PolicySchema
from app.schemas.user import UserProfile
from batch.build_snapshot import build, load_policies

DEMO = pathlib.Path(__file__).resolve().parents[2] / "data" / "demo"
# 데모 기준일. 제출일로 고정한다 — 시연 당일과 같은 날짜로 돌아야 화면의
# '남은 영업일'이 발표 내용과 어긋나지 않는다.
DEMO_TODAY = "2026-10-01"

ELIGIBLE = "DEMO-GG-2026-0001"
INELIGIBLE = "DEMO-MOLIT-2026-0002"
FUTURE = "DEMO-BUCHEON-2026-0003"
ASKABLE = "DEMO-GG-2026-0004"
BIGGEST = "DEMO-MOEL-2026-0005"


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
    # 빌더 기준일도 고정한다. 실제 오늘로 빌드하면 데모 정책의 마감일이 지나는
    # 순간 전부 걸러져, 시나리오가 "정책 0건"으로 깨진다.
    accepted, report = build(policies, today=date.fromisoformat(DEMO_TODAY))
    assert not report.rejected, f"데모 seed 가 빌더 검증에 걸렸습니다: {report.rejected}"
    assert len(accepted) == len(policies)

    load_from_json(global_holder, msgspec.json.encode(accepted), version="demo-e2e")

    from app.main import app

    with TestClient(app) as c:
        yield c


def results_of(client, profile, **params) -> dict[str, dict]:
    r = client.post("/v1/judge", json=profile, params={"include": "all", **params})
    assert r.status_code == 200, r.text
    return {item["policy_id"]: item for item in r.json()["results"]}


# --- 카탈로그: 판정 이전에 목록이 선다 (M1 완료 판정) ----------------------


def test_정책_목록을_서버에서_조회한다(client):
    r = client.get("/v1/policies")
    assert r.status_code == 200
    body = r.json()

    assert body["total"] == 5
    assert len(body["items"]) == 5
    # 마감 임박순. 시연 중 목록 순서가 흔들리면 준비한 설명과 화면이 어긋난다.
    assert [i["policy_id"] for i in body["items"]] == [
        INELIGIBLE, BIGGEST, ELIGIBLE, ASKABLE, FUTURE
    ]
    # 카드가 근거 링크와 담당부서를 들고 있어야 상세를 열지 않고도 확인 가능하다
    assert all(i["origin_url"] and i["dept_tel"] for i in body["items"])


def test_지역_필터는_상위지역과_전국을_포함한다(client):
    부천 = client.get("/v1/policies", params={"region": "41190"}).json()
    서울 = client.get("/v1/policies", params={"region": "11680"}).json()

    # 부천 사용자는 부천·경기·전국 정책을 모두 본다
    assert 부천["total"] == 5
    # 서울 사용자에게는 전국 정책만 남는다 (경기·부천 정책은 빠진다)
    assert {i["policy_id"] for i in 서울["items"]} == {INELIGIBLE, BIGGEST}


def test_목록_페이지네이션은_전체_건수를_유지한다(client):
    body = client.get("/v1/policies", params={"limit": 2, "offset": 2}).json()
    assert body["total"] == 5  # 페이지 길이가 아니라 필터 후 전체 건수
    assert [i["policy_id"] for i in body["items"]] == [ELIGIBLE, ASKABLE]


def test_같은_목록_질의는_304로_끝난다(client):
    first = client.get("/v1/policies")
    again = client.get("/v1/policies", headers={"If-None-Match": first.headers["ETag"]})
    assert again.status_code == 304


# --- Scenario 1: 신청 가능 -------------------------------------------------


def test_시나리오1_신청_가능_정책에_근거가_붙는다(client, profile):
    results = results_of(client, profile)

    assert results[ELIGIBLE]["verdict"] == "ELIGIBLE"
    assert results[BIGGEST]["verdict"] == "ELIGIBLE"
    # 충족한 조건에도 원문 인용이 있어야 '왜 되는지'를 보여줄 수 있다
    assert all(m["source_quote"] for m in results[ELIGIBLE]["matched"])
    assert not results[ELIGIBLE]["unmatched"]


# --- Scenario 2: 부적격 + 명확한 사유 --------------------------------------


def test_시나리오2_부적격은_사유와_원문을_함께_준다(client, profile):
    result = results_of(client, profile)[INELIGIBLE]

    assert result["verdict"] == "INELIGIBLE"
    소득 = next(u for u in result["unmatched"] if u["field"] == "household_income_ratio_median")
    assert 소득["required"] == 60
    assert 소득["user_value"] == 85
    assert 소득["source_quote"]
    # 소득은 시간이 지난다고 충족되지 않는다 — 없는 희망을 주면 안 된다
    assert 소득["satisfiable_from"] is None


# --- Scenario 3: 시간이 지나면 충족 (FUTURE_PASS) --------------------------


def test_시나리오3_충족_예상일이_날짜로_나온다(client, profile):
    result = results_of(client, profile)[FUTURE]

    assert result["verdict"] == "INELIGIBLE"
    거주 = next(u for u in result["unmatched"] if u["field"] == "residence_months_continuous")
    assert 거주["time_satisfiable"] is True
    # 2024-04-01 전입 → 기준일에 30개월 → 36개월까지 6개월 더
    assert 거주["satisfiable_from"] == "2027-04-01"
    assert 거주["permanently_unsatisfiable"] is False

    # 화면이 네 번째 배지를 그리는 신호. 조건별 날짜를 FE 가 직접 훑지 않아도 된다.
    assert result["future_eligible_from"] == "2027-04-01"


def test_시나리오3_기본_응답에_충족_예상일이_실려온다(client, profile):
    """부적격이라고 빼버리면 기본 화면에서 '언제부터 가능한가'가 사라진다."""
    body = client.post("/v1/judge", json=profile).json()  # include 기본값

    ids = {item["policy_id"] for item in body["results"]}
    assert FUTURE in ids
    assert INELIGIBLE not in ids  # 소득 초과 — 기다려도 안 된다

    assert body["summary"]["future_eligible"] == 1
    assert body["summary"]["future_eligible"] <= body["summary"]["ineligible"]


# --- Scenario 4: UNKNOWN → 역질문 → 재판정 ---------------------------------


def test_시나리오4_역질문에_답하면_판정이_끝난다(client, profile):
    before = results_of(client, profile)
    assert before[ASKABLE]["verdict"] == "NEEDS_INFO"

    queue = client.post("/v1/questions", json=profile).json()
    question = next(
        q for q in queue["questions"] if q["field"] == "similar_program_participation_2y"
    )
    # 이 답 하나로 판정이 끝나는 정책 수. 화면 문구가 쓰는 값이다.
    assert question["resolves"] == 1
    assert question["source_quote"]
    assert ASKABLE in question["source_policy_ids"]

    answered = {**profile, "answers": {question["field"]: False}}
    after = results_of(client, answered)
    assert after[ASKABLE]["verdict"] == "ELIGIBLE"


# --- Scenario 5: 중복수혜 → 추천 조합 --------------------------------------


def test_시나리오5_상충을_피한_조합이_두_가지로_나온다(client, profile):
    answered = {**profile, "answers": {"similar_program_participation_2y": False}}
    body = client.post("/v1/combinations", json=answered).json()

    보수, 최대 = (next(s for s in body["scenarios"] if s["kind"] == k)
                  for k in ("conservative", "maximal"))
    보수1, 최대1 = 보수["combinations"][0], 최대["combinations"][0]

    # 명시 상충(CONFIRMED)은 양쪽 다 피한다 — 두 정책이 함께 담기면 안 된다
    for combination in (보수1, 최대1):
        담긴것 = {m["policy_id"] for m in combination["members"]}
        assert not {ELIGIBLE, BIGGEST} <= 담긴것

    # 추정 상충(ESTIMATED)에서 두 안이 갈린다. 한쪽으로 단정하면 사용자가 손해를
    # 본다 — 지키면 받을 걸 놓치고, 무시하면 반려된다.
    assert 최대1["total_krw"] > 보수1["total_krw"]
    assert {m["policy_id"] for m in 최대1["members"]} == {BIGGEST, ASKABLE}
    assert {m["policy_id"] for m in 보수1["members"]} == {BIGGEST}

    # 제외된 정책에는 원문과 담당부서 연락처가 붙는다 (사용자가 직접 확인할 수단)
    제외 = {e["policy_id"]: e for e in 보수1["excluded"]}
    assert ASKABLE in 제외 and ELIGIBLE in 제외
    assert 제외[ELIGIBLE]["confidence"] == "CONFIRMED"
    assert 제외[ASKABLE]["confidence"] == "ESTIMATED"
    assert all(e["source_quote"] and e["dept_tel"] for e in 제외.values())


# --- Scenario 6: 필요서류 → 신청 Timeline ----------------------------------


def test_시나리오6_서류에서_착수일이_역산된다(client, profile):
    answered = {**profile, "answers": {"similar_program_participation_2y": False}}
    body = client.post("/v1/plan", json=answered).json()
    plans = {p["policy_id"]: p for p in body["plans"]}

    plan = plans[BIGGEST]
    assert plan["deadline_date"] == "2026-12-10"
    assert len(plan["documents"]) == 5

    # 서류는 한 번에 같이 신청한다 — 합(0+0+0+0+3)이 아니라 최댓값(3)+제출 1일.
    # 합으로 세면 모든 정책이 '급함'으로 떠서 무엇이 진짜 급한지 구별이 사라진다.
    최장 = max(d["lead_time_business_days"] for d in plan["documents"])
    assert 최장 == 3
    assert plan["preparation_business_days"] == 4

    # 납세증명서는 유효기간 30일이다. 너무 일찍 떼면 제출일엔 만료된 종이다.
    납세 = next(d for d in plan["documents"] if d["doc_code"] == "D011")
    assert 납세["issue_not_before"] == "2026-11-11"
    assert plan["issue_not_before_date"] == "2026-11-11"


def test_ics_로_내보낼_수_있다(client, profile):
    r = client.post("/v1/plan.ics", json=profile)
    assert r.status_code == 200
    assert "text/calendar" in r.headers["content-type"]
    assert "BEGIN:VCALENDAR" in r.text


# --- 결정론: 같은 입력에 같은 결과 (M2 완료 판정) --------------------------


def test_같은_조건이면_몇_번을_물어도_같은_판정이다(client, profile):
    first = client.post("/v1/judge", json=profile, params={"include": "all"}).content
    for _ in range(3):
        assert client.post("/v1/judge", json=profile, params={"include": "all"}).content == first


def test_데모_seed_는_스키마를_만족한다():
    """커밋된 파일 자체가 계약면을 지키는지. 손으로 고칠 때의 안전망이다."""
    policies = msgspec.json.decode(
        (DEMO / "policies.demo.json").read_bytes(), type=list[PolicySchema]
    )
    assert len(policies) == 5
    assert all(p.status == "published" for p in policies)
    # 근거 없는 룰은 이 저장소에서 존재할 수 없다 (DB CHECK 와 같은 규칙)
    for policy in policies:
        for rule in [*policy.eligibility, *policy.exclusions]:
            assert rule.source_quote.strip()
        for conflict in policy.conflicts:
            assert conflict.source_quote.strip()
