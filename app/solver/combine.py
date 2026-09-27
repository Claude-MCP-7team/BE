"""조합 추천 — 적격 정책들에서 보수/최대 두 시나리오의 상위 조합을 만든다.

두 시나리오를 모두 내놓는 이유 (PRD §7.4, R3)
  공고문은 "동일 목적의 타 사업과 중복 수혜 불가"처럼 쓰지만 '동일 목적'이
  무엇인지 정의하지 않는다. 이걸 시스템이 한쪽으로 단정하면 둘 중 하나가 된다:
    지키는 쪽으로 단정 → 받을 수 있었던 정책을 놓치게 한다
    무시하는 쪽으로 단정 → 신청했다가 반려되게 한다
  어느 쪽도 사용자가 감당할 손해다. 그래서 계산은 둘 다 하고, 판단 근거
  (원문 구절 + 담당부서 연락처)를 붙여 사용자가 확인하게 한다.
"""

from __future__ import annotations

import statistics

from app.engine.compile import Snapshot
from app.engine.evaluate import Verdicts
from app.schemas.combination import (
    Combination,
    CombinationMember,
    CombinationResponse,
    ExcludedPolicy,
    Scenario,
)
from app.schemas.judgement import DISCLAIMER
from app.schemas.policy import PolicySchema
from app.solver.graph import Edge, adjacency, build_edges
from app.solver.mwis import MAX_EXACT_VERTICES, solve

TOP_N = 3

# 수혜액이 명시되지 않은 정책의 임시 가중치 규칙 (미결 Q2, 기한 11/02).
#   0 으로 두면 상충이 생길 때 항상 지고, 중앙값을 주면 실제보다 과대평가된다.
#   둘 다 틀릴 수 있으므로 '알려진 금액들의 하위 사분위'를 쓴다 — 과소평가 쪽으로
#   기울여, 금액을 모르는 정책 때문에 아는 정책이 밀려나지 않게 한다.
#   어떤 조합이든 추정치가 섞이면 total_is_estimated 로 표시한다.
UNKNOWN_AMOUNT_QUANTILE = 0.25


def recommend(
    snapshot: Snapshot, verdicts: Verdicts, top_n: int = TOP_N
) -> CombinationResponse:
    eligible = [
        snapshot.policies[i] for i in range(snapshot.size) if verdicts.eligible[i]
    ]
    policy_ids = [p.policy_id for p in eligible]
    weights, estimated_flags = _weights(eligible)

    # 상충 조항은 적격 여부와 무관하게 전 정책에서 추출하고, 그래프에는 적격만 남긴다
    all_edges = build_edges(snapshot.policies)
    eligible_set = set(policy_ids)
    edges = [e for e in all_edges if e.a in eligible_set and e.b in eligible_set]

    scenarios = [
        _scenario(
            kind="conservative",
            label="보수 조합",
            description="추정 상충까지 모두 피한 조합입니다. 반려 위험이 가장 낮습니다.",
            eligible=eligible,
            policy_ids=policy_ids,
            weights=weights,
            estimated_flags=estimated_flags,
            edges=edges,  # CONFIRMED + ESTIMATED 전부 적용
            top_n=top_n,
        ),
        _scenario(
            kind="maximal",
            label="최대 조합",
            description=(
                "공고문에 명시된 중복 제한만 반영한 조합입니다. "
                "추정 상충은 담당부서 확인이 필요합니다."
            ),
            eligible=eligible,
            policy_ids=policy_ids,
            weights=weights,
            estimated_flags=estimated_flags,
            edges=[e for e in edges if e.confidence == "CONFIRMED"],
            top_n=top_n,
        ),
    ]

    return CombinationResponse(
        snapshot_version=snapshot.version,
        eligible_count=len(eligible),
        scenarios=scenarios,
        disclaimer=DISCLAIMER,
    )


def _weights(policies: list[PolicySchema]) -> tuple[list[int], list[bool]]:
    """정점 가중치와 '추정치를 썼는지' 플래그."""
    known = [
        p.benefit.estimated_total_krw
        for p in policies
        if p.benefit.estimated_total_krw is not None
    ]
    fallback = _low_quantile(known)

    weights: list[int] = []
    flags: list[bool] = []
    for policy in policies:
        amount = policy.benefit.estimated_total_krw
        weights.append(amount if amount is not None else fallback)
        # 금액이 없어서 대체값을 쓴 경우와, 금액은 있지만 확정이 아닌 경우
        # 둘 다 '확정 아님'이다. 후자를 빼면 A2 가 월액×개월로 계산한 총액이나
        # 본문에서 추정한 금액이 화면에서 공고에 적힌 확정 금액과 똑같이 보인다.
        flags.append(amount is None or policy.benefit.amount_confidence != "CONFIRMED")
    return weights, flags


