"""아키텍처 성능 가설 검증 벤치마크.

docs/ARCHITECTURE.md 의 ADR-001/002/003 이 주장하는 수치를 실제로 측정한다.
CI 에서 매번 돌려 성능 회귀를 감시한다 (숫자가 무너지면 설계 전제가 무너진 것).

  ADR-001  스냅샷을 RAM 에 올려도 되는가       → 메모리 실측
  ADR-002  룰 평가를 벡터화하면 충분히 빠른가  → p50 실측
  ADR-003  PuLP/CBC 없이 정확해를 낼 수 있는가 → 완전탐색 대조 + 지연 실측

사용법:  python bench/engine_bench.py
"""

from __future__ import annotations

import pathlib
import random
import sys
import time
from collections.abc import Iterator
from datetime import date

# `python bench/engine_bench.py` 로 바로 실행할 수 있게 레포 루트를 경로에 넣는다
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.core.console import force_utf8_console  # noqa: E402
from app.engine.compile import compile_snapshot  # noqa: E402
from app.engine.evaluate import explain, judge_all  # noqa: E402
from app.planner.backplan import build_plan  # noqa: E402
from app.planner.businessday import bundled_calendar  # noqa: E402
from app.schemas.policy import (  # noqa: E402
    Benefit,
    Dept,
    Document,
    Meta,
    Period,
    PolicySchema,
    Rule,
    Source,
)
from app.schemas.user import Core, UserProfile  # noqa: E402

SCALES = (600, 3000)  # 1차 범위 / 전국 확장 시나리오


# --- ADR-001 / 002: 실제 룰 엔진 ---------------------------------------------


def _sample_policies(n: int, rnd: random.Random) -> list[PolicySchema]:
    """1차 범위와 비슷한 분포의 정책을 만든다 (지역·소득·거주·취업상태 조건)."""
    out = []
    for i in range(n):
        rules = [
            Rule(rule_id="AGE", field="age", op="between", value=[19, 34],
                 source_quote="만 19세 이상 34세 이하"),
            Rule(rule_id="REG", field="region_code", op="in",
                 value=rnd.choice([["00"], ["41"], ["41465"], ["11"], ["11680"]]),
                 source_quote="거주지 요건"),
        ]
        if rnd.random() < 0.6:
            rules.append(Rule(rule_id="RES", field="residence_months_continuous", op=">=",
                              value=rnd.choice([3, 6, 12]), unit="months",
                              source_quote="계속하여 거주"))
        if rnd.random() < 0.7:
            rules.append(Rule(rule_id="INC", field="household_income_ratio_median", op="<=",
                              value=rnd.choice([100, 120, 150, 180]),
                              source_quote="기준 중위소득 이하"))
        if rnd.random() < 0.5:
            rules.append(Rule(rule_id="EMP", field="employment_status", op="in",
                              value=rnd.sample(["employed", "job_seeking", "student"], 2),
                              source_quote="취업 상태"))
        excl = []
        if rnd.random() < 0.25:
            excl.append(Rule(rule_id="SIM", field="similar_program_participation_2y",
                             op="==", value=False, askable=True,
                             question_template="최근 2년 이내 유사사업에 참여한 적 있나요?",
                             source_quote="타 유사사업 참여자는 제외"))
        out.append(PolicySchema(
            policy_id=f"P{i:05d}", status="published",
            meta=Meta(title=f"정책{i}", category="housing", authority_level="local",
                      region_code=["41465"], dept=Dept(name="청년정책과", tel="031-000-0000")),
            source=Source(origin_url=f"https://example.kr/{i}"),
            eligibility=rules, exclusions=excl,
        ))
    return out


def _sample_profile() -> UserProfile:
    return UserProfile(core=Core(
        birth_date=date(2001, 3, 14), region_code="41465",
        residence_start_date=date(2025, 11, 1), employment_status="job_seeking",
        household_income_ratio_median=120,
    ))


# --- ADR-003: bitset 분기한정 MWIS ------------------------------------------


