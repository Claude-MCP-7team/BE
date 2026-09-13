"""조합 솔버 테스트 (US-04 / G4 게이트).

G4 기준은 확률값이 아니라 **완전탐색과 100% 일치**다. 조합 계산은 결정론이므로
'대체로 맞음'이라는 상태가 존재할 수 없다.
"""

import random
from datetime import date

from app.engine.compile import compile_snapshot
from app.engine.evaluate import judge_all
from app.schemas.policy import Benefit, Conflict, Dept, Meta, PolicySchema, Rule, Source
from app.schemas.user import Core, UserProfile
from app.solver.combine import recommend
from app.solver.graph import adjacency, build_edges
from app.solver.mwis import brute_force, solve

TODAY = date(2026, 9, 13)


def random_graph(n, density, rnd):
    weights = [rnd.randint(1, 40) * 100_000 for _ in range(n)]
    adj = [0] * n
    for i in range(n):
        for j in range(i + 1, n):
            if rnd.random() < density:
                adj[i] |= 1 << j
                adj[j] |= 1 << i
    return weights, adj


# --- 🔴 G4: 완전탐색과 100% 일치 ---------------------------------------------


def test_솔버가_완전탐색과_상위3개까지_완전히_일치한다():
    rnd = random.Random(20260913)
    for _ in range(200):
        n = rnd.randint(3, 13)
        weights, adj = random_graph(n, rnd.choice([0.15, 0.35, 0.6]), rnd)
        got = [s.weight for s in solve(weights, adj, k=3)]
        want = [s.weight for s in brute_force(weights, adj, k=3)]
        assert got == want, f"n={n} weights={weights} adj={adj}"


def test_최적해의_구성원까지_일치한다():
    rnd = random.Random(7)
    for _ in range(50):
        n = rnd.randint(4, 11)
        weights, adj = random_graph(n, 0.4, rnd)
        got = solve(weights, adj, k=1)[0]
        want = brute_force(weights, adj, k=1)[0]
        assert got.weight == want.weight
        assert sum(weights[i] for i in got.indices()) == want.weight


def test_결과는_항상_독립집합이다():
    """상충하는 두 정책이 같은 조합에 들어가면 사용자가 반려당한다."""
    rnd = random.Random(11)
    for _ in range(100):
        n = rnd.randint(3, 14)
        weights, adj = random_graph(n, 0.45, rnd)
        for solution in solve(weights, adj, k=3):
            chosen = solution.indices()
            for i in chosen:
                assert adj[i] & solution.members == 0


def test_부분집합은_다음_순위로_올라오지_않는다():
    """1등에서 하나 뺀 것이 2등이면 사용자에게 보여줄 값이 없다."""
    weights = [10, 20, 30]
    adj = [0, 0, 0]
    assert len(solve(weights, adj, k=3)) == 1  # 극대집합이 하나뿐


def test_상충이_전혀_없으면_전부_받는다():
    weights = [100, 200, 300]
    best = solve(weights, [0, 0, 0], k=1)[0]
    assert best.weight == 600
    assert sorted(best.indices()) == [0, 1, 2]


def test_모두_서로_상충하면_가장_큰_것_하나():
    weights = [100, 500, 300]
    adj = [0b110, 0b101, 0b011]
    solutions = solve(weights, adj, k=3)
    assert solutions[0].weight == 500
    assert [s.weight for s in solutions] == [500, 300, 100]


def test_정점이_없으면_빈_결과():
    assert solve([], [], k=3) == []


# --- 상충 그래프 ------------------------------------------------------------


def P(policy_id, *, category="housing", benefit_type="cash_monthly", total=1_000_000,
      authority="local", dept="청년정책과", conflicts=(), rules=None):
    return PolicySchema(
        policy_id=policy_id,
        status="published",
        meta=Meta(title=f"{policy_id} 정책", category=category, authority_level=authority,
                  region_code=["41465"], dept=Dept(name=dept, tel="031-000-0000")),
        source=Source(origin_url=f"https://example.kr/{policy_id}"),
        benefit=Benefit(type=benefit_type, estimated_total_krw=total),
        eligibility=list(rules or [Rule(rule_id="AGE", field="age", op="between",
                                        value=[19, 34], source_quote="만 19~34세")]),
        conflicts=list(conflicts),
    )


