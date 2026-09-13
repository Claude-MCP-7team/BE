"""상충 그래프 빌더 — 공고문의 중복수혜 조항을 간선으로 바꾼다.

AI 역할은 공고문에서 '관계를 추출'하는 데까지만 한다 (마일스톤 §0-1).
추출된 서술을 어떤 정책 쌍의 간선으로 확정할지는 여기서 결정론적으로 정한다.

간선 생성 규칙 (PRD §7.4)
  explicit_policy   상대 정책명이 명시됨              → CONFIRMED
  category_overlap  같은 분야 + 같은 급부 형태         → ESTIMATED
  same_authority    같은 발주기관의 타 사업            → ESTIMATED

'유사사업' 정의가 공고문마다 없다는 것이 이 프로젝트의 핵심 난제다(R3).
그래서 ESTIMATED 간선을 강제로 적용하지도, 무시하지도 않는다. 양쪽 시나리오를
모두 계산해 사용자에게 보여주고 판단 근거(원문 + 담당부서 연락처)를 함께 준다.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.schemas.policy import PolicySchema


@dataclass(frozen=True, slots=True)
class Edge:
    """두 정책을 동시에 받을 수 없다는 관계. 항상 a < b 로 정규화한다."""

    a: str
    b: str
    confidence: str  # CONFIRMED | ESTIMATED
    conflict_type: str
    source_quote: str
    source_policy_id: str  # 이 조항이 실린 공고
    source_url: str | None = None

    @staticmethod
    def make(left: str, right: str, **kw) -> Edge:
        lo, hi = (left, right) if left < right else (right, left)
        return Edge(a=lo, b=hi, **kw)

    def other(self, policy_id: str) -> str:
        return self.b if policy_id == self.a else self.a


def build_edges(policies: list[PolicySchema]) -> list[Edge]:
    """정책 목록에서 상충 간선을 만든다. 같은 쌍은 가장 강한 근거 하나만 남긴다."""
    by_id = {p.policy_id: p for p in policies}
    best: dict[tuple[str, str], Edge] = {}

    for policy in policies:
        for conflict in policy.conflicts:
            for edge in _edges_from(policy, conflict, by_id):
                key = (edge.a, edge.b)
                current = best.get(key)
                # CONFIRMED 가 ESTIMATED 를 이긴다. 같은 쌍에 명시 조항과 포괄 조항이
                # 함께 걸리면, 명시 쪽이 사용자에게 보여줄 근거로 더 정확하다.
                if current is None or _rank(edge) > _rank(current):
                    best[key] = edge

    return sorted(best.values(), key=lambda e: (e.a, e.b))


def _rank(edge: Edge) -> int:
    return 1 if edge.confidence == "CONFIRMED" else 0


def _edges_from(policy: PolicySchema, conflict, by_id: dict[str, PolicySchema]) -> list[Edge]:
    kind = conflict.type
    common = {
        "conflict_type": kind,
        "source_quote": conflict.source_quote,
        "source_policy_id": policy.policy_id,
        "source_url": conflict.source_url or policy.source.origin_url,
    }

    if kind == "explicit_policy":
        # 명시된 정책이 우리 범위 안에 있을 때만 간선이 된다.
        # 범위 밖 정책명은 사용자에게 안내할 수는 있어도 계산에는 넣을 수 없다.
        return [
            Edge.make(policy.policy_id, target, confidence="CONFIRMED", **common)
            for target in conflict.target_policy_ids
            if target in by_id and target != policy.policy_id
        ]

    if kind == "category_overlap":
        return [
            Edge.make(policy.policy_id, other.policy_id, confidence="ESTIMATED", **common)
            for other in by_id.values()
            if other.policy_id != policy.policy_id
            and other.meta.category == conflict.target_category
            and _benefit_matches(other, conflict.target_benefit_type)
        ]

    if kind == "same_authority":
        mine = _authority_key(policy)
        return [
            Edge.make(policy.policy_id, other.policy_id, confidence="ESTIMATED", **common)
            for other in by_id.values()
            if other.policy_id != policy.policy_id and _authority_key(other) == mine
        ]

    return []


def _benefit_matches(policy: PolicySchema, wanted: str | None) -> bool:
    """급부 형태가 지정되지 않은 조항은 분야만으로 본다 (조항이 더 포괄적이라는 뜻)."""
    return wanted is None or policy.benefit.type == wanted


def _authority_key(policy: PolicySchema) -> tuple[str, str]:
    """같은 부서인지 판단하는 키. 부서명이 없으면 기관 수준으로 떨어뜨린다."""
    return (policy.meta.authority_level, policy.meta.dept.name or "")


def adjacency(policy_ids: list[str], edges: list[Edge]) -> list[int]:
    """정점 순서에 맞춘 인접 비트마스크. 솔버 입력 형식."""
    index = {pid: i for i, pid in enumerate(policy_ids)}
    adj = [0] * len(policy_ids)
    for edge in edges:
        i, j = index.get(edge.a), index.get(edge.b)
        if i is None or j is None:
            continue  # 적격이 아닌 정책은 그래프에 없다
        adj[i] |= 1 << j
        adj[j] |= 1 << i
    return adj
