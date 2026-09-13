"""아키텍처 성능 가설 검증 벤치마크.

docs/ARCHITECTURE.md 의 ADR-001/002/003 이 주장하는 수치를 실제로 측정한다.
CI 에서 매번 돌려 성능 회귀를 감시한다 (숫자가 무너지면 설계 전제가 무너진 것).

  ADR-001  스냅샷을 RAM 에 올려도 되는가       → 메모리 실측
  ADR-002  룰 평가를 벡터화하면 충분히 빠른가  → p50 실측
  ADR-003  PuLP/CBC 없이 정확해를 낼 수 있는가 → 완전탐색 대조 + 지연 실측

사용법:  python bench/engine_bench.py
"""

from __future__ import annotations

import random
import time
from collections.abc import Iterator

import numpy as np

N_POLICIES = 3000  # 전국 확장 시나리오 최대치 (1차 범위는 150~600)
SENTINEL = np.iinfo(np.int16).min  # "해당 조건 없음"(무제약) 표시


# --- ADR-002: 벡터화 룰 평가 ------------------------------------------------


class CompiledRules:
    """정책을 '필드별 열 배열'로 뒤집어 둔 것 = 스냅샷의 핵심 자료구조.

    행(정책)마다 파이썬 루프를 도는 대신, 필드마다 numpy 연산 한 번으로
    전체 정책을 동시에 평가한다.
    """

    def __init__(self, n: int, rng: np.random.Generator) -> None:
        self.age_min = rng.integers(18, 30, n).astype(np.int16)
        self.age_max = rng.integers(30, 40, n).astype(np.int16)
        self.res_min = self._maybe(rng, n, 0.6, 0, 13)
        self.inc_max = self._maybe(rng, n, 0.7, 80, 201)
        self.emp_mask = rng.integers(1, 32, n).astype(np.int8)  # 취업상태 5종 비트마스크
        # 중앙부처 정책은 전국(0) 대상이라 실제 분포에서 큰 비중을 차지한다.
        # 지역 매칭률을 현실보다 낮게 잡으면 적격 건수가 0에 수렴해
        # NEEDS_INFO 경로가 한 번도 실행되지 않는 벤치마크가 된다.
        self.region = np.where(
            rng.random(n) < 0.30, 0, rng.integers(1, 300, n)
        ).astype(np.int32)
        # 사용자 답변이 있어야 판정 가능한 정책 (역질문 대상)
        self.needs_answer = rng.random(n) < 0.25

    @staticmethod
    def _maybe(
        rng: np.random.Generator, n: int, ratio: float, lo: int, hi: int
    ) -> np.ndarray:
        """비율만큼만 조건이 있고 나머지는 무제약인 열을 만든다."""
        return np.where(
            rng.random(n) < ratio, rng.integers(lo, hi, n), SENTINEL
        ).astype(np.int16)

    def nbytes(self) -> int:
        return sum(
            c.nbytes
            for c in (
                self.age_min,
                self.age_max,
                self.res_min,
                self.inc_max,
                self.emp_mask,
                self.region,
                self.needs_answer,
            )
        )

    def judge(
        self,
        age: int,
        res_months: int,
        income_ratio: int,
        emp_bit: int,
        region_chain: np.ndarray,
        answered: np.ndarray,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """전 정책을 동시에 평가해 (적격, 확인필요, 부적격) 마스크를 돌려준다."""
        ok = (self.age_min <= age) & (age <= self.age_max)

        has = self.res_min != SENTINEL
        ok &= ~has | (self.res_min <= res_months)

        has = self.inc_max != SENTINEL
        ok &= ~has | (income_ratio <= self.inc_max)

        ok &= (self.emp_mask & emp_bit) != 0
        ok &= np.isin(self.region, region_chain)

        # 다른 조건은 통과했는데 정보만 부족한 정책 → NEEDS_INFO
        unknown = self.needs_answer & ~answered & ok
        return ok & ~unknown, unknown, ~ok


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
    rng = np.random.default_rng(7)
    rules = CompiledRules(N_POLICIES, rng)
    region_chain = np.array([0, 41, 165], dtype=np.int32)
    answered = np.zeros(N_POLICIES, dtype=bool)
    args = (25, 4, 120, 2, region_chain, answered)

    rules.judge(*args)  # 워밍업
    start = time.perf_counter()
    for _ in range(1000):
        eligible, unknown, bad = rules.judge(*args)
    elapsed_ms = (time.perf_counter() - start)

    print(
        f"[ADR-002] 룰 평가 {N_POLICIES}개 정책 x 5필드 : {elapsed_ms:.3f} ms/요청  "
        f"(적격 {eligible.sum()} / 확인필요 {unknown.sum()} / 부적격 {bad.sum()})"
    )
    print(
        f"[ADR-001] 룰 열배열 메모리({N_POLICIES}개 정책, 7필드) : "
        f"{rules.nbytes() / 1024:.1f} KB"
    )


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


if __name__ == "__main__":
    bench_rules()
    bench_solver()