def iter_bits(mask: int) -> Iterator[int]:
    while mask:
        low = mask & -mask
        yield low.bit_length() - 1
        mask ^= low


def mwis(weights: list[int], adj: list[int]) -> tuple[int, int]:
    """최대 가중 독립집합의 정확해. (총 가중치, 선택 비트마스크) 를 돌려준다.

    adj[i] 는 i 와 상충하는 정점들의 비트마스크다. 남은 후보 전체를
    정수 하나로 들고 다니므로 집합 연산이 전부 CPU 비트 연산이 된다.
    """
    best_weight = 0
    best_set = 0

    def recurse(cand: int, cur_w: int, cur_set: int) -> None:
        nonlocal best_weight, best_set
        # 상계: 남은 후보를 전부 담아도 최선을 못 넘으면 가지를 버린다
        if cur_w + sum(weights[i] for i in iter_bits(cand)) <= best_weight:
            return
        if not cand:
            if cur_w > best_weight:
                best_weight, best_set = cur_w, cur_set
            return
        v = (cand & -cand).bit_length() - 1
        # v 를 넣는다 → v 와 상충하는 정점은 후보에서 제거
        recurse(cand & ~(1 << v) & ~adj[v], cur_w + weights[v], cur_set | (1 << v))
        # v 를 뺀다
        recurse(cand & ~(1 << v), cur_w, cur_set)

    recurse((1 << len(weights)) - 1, 0, 0)
    return best_weight, best_set


def brute_force(weights: list[int], adj: list[int]) -> int:
    """완전탐색. 솔버의 정답을 대조하기 위한 기준선 (G4 게이트: 100% 일치)."""
    n = len(weights)
    best = 0
    for subset in range(1 << n):
        if any((subset >> i) & 1 and (adj[i] & subset) for i in range(n)):
            continue
        best = max(best, sum(weights[i] for i in range(n) if (subset >> i) & 1))
    return best


def random_graph(n: int, density: float, rnd: random.Random) -> tuple[list[int], list[int]]:
    weights = [rnd.randint(1, 30) * 100_000 for _ in range(n)]
    adj = [0] * n
    for i in range(n):
        for j in range(i + 1, n):
            if rnd.random() < density:
                adj[i] |= 1 << j
                adj[j] |= 1 << i
    return weights, adj


# --- 실행 -------------------------------------------------------------------


def bench_rules() -> None:
    rnd = random.Random(42)
    profile = _sample_profile()
    today = date(2026, 9, 13)

    for n in SCALES:
        policies = _sample_policies(n, rnd)

        t = time.perf_counter()
        snapshot = compile_snapshot(policies, version="bench")
        compile_ms = (time.perf_counter() - t) * 1000

        judge_all(snapshot, profile, today)  # 워밍업
        t = time.perf_counter()
        for _ in range(200):
            verdicts = judge_all(snapshot, profile, today)
        judge_ms = (time.perf_counter() - t) / 200 * 1000

        # 최악 가정: 대시보드가 전 정책의 근거를 한 번에 조립한다
        t = time.perf_counter()
        for i in range(n):
            verdict = (
                "ELIGIBLE" if verdicts.eligible[i]
                else "INELIGIBLE" if verdicts.ineligible[i]
                else "NEEDS_INFO"
            )
            explain(snapshot, profile, today, i, verdict)
        explain_ms = (time.perf_counter() - t) * 1000

        s = verdicts.summary()
        print(
            f"[ADR-002] 정책 {n:5d}건 (룰 {len(snapshot.rule_refs):5d}) : "
            f"판정 {judge_ms:6.3f} ms  전체상세 {explain_ms:6.1f} ms  컴파일 {compile_ms:5.1f} ms  "
            f"| 적격 {s.eligible} 부적격 {s.ineligible} 확인필요 {s.needs_info}"
        )
        if judge_ms > 50:
            raise SystemExit(f"판정이 {judge_ms:.1f}ms — 인메모리 설계 전제가 깨졌습니다")