def test_명시된_정책명은_CONFIRMED_간선이_된다():
    policies = [
        P("A", conflicts=[Conflict(type="explicit_policy", target_policy_ids=["B"],
                                   source_quote="B 사업과 중복 수혜 불가")]),
        P("B"),
    ]
    edges = build_edges(policies)
    assert len(edges) == 1
    assert (edges[0].a, edges[0].b) == ("A", "B")
    assert edges[0].confidence == "CONFIRMED"
    assert edges[0].source_quote == "B 사업과 중복 수혜 불가"


def test_범위_밖_정책명은_간선이_되지_않는다():
    """우리가 모르는 정책은 계산에 넣을 수 없다."""
    policies = [P("A", conflicts=[Conflict(type="explicit_policy",
                                           target_policy_ids=["범위밖-사업"],
                                           source_quote="타 사업과 중복 불가")])]
    assert build_edges(policies) == []


def test_동일_분야_포괄_배제는_ESTIMATED_간선이_된다():
    policies = [
        P("A", conflicts=[Conflict(type="category_overlap", target_category="housing",
                                   target_benefit_type="cash_monthly",
                                   source_quote="동일 목적의 타 주거지원사업과 중복 불가")]),
        P("B", category="housing", benefit_type="cash_monthly"),
        P("C", category="job", benefit_type="cash_lump"),
    ]
    edges = build_edges(policies)
    assert {(e.a, e.b) for e in edges} == {("A", "B")}
    assert edges[0].confidence == "ESTIMATED"


def test_같은_쌍에_명시와_포괄이_겹치면_명시가_이긴다():
    """담당부서에 물을 때 필요한 건 가장 확실한 문구다."""
    policies = [
        P("A", conflicts=[
            Conflict(type="explicit_policy", target_policy_ids=["B"], source_quote="B와 중복 불가"),
            Conflict(type="category_overlap", target_category="housing",
                     target_benefit_type="cash_monthly", source_quote="유사사업 중복 불가"),
        ]),
        P("B"),
    ]
    edges = build_edges(policies)
    assert len(edges) == 1
    assert edges[0].confidence == "CONFIRMED"
    assert edges[0].source_quote == "B와 중복 불가"


def test_간선은_방향에_관계없이_같은_쌍으로_정규화된다():
    policies = [
        P("B", conflicts=[Conflict(type="explicit_policy", target_policy_ids=["A"],
                                   source_quote="A와 중복 불가")]),
        P("A"),
    ]
    edges = build_edges(policies)
    assert (edges[0].a, edges[0].b) == ("A", "B")


def test_인접행렬은_적격_정책만_담는다():
    policies = [
        P("A", conflicts=[Conflict(type="explicit_policy", target_policy_ids=["B"],
                                   source_quote="중복 불가")]),
        P("B"),
    ]
    edges = build_edges(policies)
    adj = adjacency(["A"], edges)  # B 는 적격이 아니라 그래프에 없다
    assert adj == [0]


# --- 보수 / 최대 2안 ---------------------------------------------------------


def eligible_user():
    return UserProfile(core=Core(birth_date=date(2001, 3, 14), region_code="41465"))


def recommend_for(policies):
    snap = compile_snapshot(policies)
    user = eligible_user()
    return recommend(snap, judge_all(snap, user, TODAY))


def test_두_시나리오가_모두_제시된다():
    r = recommend_for([P("A", total=1_000_000), P("B", total=2_000_000)])
    assert [s.kind for s in r.scenarios] == ["conservative", "maximal"]


def test_추정_상충은_보수에서만_적용된다():
    """'유사사업' 정의가 없어서 한쪽으로 단정하지 않는다 (R3)."""
    policies = [
        P("A", total=1_000_000,
          conflicts=[Conflict(type="category_overlap", target_category="housing",
                              target_benefit_type="cash_monthly",
                              source_quote="동일 목적 타 사업과 중복 불가")]),
        P("B", total=2_000_000),
    ]
    r = recommend_for(policies)
    conservative, maximal = r.scenarios

    assert conservative.combinations[0].total_krw == 2_000_000  # 하나만
    assert maximal.combinations[0].total_krw == 3_000_000  # 둘 다


def test_명시_상충은_최대_조합에서도_지켜진다():
    policies = [
        P("A", total=1_000_000,
          conflicts=[Conflict(type="explicit_policy", target_policy_ids=["B"],
                              source_quote="B와 중복 수혜 불가")]),
        P("B", total=2_000_000),
    ]
    for scenario in recommend_for(policies).scenarios:
        assert scenario.combinations[0].total_krw == 2_000_000


