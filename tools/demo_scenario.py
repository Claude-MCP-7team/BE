"""제출용 데모 시나리오를 실행 중인 API 에 HTTP 로 재현한다.

  SNAPSHOT_PATH=data/manual/snapshot.json YPC_FIXED_TODAY=2026-09-19 \
      uvicorn app.main:app --port 8765
  python tools/demo_scenario.py --base http://127.0.0.1:8765

data/manual/ 의 실제 공고 3건을 전제로 한다. 화면(FE)이 아니라 응답 JSON 을 그대로
읽어 보여주므로, 판정·역질문·재판정·조합·계획이 실제로 연결되는지 사람이 눈으로 확인하는
용도다 (마일스톤 §M5 9/28 E2E 시나리오 1·4·5·6).
"""

from __future__ import annotations

import argparse
import json
from typing import Any

import httpx

from app.core.console import force_utf8_console

Profile = dict[str, Any]

# 시나리오 A — 24세 · 용인 · 2026-05-15 전입 · 대학생 · 소득 미입력
YONGIN: Profile = {
    "core": {
        "birth_date": "2002-03-01",
        "region_code": "41460",
        "residence_start_date": "2026-05-15",
        "education": "university_enrolled",
        "employment_status": "student",
    }
}
# 시나리오 B — 25세 · 가평 · 미혼 · 1인 가구 · 소득 90% · 재직
GAPYEONG: Profile = {
    "core": {
        "birth_date": "2001-06-10",
        "region_code": "41820",
        "marital_status": "single",
        "household_size": 1,
        "household_income_ratio_median": 90,
        "employment_status": "employed",
        "employment_start_date": "2025-01-02",
    }
}
# 시나리오 C — 23세(2026-10-15 에 24세) · 수원 · 2020-03-01 부터 거주 → FUTURE_PASS (시나리오 3)
SUWON_23: Profile = {
    "core": {
        "birth_date": "2002-10-15",
        "region_code": "41110",
        "residence_start_date": "2020-03-01",
        "employment_status": "job_seeking",
    }
}


def main() -> int:
    parser = argparse.ArgumentParser(description="데모 시나리오 재현")
    parser.add_argument("--base", default="http://127.0.0.1:8765")
    args = parser.parse_args()
    client = httpx.Client(base_url=args.base, timeout=10)

    def post(path: str, body: Profile) -> dict[str, Any]:
        r = client.post(path, json=body)
        r.raise_for_status()
        return r.json()  # type: ignore[no-any-return]

    def judge(title: str, profile: Profile) -> None:
        print(f"\n=== {title}")
        j = post("/v1/judge?include=all", profile)
        print("  summary:", j["summary"])
        for res in j["results"]:
            print(f"  - {res['policy_id']}: {res['verdict']} ({res['confidence']})")
            if res.get("explanation"):
                print(f"      💬 {res['explanation']}")
            for u in res["unmatched"]:
                quote = u["source_quote"][:50]
                need = f"내 값={u['user_value']} 필요={u['required']}"
                when = f"  → {u['satisfiable_from']}부터 가능" if u.get("satisfiable_from") else ""
                if u.get("permanently_unsatisfiable"):
                    when = "  → 시간이 지나도 충족 불가"
                print(f'      FAIL {u["field"]}: {need}{when}  ⟵ "{quote}"')
            for u in res["unknown"]:
                print(f"      ?    {u['field']}: {u['question_template']}")

    judge("A-1  24세 용인 대학생, 소득 미입력 → 확인 필요", YONGIN)

    print("\n=== A-2  역질문 큐 (필드별 병합)")
    for item in post("/v1/questions", YONGIN)["questions"]:
        print("  ", json.dumps(item, ensure_ascii=False)[:300])

    judge(
        "A-3  소득 120% 답변 후 재판정 → 적격",
        {**YONGIN, "answers": {"household_income_ratio_median": 120}},
    )

    judge("B-1  25세 가평 미혼 1인가구 직장인", GAPYEONG)

    judge("C-1  23세(다음 달 24세) 수원 거주 6년 → 청년기본소득 향후 가능일", SUWON_23)

    print("\n=== B-2  조합 추천")
    for scenario in post("/v1/combinations", GAPYEONG)["scenarios"]:
        for combo in scenario["combinations"]:
            names = ", ".join(m["policy_id"] for m in combo["members"])
            print(f"  {scenario['label']} #{combo['rank']}: {combo['total_krw']:,}원 = {names}")

    print("\n=== B-3  신청 계획")
    for plan in post("/v1/plan", GAPYEONG)["plans"]:
        print(
            f"  - {plan['title'][:30]}: {plan['status']} 마감 {plan['deadline_date']} "
            f"권장착수 {plan['recommended_start_date']} 서류 {len(plan['documents'])}종"
        )
        for d in plan["documents"]:
            flag = " [마스터 미검증]" if d.get("master_unverified") else ""
            days = d["lead_time_business_days"]
            print(f"      · {d['name']} ({d.get('doc_code')}) 소요 {days}일{flag}")
    return 0


if __name__ == "__main__":
    force_utf8_console()
    raise SystemExit(main())
