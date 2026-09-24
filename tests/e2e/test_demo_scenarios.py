"""제출용 E2E 시나리오 (마일스톤 v3.0 §9).

여기서 검증하는 것은 모듈이 아니라 **데모 그 자체**다. 발표에서 보여줄 경로를
그대로 HTTP 로 밟는다: `data/demo/` 의 고정 공고·사용자 → 스냅샷 빌더 →
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
# 데모 기준일. seed 를 수집한 날로 고정한다. 앞뒤로 옮기면 게시 건수가 바뀐다 —
# 실공고는 신청기간이 짧아서 하루만 밀려도 목록이 줄어든다 (data/demo/README.md).
DEMO_TODAY = "2026-09-24"

# 이름은 '데모에서 무엇을 보여주는가'로 붙였다. ID 만 적어두면 단언이 왜 그 값인지
# 읽히지 않는다.
ELIGIBLE = "20260317005400112169"  # 매장유산 — 적격이고 서류가 붙는다
ASKABLE = "GG-12010"  # 가평 월세 — 소득을 밝히면 적격
ASKABLE_FAIL = "20260528005400113227"  # 농식품 바우처 — 같은 답으로 부적격이 된다
FUTURE = "JB-5885"  # 경기도 청년기본소득 — 24세가 되면 해당
INELIGIBLE = "GG-12048"  # 포천 전용 — 가평 사용자에게는 지역 불일치

EXPIRED = "GG-12023"  # 용인, 9/22 마감 — 스냅샷에 들어가면 안 된다


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
    r = client.post("/v1/judge", json=profile, params={"include": "all", **params})
    assert r.status_code == 200, r.text
    return {item["policy_id"]: item for item in r.json()["results"]}


# --- 카탈로그: 판정 이전에 목록이 선다 (M1 완료 판정) ----------------------


def test_정책_목록을_서버에서_조회한다(client):
    r = client.get("/v1/policies")
    assert r.status_code == 200
    body = r.json()

    assert body["total"] == 23
    assert len(body["items"]) == 20  # limit 기본값

    # 마감 임박순. 시연 중 목록 순서가 흔들리면 준비한 설명과 화면이 어긋난다.
    deadlines = [i["apply_end"] for i in body["items"]]
    assert deadlines == sorted(deadlines)

    # 근거 링크는 전부 있어야 한다 — 없으면 빌더가 애초에 거부한다
    assert all(i["origin_url"] for i in body["items"])


def test_담당부서_연락처는_대부분_비어_있다(client):
    """FE 가 `dept_tel` 을 필수로 그리면 카드 대부분이 깨진다.

    연락처는 API 구조화 필드에 없고 공고문 본문에서 뽑아야 한다. A2(LLM)를 거친
    공고만 가지고 있고, 나머지는 `null` 이다. 데이터가 좋아지면 이 테스트가
    깨지면서 FE 에 알릴 시점을 알려준다.
    """
    items = client.get("/v1/policies", params={"limit": 100}).json()["items"]
    with_tel = [i["policy_id"] for i in items if i["dept_tel"]]
    assert 0 < len(with_tel) < len(items), "연락처 유무가 한쪽으로 쏠렸습니다"


def test_마감된_공고는_목록에_서지_않는다(client):
    """마감된 공고를 열린 것처럼 보여주면 사용자가 헛수고를 한다."""
    보이는것 = {i["policy_id"] for i in client.get("/v1/policies", params={"limit": 100}).json()["items"]}
    assert EXPIRED not in 보이는것

    # 목록에 없으면 상세도 없다 — 링크를 직접 친 사람에게 404 를 준다
    assert client.get(f"/v1/policies/{EXPIRED}").status_code == 404


def test_지역_필터는_상위지역과_전국을_포함한다(client):
    가평 = client.get("/v1/policies", params={"region": "41820", "limit": 100}).json()
    서울 = client.get("/v1/policies", params={"region": "11680", "limit": 100}).json()

    assert {i["policy_id"] for i in 가평["items"]} >= {ASKABLE, FUTURE, ELIGIBLE}
    # 서울 사용자에게는 전국 정책만 남는다 (경기 공고는 빠진다)
    assert INELIGIBLE not in {i["policy_id"] for i in 서울["items"]}
    assert 0 < 서울["total"] < 가평["total"]


def test_시_단위로_입력해도_구_단위_공고가_보인다(client):
    """API 는 용인을 41461·41463·41465(구)로 적는다. 사용자가 41460(시)으로

    입력하면 예전에는 교집합이 비어 **목록에서 통째로 사라졌다** — 부적격도
    확인필요도 아니라 화면에 흔적이 없었다 (HANDOFF §8 #20).
    """
    시 = client.get("/v1/policies", params={"region": "41460", "limit": 100}).json()
    구 = client.get("/v1/policies", params={"region": "41461", "limit": 100}).json()
    assert 시["total"] == 구["total"] > 0
    assert {i["policy_id"] for i in 시["items"]} == {i["policy_id"] for i in 구["items"]}


def test_목록_페이지네이션은_전체_건수를_유지한다(client):
    body = client.get("/v1/policies", params={"limit": 2, "offset": 2}).json()
    assert body["total"] == 23  # 페이지 길이가 아니라 필터 후 전체 건수
    assert len(body["items"]) == 2

    전체 = client.get("/v1/policies", params={"limit": 100}).json()["items"]
    assert [i["policy_id"] for i in body["items"]] == [i["policy_id"] for i in 전체[2:4]]


def test_같은_목록_질의는_304로_끝난다(client):
    first = client.get("/v1/policies")
    again = client.get("/v1/policies", headers={"If-None-Match": first.headers["ETag"]})
    assert again.status_code == 304


# --- Scenario 1: 신청 가능 -------------------------------------------------


def test_시나리오1_신청_가능_정책에_근거가_붙는다(client, profile):
    result = results_of(client, profile)[ELIGIBLE]

    assert result["verdict"] == "ELIGIBLE"
    assert not result["unmatched"] and not result["unknown"]
    # 충족한 조건에도 원문 인용이 있어야 '왜 되는지'를 보여줄 수 있다
    assert all(m["source_quote"] for m in result["matched"])
    assert all(m["source_url"] for m in result["matched"])

    # 실공고에는 구조화되지 않는 문구가 섞여 있다. 조건을 다 충족해도 확신을
    # 낮춘다 — '적격'이라고 단정해서 반려되면 신고가 온다.
    assert result["confidence"] == "NEEDS_REVIEW"
    assert result["disclaimer_required"] is True


# --- Scenario 2: 부적격 + 명확한 사유 --------------------------------------


def test_시나리오2_부적격은_사유와_원문을_함께_준다(client, profile):
    result = results_of(client, profile)[INELIGIBLE]

    assert result["verdict"] == "INELIGIBLE"
    지역 = next(u for u in result["unmatched"] if u["field"] == "region_code")
    assert 지역["required"] == ["41650"]  # 포천
    assert "41820" in 지역["user_value"]  # 가평
    assert 지역["source_quote"]
    # 사는 곳은 기다린다고 바뀌지 않는다 — 없는 희망을 주면 안 된다
    assert 지역["time_satisfiable"] is False
    assert 지역["satisfiable_from"] is None


# --- Scenario 3: 시간이 지나면 충족 (FUTURE_PASS) --------------------------


def test_시나리오3_충족_예상일이_날짜로_나온다(client, profile):
    result = results_of(client, profile)[FUTURE]

    assert result["verdict"] == "INELIGIBLE"
    나이 = next(u for u in result["unmatched"] if u["field"] == "age")
    assert 나이["required"] == [24, 24]
    assert 나이["user_value"] == 23
    assert 나이["time_satisfiable"] is True
    # 2003-03-15 생 → 24세가 되는 2027-03-15 부터 해당된다
    assert 나이["satisfiable_from"] == "2027-03-15"
    assert 나이["permanently_unsatisfiable"] is False

    # 화면이 네 번째 배지를 그리는 신호. 조건별 날짜를 FE 가 훑지 않아도 된다.
    assert result["future_eligible_from"] == "2027-03-15"


def test_시나리오3_기본_응답에_충족_예상일이_실려온다(client, profile):
    """부적격이라고 빼버리면 기본 화면에서 '언제부터 가능한가'가 사라진다."""
    body = client.post("/v1/judge", json=profile).json()  # include 기본값

    ids = {item["policy_id"] for item in body["results"]}
    assert FUTURE in ids  # 2027-03-15 부터 가능 — 기본 화면에 남아야 한다
    assert INELIGIBLE not in ids  # 포천 거주자가 아니다 — 기다려도 안 된다

    assert body["summary"]["future_eligible"] == 1
    assert body["summary"]["future_eligible"] <= body["summary"]["ineligible"]


# --- Scenario 4: UNKNOWN → 역질문 → 재판정 ---------------------------------


def test_시나리오4_역질문_하나가_정책_둘을_반대로_가른다(client, profile):
    """같은 답이 한 정책은 적격으로, 다른 정책은 부적격으로 만든다.

    질문을 정책별이 아니라 **필드별로 병합**하는 이유가 여기서 보인다. 정책마다
    물었다면 사용자는 같은 소득을 두 번 답한다.
    """
    before = results_of(client, profile)
    assert before[ASKABLE]["verdict"] == "NEEDS_INFO"
    assert before[ASKABLE_FAIL]["verdict"] == "NEEDS_INFO"

    queue = client.post("/v1/questions", json=profile).json()
    question = next(
        q for q in queue["questions"] if q["field"] == "household_income_ratio_median"
    )
    assert question["resolves"] == 2  # 이 답 하나로 판정이 끝나는 정책 수
    assert set(question["source_policy_ids"]) == {ASKABLE, ASKABLE_FAIL}
    assert question["source_quote"]
    # 단위를 묻지 않으면 사용자가 원 단위 월소득을 적는다 (150 이 아니라 2,500,000)
    assert question["answer_type"] == "number"

    after = results_of(client, {**profile, "answers": {question["field"]: 85}})
    assert after[ASKABLE]["verdict"] == "ELIGIBLE"  # 150% 이하
    assert after[ASKABLE_FAIL]["verdict"] == "INELIGIBLE"  # 32% 이하가 아니다
    assert not after[ASKABLE]["unknown"] and not after[ASKABLE_FAIL]["unknown"]

    소득 = next(
        u for u in after[ASKABLE_FAIL]["unmatched"]
        if u["field"] == "household_income_ratio_median"
    )
    assert (소득["required"], 소득["user_value"]) == (32, 85)


# --- Scenario 5: 중복수혜 → 추천 조합 --------------------------------------


def test_시나리오5_적격_정책을_함께_담은_조합이_나온다(client, profile):
    answered = {**profile, "answers": {"household_income_ratio_median": 85}}
    body = client.post("/v1/combinations", json=answered).json()

    assert body["eligible_count"] >= 2
    보수, 최대 = (
        next(s for s in body["scenarios"] if s["kind"] == k)
        for k in ("conservative", "maximal")
    )
    보수1, 최대1 = 보수["combinations"][0], 최대["combinations"][0]
    assert 보수1["total_krw"] > 0

    # 지금 seed 에는 서로 상충하는 '동시에 적격인' 두 공고가 없다. 그래서 보수안과
    # 최대안이 같아진다. 같다는 걸 단언해 두는 이유: 나중에 상충이 생기면 이
    # 테스트가 먼저 깨져서, 조합 로직이 조용히 지나가지 않는다.
    assert 최대1 == 보수1
    assert 보수1["excluded"] == []


def test_시나리오5_상충_선언은_있지만_기간이_겹치지_않는다():
    """왜 '상충을 피한 두 가지 조합'을 실데이터로 못 보여주는지를 코드에 남긴다.

    가평 월세는 국토부 청년월세를 명시적으로 배제한다. 상충 해소 로직을 시연할
    진짜 재료지만, 두 공고의 신청기간이 겹치지 않아 한 스냅샷에 같이 설 수 없다.
    기간이 겹치는 공고를 새로 수집하면 이 테스트가 깨진다 — 그때 시나리오 5 의
    두 갈래(보수/최대)를 복원하면 된다 (HANDOFF §8 #16).
    """
    seed = msgspec.json.decode(
        (DEMO / "policies.demo.json").read_bytes(), type=list[PolicySchema]
    )
    policies = {p.policy_id: p for p in seed}

    월세 = policies[ASKABLE]
    명시상충 = [c for c in 월세.conflicts if c.type == "explicit_policy"]
    assert 명시상충, "가평 월세의 명시 상충 선언이 사라졌습니다"
    assert 명시상충[0].target_policy_name == "청년월세 한시 특별지원"
    assert 명시상충[0].confidence == "CONFIRMED"
    assert 명시상충[0].source_quote.strip()

    상대 = policies["20260319005400112218"]  # (국토부) 26년 청년월세 지원사업
    assert 상대.period.apply_end < 월세.period.apply_start, (
        "두 공고의 신청기간이 겹칩니다 — 이제 시나리오 5 의 보수/최대 갈림길을 "
        "실데이터로 복원할 수 있습니다"
    )


# --- Scenario 6: 필요서류 → 신청 Timeline ----------------------------------


def test_시나리오6_서류에서_착수일이_역산된다(client, profile):
    answered = {**profile, "answers": {"household_income_ratio_median": 85}}
    body = client.post("/v1/plan", json=answered).json()
    plans = {p["policy_id"]: p for p in body["plans"]}

    plan = plans[ELIGIBLE]
    assert plan["deadline_date"] == "2026-11-30"
    assert {d["doc_code"] for d in plan["documents"]} == {"D020", "D022"}

    # 서류는 한 번에 같이 신청한다 — 합(3+3)이 아니라 최댓값(3)+제출 1일.
    # 합으로 세면 모든 정책이 '급함'으로 떠서 무엇이 진짜 급한지 구별이 사라진다.
    assert max(d["lead_time_business_days"] for d in plan["documents"]) == 3
    assert plan["preparation_business_days"] == 4
    assert plan["status"] == "ON_TRACK"
    assert plan["slack_business_days"] > 0

    # 재학증명서는 유효기간 30일이다. 너무 일찍 떼면 제출일엔 만료된 종이다 —
    # 졸업증명서(90일)보다 늦게 떼야 해서, 전체 착수 가능일이 그쪽으로 밀린다.
    졸업 = next(d for d in plan["documents"] if d["doc_code"] == "D020")
    재학 = next(d for d in plan["documents"] if d["doc_code"] == "D022")
    assert (졸업["validity_days"], 재학["validity_days"]) == (90, 30)
    assert 재학["issue_not_before"] > 졸업["issue_not_before"]
    assert plan["issue_not_before_date"] == 재학["issue_not_before"]


def test_시나리오6_마감이_지나기_전에_못_끝내면_INFEASIBLE_이다(client, profile):
    """추석 연휴(9/24~9/25, 9/28 대체공휴일) 한가운데가 기준일이다.

    가평 월세는 9/30 마감인데 남은 영업일이 1일뿐이라 3일짜리 준비가 들어가지
    않는다. 달력을 안 보고 '일주일 남았다'고 세면 사용자를 못 보낼 신청에 보낸다.
    """
    answered = {**profile, "answers": {"household_income_ratio_median": 85}}
    plans = {p["policy_id"]: p for p in client.post("/v1/plan", json=answered).json()["plans"]}

    plan = plans[ASKABLE]
    assert plan["deadline_date"] == "2026-09-30"
    assert plan["business_days_to_deadline"] == 1
    assert plan["preparation_business_days"] == 3
    assert plan["slack_business_days"] == -2
    assert plan["status"] == "INFEASIBLE"


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
    assert len(policies) == 31
    # 근거 없는 룰은 이 저장소에서 존재할 수 없다 (DB CHECK 와 같은 규칙)
    for policy in policies:
        for rule in [*policy.eligibility, *policy.exclusions]:
            assert rule.source_quote.strip()
        for conflict in policy.conflicts:
            assert conflict.source_quote.strip()


def test_데모_seed_는_실공고다():
    """합성 데이터로 되돌아가면 여기서 잡는다.

    합성 공고를 발표에 쓰는 것 자체는 선택의 문제지만, 실데이터인 줄 알고 쓰는
    건 아니다. 출처 URL 과 수집 경로를 단언해 둬서 둘이 섞이지 않게 한다.
    """
    policies = msgspec.json.decode(
        (DEMO / "policies.demo.json").read_bytes(), type=list[PolicySchema]
    )
    for policy in policies:
        assert policy.source.api == "ontong", policy.policy_id
        assert ".invalid" not in policy.source.origin_url, policy.policy_id
        assert not policy.policy_id.startswith("DEMO-"), policy.policy_id
        assert not policy.meta.title.startswith("[데모]"), policy.policy_id