def test_배제된_정책에_사유와_원문_근거가_붙는다():
    policies = [
        P("A", total=1_000_000,
          conflicts=[Conflict(type="explicit_policy", target_policy_ids=["B"],
                              source_quote="B 사업과 중복 수혜 불가")]),
        P("B", total=2_000_000),
    ]
    top = recommend_for(policies).scenarios[0].combinations[0]
    assert [m.policy_id for m in top.members] == ["B"]

    excluded = top.excluded
    assert len(excluded) == 1
    assert excluded[0].policy_id == "A"
    assert excluded[0].conflicts_with == "B"
    assert excluded[0].source_quote == "B 사업과 중복 수혜 불가"
    assert excluded[0].source_url
    assert excluded[0].dept_tel  # 담당부서 확인 경로


def test_추정_배제에는_담당부서_연락처가_반드시_붙는다():
    """ESTIMATED 는 시스템이 단정할 수 없으므로 확인 경로가 있어야 한다."""
    policies = [
        P("A", total=1_000_000,
          conflicts=[Conflict(type="category_overlap", target_category="housing",
                              target_benefit_type="cash_monthly",
                              source_quote="동일 목적 중복 불가")]),
        P("B", total=2_000_000),
    ]
    conservative = recommend_for(policies).scenarios[0]
    excluded = conservative.combinations[0].excluded
    assert excluded[0].confidence == "ESTIMATED"
    assert excluded[0].dept_tel


def test_상위_3개까지_제시된다():
    policies = [P(f"P{i}", total=(i + 1) * 100_000) for i in range(4)]
    policies[0] = P("P0", total=100_000,
                    conflicts=[Conflict(type="explicit_policy", target_policy_ids=["P1", "P2"],
                                        source_quote="중복 불가")])
    combos = recommend_for(policies).scenarios[1].combinations
    assert 1 <= len(combos) <= 3
    assert [c.rank for c in combos] == list(range(1, len(combos) + 1))
    assert combos == sorted(combos, key=lambda c: -c.total_krw)


# --- 수혜액 미명시 처리 (Q2 임시 규칙) ---------------------------------------


def test_금액_미명시_정책은_추정치로_표시된다():
    policies = [
        P("KNOWN1", total=1_000_000), P("KNOWN2", total=2_000_000),
        P("KNOWN3", total=3_000_000), P("KNOWN4", total=4_000_000),
        P("UNKNOWN", total=None),
    ]
    combo = recommend_for(policies).scenarios[0].combinations[0]
    unknown = next(m for m in combo.members if m.policy_id == "UNKNOWN")
    assert unknown.amount_estimated is True
    assert combo.total_is_estimated is True


def test_금액을_모르는_정책도_상충이_없으면_포함된다():
    """모른다는 이유로 받을 수 있는 정책을 떨어뜨리면 안 된다."""
    policies = [P("KNOWN", total=5_000_000), P("UNKNOWN", total=None)]
    members = {m.policy_id for m in recommend_for(policies).scenarios[0].combinations[0].members}
    assert members == {"KNOWN", "UNKNOWN"}


def test_금액_미명시_정책이_아는_정책을_밀어내지_않는다():
    """상충할 때는 금액이 확인된 쪽을 택한다 — 과대평가가 더 위험하다."""
    policies = [
        P("KNOWN1", total=1_000_000), P("KNOWN2", total=2_000_000),
        P("KNOWN3", total=3_000_000), P("KNOWN4", total=4_000_000),
        P("UNKNOWN", total=None,
          conflicts=[Conflict(type="explicit_policy", target_policy_ids=["KNOWN4"],
                              source_quote="중복 불가")]),
    ]
    top = recommend_for(policies).scenarios[0].combinations[0]
    assert "KNOWN4" in {m.policy_id for m in top.members}
    assert "UNKNOWN" not in {m.policy_id for m in top.members}


def test_적격이_없으면_빈_시나리오():
    policies = [P("A", rules=[Rule(rule_id="AGE", field="age", op="between",
                                   value=[40, 50], source_quote="만 40~50세")])]
    r = recommend_for(policies)
    assert r.eligible_count == 0
    for scenario in r.scenarios:
        assert scenario.combinations == []


def test_면책_고지가_붙는다():
    assert "법적 효력이 없습니다" in recommend_for([P("A")]).disclaimer
