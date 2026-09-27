"""역질문 답변 정규화 (AI-M3) 를 실공고로 검증한다 — 이슈 #3 6번.

무엇을 확인하는가
  사용자는 "한 85% 정도요", "혼자 살아요" 라고 답한다. 엔진은 정수·불리언·코드값만
  비교한다. 그 사이를 `app.llm.answers` 가 잇는데, 지금까지 이 모듈은 단위 테스트만
  있었고 **실제 판정에 연결된 적이 없었다.**

여기서 고정하는 경계 (실측)
  1. 정규화를 거친 답은 사람이 타입 맞춰 넣은 답과 **같은 판정**을 낸다 (결과 전체 비교).
  2. 정규화를 거치지 않은 숫자 답("85", "85%")은 422 로 거부된다. 범위 가드가 정수만
     받기 때문이다 — 조용히 통과하는 쪽보다 낫다.
  3. 정규화를 거치지 않은 enum 답("혼자 살아요")도 이제 422 다. 예전에는 통과해서
     `== "single"` 비교가 불일치로 읽혔고, 가평 월세가 NEEDS_INFO → INELIGIBLE 로
     뒤집혔다 (`tests/unit/test_profile_bounds.py` 가 그 가드를 지킨다).
  4. 알아들을 수 없는 답은 빠져서 필드가 UNKNOWN 으로 남는다 — 추측한 값으로 메우지
     않는다.

따라서 정규화는 **서버에 닿기 전에** 끝나야 한다. MCP 경로는
`tools/mcp_server.py::_profile` 이 `apply_answers` 를 불러 그 일을 한다. FE 폼은
타입이 잡혀 있어 해당 없지만, 자유 입력을 붙인다면 같은 변환이 필요하다.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.llm.answers import apply_answers

# `client` · `profile` fixture 는 conftest 에서 온다 — 데모 시나리오와 **같은 seed·
# 같은 기준일**이어야 여기서 본 판정이 거기서 본 판정과 같은 것임이 보장된다.
from tests.e2e.conftest import DEMO_TODAY, results_of
from tests.e2e.test_demo_scenarios import ASKABLE, ASKABLE_FAIL

TODAY = date.fromisoformat(DEMO_TODAY)


def test_말로_한_답과_숫자로_한_답이_같은_판정을_낸다(client, profile):
    """MCP 경로가 실제로 같은 판정에 도달하는지. 여기가 끊기면 Claude 를 통해
    답한 사용자만 다른 결과를 본다 — 화면이 두 갈래가 된 걸 아무도 모른다.
    """
    typed = {**profile, "answers": {"household_income_ratio_median": 85}}
    spoken = apply_answers(
        profile, {"household_income_ratio_median": "한 85% 정도요"}, today=TODAY
    )

    assert spoken["answers"] == {"household_income_ratio_median": 85}

    a = client.post("/v1/judge", json=typed, params={"include": "all"})
    b = client.post("/v1/judge", json=spoken, params={"include": "all"})
    assert a.status_code == b.status_code == 200, (a.text, b.text)
    # session_id 는 호출마다 새로 생기므로 판정 내용만 본다
    assert (a.json()["results"], a.json()["summary"]) == (
        b.json()["results"],
        b.json()["summary"],
    )

    # 시나리오 4 와 같은 갈림길이 말로 답해도 나와야 한다
    after = {i["policy_id"]: i for i in b.json()["results"]}
    assert after[ASKABLE]["verdict"] == "ELIGIBLE"  # 150% 이하
    assert after[ASKABLE_FAIL]["verdict"] == "INELIGIBLE"  # 32% 이하가 아니다


def test_말로_한_답_여러_개가_한꺼번에_들어간다(client, profile):
    """묶어 보내도 필드별로 각자의 타입으로 바뀌어야 한다."""
    spoken = apply_answers(
        profile,
        {
            "household_income_ratio_median": "백이십 퍼센트쯤 돼요",
            "household_size": "혼자 살아요",
            "marital_status": "아직 미혼이에요",
            "employment_status": "취업 준비 중이에요",
            "similar_program_participation_2y": "없어요",
        },
        today=TODAY,
    )
    assert spoken["answers"] == {
        "household_income_ratio_median": 120,
        "household_size": 1,
        "marital_status": "single",
        "employment_status": "job_seeking",
        "similar_program_participation_2y": False,
    }
    assert client.post("/v1/judge", json=spoken).status_code == 200


@pytest.mark.parametrize(
    ("field", "answer"),
    [
        ("household_income_ratio_median", "85"),  # 숫자를 문자열로
        ("household_income_ratio_median", "85%"),
        ("household_income_ratio_median", "한 85% 정도요"),
        ("household_size", "혼자 살아요"),
        ("marital_status", "혼자 살아요"),  # enum — 예전에는 조용히 부적격이 됐다
        ("employment_status", "취준생이에요"),
        ("similar_program_participation_2y", "없어요"),
    ],
)
def test_정규화하지_않은_답은_판정에_닿지_못한다(client, profile, field, answer):
    """사용자 말을 그대로 `answers` 에 실으면 422 다.

    통과시키면 안 되는 이유: 룰은 `== "single"`, `<= 150` 처럼 비교만 한다. 형태가
    맞는 엉뚱한 값은 예외를 내지 않고 **불일치**로 읽혀 그럴듯한 부적격이 된다.
    422 는 정규화를 빼먹은 쪽(FE·MCP)에게 즉시 알려 준다.
    """
    r = client.post("/v1/judge", json={**profile, "answers": {field: answer}})
    assert r.status_code == 422, r.text
    # FE 는 상태 코드가 아니라 problem type 으로 분기한다 (docs/contracts/problems.json)
    assert r.json()["type"] == "/problems/invalid-profile"
    assert field in r.json()["detail"]


def test_알아들을_수_없는_답은_버려서_다시_묻게_한다(client, profile):
    """추측한 값으로 메우면 확신에 찬 오판정이 된다. 빼면 그냥 다시 묻는다."""
    before = results_of(client, profile)
    assert before[ASKABLE]["verdict"] == "NEEDS_INFO"

    spoken = apply_answers(
        profile, {"household_income_ratio_median": "그건 잘 모르겠어요"}, today=TODAY
    )
    assert "household_income_ratio_median" not in spoken.get("answers", {})

    after = results_of(client, spoken)
    assert after[ASKABLE]["verdict"] == "NEEDS_INFO"
    assert any(
        u["field"] == "household_income_ratio_median" for u in after[ASKABLE]["unknown"]
    )


def test_한글_숫자는_앞에_말이_붙으면_못_읽는다(client, profile):
    """알려진 한계를 안전한 방향으로 고정한다.

    `_korean_number` 는 첫 글자부터 숫자로 읽으므로 "백이십 퍼센트"는 되고
    "소득이 백이십 퍼센트쯤"은 None 이 된다. 못 읽은 답은 빠지므로 결과는 '다시
    묻기'다 — 엉뚱한 값으로 판정하는 것보다 낫다. 파서를 키우려면 이 테스트를
    먼저 고쳐서, 어느 쪽으로 넓히는지 드러나게 할 것.
    """
    읽힌다 = apply_answers(profile, {"household_income_ratio_median": "백이십 퍼센트"}, today=TODAY)
    assert 읽힌다["answers"]["household_income_ratio_median"] == 120

    못읽는다 = apply_answers(
        profile, {"household_income_ratio_median": "소득이 백이십 퍼센트쯤"}, today=TODAY
    )
    assert "household_income_ratio_median" not in 못읽는다.get("answers", {})
    assert results_of(client, 못읽는다)[ASKABLE]["verdict"] == "NEEDS_INFO"


def test_모르는_필드에_답해도_프로필이_오염되지_않는다(client, profile):
    """Claude 가 없는 필드명을 만들어 낼 수 있다. 통과하면 422 로 판정 자체가 죽는다."""
    spoken = apply_answers(profile, {"annual_income_krw": "3000만원"}, today=TODAY)
    assert "annual_income_krw" not in spoken.get("answers", {})
    assert client.post("/v1/judge", json=spoken).status_code == 200


def test_개월_수_답은_core_시작일로_들어간다(client, profile):
    """엔진은 `resolve()` 에서 거주·근속 개월을 **시작일로부터 센다.**

    답을 개월 수 그대로 `answers` 에 넣어도 폴백이 받아 주지만, 시작일로 바꿔 넣으면
    기준일이 달라져도 같은 사실을 유지한다 — "2년 살았어요"는 내년에는 3년이다.
    """
    spoken = apply_answers(
        profile, {"residence_months_continuous": "작년 5월부터 살고 있어요"}, today=TODAY
    )
    assert "residence_months_continuous" not in spoken.get("answers", {})
    assert spoken["core"]["residence_start_date"] == "2025-05-24"
    assert client.post("/v1/judge", json=spoken).status_code == 200
