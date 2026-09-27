"""최대 가중 독립집합 (MWIS) — 조합 최적화의 계산 엔진.

⚠️ 이 계산을 LLM 에게 맡기지 않는다 (PRD §7.4). 제약조건 하 최대화 문제이고,
   답이 '그럴듯한지'가 아니라 '맞는지'로 판정되기 때문이다.
   G4 게이트 기준은 92% 같은 확률값이 아니라 완전탐색과 **100% 일치**다.

PuLP/CBC 를 쓰지 않는 이유는 ADR-003 에 있다. 정점이 64개 이하면 인접관계를
파이썬 정수 하나(비트마스크)로 표현할 수 있고, 집합 연산이 전부 CPU 비트연산이
되어 분기한정만으로 정확해가 나온다. CBC 바이너리는 ARM/슬림 이미지에서 자주
깨지는데, 무료 티어에서 그건 실질적인 장애 위험이다.

상위 k개를 뽑을 때 **극대(maximal) 집합만** 센다. 그러지 않으면 2등·3등이
1등에서 정책을 하나씩 뺀 부분집합으로 채워져, 사용자에게 보여줄 값이 없다.
"""

from __future__ import annotations

import heapq
from collections.abc import Iterator
from dataclasses import dataclass

# 비트마스크 하나로 표현 가능한 정점 수. 넘으면 근사로 폴백한다.
MAX_EXACT_VERTICES = 64


@dataclass(frozen=True, slots=True)
class Solution:
    """독립집합 하나. members 는 정점 인덱스 집합의 비트마스크."""

    weight: int
    members: int

    def indices(self) -> list[int]:
        return list(iter_bits(self.members))

    def size(self) -> int:
        return bin(self.members).count("1")


def iter_bits(mask: int) -> Iterator[int]:
    while mask:
        low = mask & -mask
        yield low.bit_length() - 1
        mask ^= low


def solve(weights: list[int], adj: list[int], k: int = 1) -> list[Solution]:
    """가중치 합이 큰 순서로 극대 독립집합 상위 k개를 돌려준다.

    adj[i] 는 i 와 동시 수혜가 불가능한 정점들의 비트마스크다.
    정확해이며, 정점 수가 MAX_EXACT_VERTICES 를 넘으면 근사로 폴백한다.
    """
    n = len(weights)
    if n == 0:
        return []
    if len(adj) != n:
        raise ValueError("weights 와 adj 의 길이가 다릅니다")
    if n > MAX_EXACT_VERTICES:
        return _greedy(weights, adj, k)

    full = (1 << n) - 1
    # 상위 k개를 담는 최소 힙. 루트가 'k번째로 좋은 답'이라 가지치기 기준이 된다.
    heap: list[tuple[int, int]] = []
    seen: set[int] = set()

    def record(weight: int, members: int) -> None:
        if members in seen:
            return
        if len(heap) < k:
            seen.add(members)
            heapq.heappush(heap, (weight, members))
        elif weight > heap[0][0]:
            seen.discard(heap[0][1])
            seen.add(members)
            heapq.heapreplace(heap, (weight, members))

    def cutoff() -> int:
        """이 값 이하로는 상위 k에 들 수 없다."""
        return heap[0][0] if len(heap) == k else -1

    def recurse(cand: int, cur_weight: int, cur_set: int) -> None:
        # 상계: 남은 후보를 전부 담아도 k번째를 못 넘으면 이 가지는 볼 필요가 없다
        if cur_weight + sum(weights[i] for i in iter_bits(cand)) <= cutoff():
            return

        if cand == 0:
            # 극대인 집합만 기록한다. 더 넣을 수 있는데 멈춘 집합은
            # 1등의 부분집합일 뿐이라 사용자에게 보여줄 의미가 없다.
            if _is_maximal(cur_set, adj, full):
                record(cur_weight, cur_set)
            return

        v = (cand & -cand).bit_length() - 1
        # v 를 넣는다 → v 와 상충하는 정점은 후보에서 빠진다
        recurse(cand & ~(1 << v) & ~adj[v], cur_weight + weights[v], cur_set | (1 << v))
        # v 를 뺀다
        recurse(cand & ~(1 << v), cur_weight, cur_set)

    recurse(full, 0, 0)
    return [Solution(weight=w, members=m) for w, m in sorted(heap, reverse=True)]


def _is_maximal(members: int, adj: list[int], full: int) -> bool:
    """집합 밖에 '넣어도 되는' 정점이 남아 있으면 극대가 아니다."""
    outside = full & ~members
    return all(adj[v] & members != 0 for v in iter_bits(outside))


def _greedy(weights: list[int], adj: list[int], k: int) -> list[Solution]:
    """정점이 너무 많을 때의 근사해.

    실사용에서 적격 정책이 64개를 넘는 경우는 관측되지 않았지만, 넘었을 때
    답을 못 주는 것보다는 근사라도 주고 근사임을 알리는 편이 낫다.
    호출부가 approximate 플래그를 붙여 사용자에게 표시한다.
    """
    order = sorted(range(len(weights)), key=lambda i: -weights[i])
    out: list[Solution] = []

    for start in range(min(k, len(order))):
        members = 0
        weight = 0
        blocked = 0
        # 매번 다른 정점을 먼저 넣어 서로 다른 조합을 만든다
        for i in [*order[start:], *order[:start]]:
            if (1 << i) & blocked:
                continue
            members |= 1 << i
            weight += weights[i]
            blocked |= adj[i] | (1 << i)
        candidate = Solution(weight=weight, members=members)
        if candidate.members not in {s.members for s in out}:
            out.append(candidate)

    return sorted(out, key=lambda s: -s.weight)[:k]


def brute_force(weights: list[int], adj: list[int], k: int = 1) -> list[Solution]:
    """완전탐색. 솔버를 검증하기 위한 기준선이며 운영 경로에서는 쓰지 않는다."""
    n = len(weights)
    full = (1 << n) - 1
    found: list[Solution] = []

    for subset in range(1 << n):
        if any(adj[i] & subset for i in iter_bits(subset)):
            continue  # 독립집합이 아니다
        if not _is_maximal(subset, adj, full):
            continue
        found.append(Solution(weight=sum(weights[i] for i in iter_bits(subset)), members=subset))

    return sorted(found, key=lambda s: (-s.weight, s.members))[:k]