def bench_solver() -> None:
    rnd = random.Random(7)

    # G4 게이트: 정점 <=12 케이스 20건을 완전탐색과 대조. 기준은 100% 일치.
    agree = 0
    for _ in range(20):
        weights, adj = random_graph(rnd.randint(6, 12), 0.35, rnd)
        agree += mwis(weights, adj)[0] == brute_force(weights, adj)
    print(f"[ADR-003] 솔버 정확성 (정점<=12, 20건 완전탐색 대조) : {agree}/20 일치")
    if agree != 20:
        raise SystemExit("솔버가 정확해를 내지 못했습니다 — G4 게이트 미달")

    # 실사용 규모: 적격 정책 30개
    weights, adj = random_graph(30, 0.25, rnd)
    start = time.perf_counter()
    for _ in range(100):
        mwis(weights, adj)
    print(f"[ADR-003] MWIS 정점 30개 정확해 : {(time.perf_counter() - start) * 10:.3f} ms/요청")

    # 최악 가정: 적격 64개 (실사용상 발생하지 않는 수준)
    weights, adj = random_graph(64, 0.30, rnd)
    start = time.perf_counter()
    mwis(weights, adj)
    print(f"[ADR-003] MWIS 정점 64개(최악) 정확해 : {(time.perf_counter() - start) * 1000:.1f} ms")


def bench_planner() -> None:
    """일정 역산 (BE-M5). 목표 p95 는 50ms (docs/ARCHITECTURE.md §5).

    달력 산술을 하루씩 훑는 구현으로 두면 3,000건에서 300ms 가 나온다.
    영업일 색인이 살아 있는지 이 벤치가 지킨다.
    """
    rnd = random.Random(11)
    profile = _sample_profile()
    today = date(2026, 9, 14)
    cal = bundled_calendar()

    # 달력 자체의 산술 비용 (색인이 깨지면 여기가 먼저 터진다)
    t = time.perf_counter()
    for _ in range(10_000):
        cal.subtract_business_days(date(2026, 10, 30), 4)
    calendar_us = (time.perf_counter() - t) / 10_000 * 1_000_000
    print(f"[BE-M5] 영업일 역산 1회 : {calendar_us:6.2f} us")
    if calendar_us > 50:
        raise SystemExit(f"영업일 산술이 {calendar_us:.0f}us — 색인 경로가 깨졌습니다")

    for n in SCALES:
        policies = _sample_policies(n, rnd)
        for policy in policies:
            policy.period = Period(
                apply_end=rnd.choice(["2026-10-30", "2026-12-01", "2026-09-18", None]),
                is_rolling=rnd.random() < 0.2,
            )
            policy.benefit = Benefit(type="cash_lump", amount_krw=10**6, estimated_total_krw=10**6)
            policy.documents = [
                Document(
                    name=f"서류{j}",
                    doc_code=f"DOC{j % 20:02d}",
                    lead_time_business_days=rnd.choice([0, 1, 3, None]),
                    cost_krw=rnd.choice([0, 400, 1000]),
                )
                for j in range(rnd.randint(0, 4))
            ]

        snapshot = compile_snapshot(policies, version="bench")
        verdicts = judge_all(snapshot, profile, today)

        build_plan(snapshot, verdicts, today)  # 워밍업
        t = time.perf_counter()
        for _ in range(10):
            plan = build_plan(snapshot, verdicts, today)
        plan_ms = (time.perf_counter() - t) / 10 * 1000

        print(
            f"[BE-M5] 정책 {n:5d}건 (적격 {plan.summary.total:4d}) : "
            f"계획 {plan_ms:6.2f} ms  | 서류 {len(plan.documents):2d}종 "
            f"급함 {plan.summary.urgent} 불가 {plan.summary.infeasible} "
            f"마감미상 {plan.summary.unknown_deadline}"
        )
        if plan_ms > 50:
            raise SystemExit(f"계획 생성이 {plan_ms:.1f}ms — 목표 p95(50ms)를 넘었습니다")


if __name__ == "__main__":
    force_utf8_console()
    bench_rules()
    bench_solver()
    bench_planner()