def _low_quantile(values: list[int]) -> int:
    """알려진 금액들의 하위 사분위. 표본이 적으면 최솟값으로 떨어뜨린다."""
    if not values:
        return 0
    if len(values) < 4:
        return min(values)
    ordered = sorted(values)
    return int(statistics.quantiles(ordered, n=4)[0])


def _scenario(
    *,
    kind: str,
    label: str,
    description: str,
    eligible: list[PolicySchema],
    policy_ids: list[str],
    weights: list[int],
    estimated_flags: list[bool],
    edges: list[Edge],
    top_n: int,
) -> Scenario:
    adj = adjacency(policy_ids, edges)
    solutions = solve(weights, adj, k=top_n)

    by_id = {p.policy_id: p for p in eligible}
    combinations = [
        _to_combination(
            rank=rank,
            chosen=set(solution.indices()),
            total=solution.weight,
            policy_ids=policy_ids,
            by_id=by_id,
            weights=weights,
            estimated_flags=estimated_flags,
            edges=edges,
        )
        for rank, solution in enumerate(solutions, start=1)
    ]

    return Scenario(
        kind=kind,
        label=label,
        description=description,
        combinations=combinations,
        approximate=len(policy_ids) > MAX_EXACT_VERTICES,
    )


def _to_combination(
    *,
    rank: int,
    chosen: set[int],
    total: int,
    policy_ids: list[str],
    by_id: dict[str, PolicySchema],
    weights: list[int],
    estimated_flags: list[bool],
    edges: list[Edge],
) -> Combination:
    chosen_ids = {policy_ids[i] for i in chosen}

    members = [
        CombinationMember(
            policy_id=policy_ids[i],
            title=by_id[policy_ids[i]].meta.title,
            estimated_total_krw=weights[i],
            amount_estimated=estimated_flags[i],
        )
        for i in sorted(chosen, key=lambda i: -weights[i])
    ]

    return Combination(
        rank=rank,
        total_krw=total,
        members=members,
        excluded=_excluded(chosen_ids, policy_ids, by_id, weights, estimated_flags, edges),
        total_is_estimated=any(estimated_flags[i] for i in chosen),
    )


def _excluded(
    chosen_ids: set[str],
    policy_ids: list[str],
    by_id: dict[str, PolicySchema],
    weights: list[int],
    estimated_flags: list[bool],
    edges: list[Edge],
) -> list[ExcludedPolicy]:
    """빠진 정책마다 '무엇 때문에 빠졌는지'를 원문 근거와 함께 붙인다.

    한 정책이 여러 선택 정책과 충돌할 수 있다. 그중 CONFIRMED 근거를 우선해
    보여준다 — 사용자가 담당부서에 물을 때 가장 확실한 문구가 필요하기 때문이다.
    """
    out: list[ExcludedPolicy] = []
    index = {pid: i for i, pid in enumerate(policy_ids)}

    for policy_id in policy_ids:
        if policy_id in chosen_ids:
            continue

        blocking = [
            e for e in edges if policy_id in (e.a, e.b) and e.other(policy_id) in chosen_ids
        ]
        if not blocking:
            continue  # 상충이 아니라 다른 이유로 빠진 경우는 표시하지 않는다

        edge = max(blocking, key=lambda e: e.confidence == "CONFIRMED")
        winner = edge.other(policy_id)
        policy = by_id[policy_id]
        i = index[policy_id]

        out.append(
            ExcludedPolicy(
                policy_id=policy_id,
                title=policy.meta.title,
                estimated_total_krw=weights[i],
                conflicts_with=winner,
                conflicts_with_title=by_id[winner].meta.title,
                confidence=edge.confidence,
                conflict_type=edge.conflict_type,
                source_quote=edge.source_quote,
                source_url=edge.source_url,
                dept_name=policy.meta.dept.name,
                dept_tel=policy.meta.dept.tel,
            )
        )

    return sorted(out, key=lambda x: -x.estimated_total_krw)
