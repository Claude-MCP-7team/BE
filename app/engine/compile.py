"""PolicySchema 목록 → 컴파일된 스냅샷.

설계 요지: 필드별로 특수 처리를 만들지 않는다.
  필드가 11개인데 필드마다 전용 배열과 전용 비교 코드를 두면, 분기가 늘어난
  만큼 rules.py 의 기준 의미와 어긋날 자리가 생긴다. 대신 **연산자를 4가지
  형태로 일반화**해, 어떤 필드가 오더라도 같은 코드가 처리하게 한다.

    Cmp      >  >=  <  <=          숫자 임계값 비교
    Between  between                숫자 범위
    Member   in  not_in  contains  ==  !=    집합 교집합 여부
    Exists   exists                  값의 유무

  `in` 과 `contains` 는 방향만 다를 뿐 "사용자 값 집합과 룰 값 집합이 겹치는가"로
  같다. `==` 는 원소가 1개인 집합이다. 그래서 셋을 한 형태로 묶는다.

평가는 룰을 '정책별'이 아니라 '(필드, 연산자)별'로 모아서 한다. 그러면 그룹
하나당 numpy 연산 한 번으로 전체 정책이 동시에 평가된다.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as dc_field

import numpy as np

from app.schemas.enums import KNOWN_FIELDS, LIST_VALUED_FIELDS
from app.schemas.policy import PolicySchema, Rule

# 집합 교집합으로 환원되는 연산자
MEMBER_OPS = frozenset({"in", "not_in", "contains", "==", "!="})
CMP_OPS = frozenset({">", ">=", "<", "<="})


class CompileError(ValueError):
    pass


@dataclass(slots=True)
class RuleRef:
    """원본 룰로 되돌아가기 위한 참조. 설명문을 만들 때 쓴다."""

    policy_index: int
    rule: Rule
    kind: str  # 'eligibility' | 'exclusion'


@dataclass(slots=True)
class CmpGroup:
    """`user_value <op> threshold` 형태."""

    field: str
    op: str
    policy_idx: np.ndarray  # int32[k]
    rule_ref: np.ndarray  # int32[k] → RuleRef 인덱스
    threshold: np.ndarray  # float64[k]


@dataclass(slots=True)
class BetweenGroup:
    field: str
    policy_idx: np.ndarray
    rule_ref: np.ndarray
    lo: np.ndarray  # float64[k]
    hi: np.ndarray


@dataclass(slots=True)
class MemberGroup:
    """사용자 값 집합과 룰 값 집합이 겹치는지 본다.

    `member` 는 역색인이다. 값 하나가 어느 룰들에 등장하는지 미리 계산해 두면,
    평가 시에는 사용자가 가진 값의 개수(보통 1~3개)만큼만 OR 하면 된다.
    """

    field: str
    op: str
    policy_idx: np.ndarray
    rule_ref: np.ndarray
    member: dict[object, np.ndarray]  # 값 → bool[k]
    negate: bool  # not_in / != 는 결과를 뒤집는다


@dataclass(slots=True)
class ExistsGroup:
    field: str
    policy_idx: np.ndarray
    rule_ref: np.ndarray
    want: np.ndarray  # bool[k]


@dataclass(slots=True)
class Snapshot:
    """실시간 판정이 읽는 유일한 자료구조. DB 를 대신한다."""

    version: str
    policy_ids: list[str]
    policies: list[PolicySchema]
    rule_refs: list[RuleRef]

    cmp_groups: list[CmpGroup] = dc_field(default_factory=list)
    between_groups: list[BetweenGroup] = dc_field(default_factory=list)
    member_groups: list[MemberGroup] = dc_field(default_factory=list)
    exists_groups: list[ExistsGroup] = dc_field(default_factory=list)

    # 정책별 룰 참조 인덱스. 상세 화면에서 한 정책의 룰만 훑을 때 쓴다.
    rules_by_policy: list[list[int]] = dc_field(default_factory=list)

    @property
    def size(self) -> int:
        return len(self.policy_ids)

    def index_of(self, policy_id: str) -> int:
        return self.policy_ids.index(policy_id)

    def fields_used(self) -> set[str]:
        return {r.rule.field for r in self.rule_refs}


def compile_snapshot(policies: list[PolicySchema], version: str = "dev") -> Snapshot:
    """정책 목록을 평가 가능한 스냅샷으로 컴파일한다.

    잘못된 룰은 여기서 예외로 터진다. 배치에서 걸러내는 편이,
    사용자 요청 중에 조용히 틀린 답을 내는 것보다 낫다.
    """
    snap = Snapshot(
        version=version,
        policy_ids=[p.policy_id for p in policies],
        policies=list(policies),
        rule_refs=[],
        rules_by_policy=[[] for _ in policies],
    )

    # (필드, 연산자) → [(룰참조 인덱스, 룰)]
    buckets: dict[tuple[str, str], list[int]] = {}

    # kind 는 기록만 하고 평가에는 쓰지 않는다. 두 배열의 룰은 모두 '충족되어야
    # 적격'이며, 제외조항은 생산자가 부정형으로 뒤집어 넣는다 (PolicySchema 도크스트링).
    # 여기서 exclusion 을 자동으로 부정하면, 이미 부정형으로 들어온 룰이 두 번
    # 뒤집혀 조용히 반대로 판정된다.
    for pi, policy in enumerate(policies):
        for kind, rules in (("eligibility", policy.eligibility), ("exclusion", policy.exclusions)):
            for rule in rules:
                _check(rule, policy.policy_id)
                ref_index = len(snap.rule_refs)
                snap.rule_refs.append(RuleRef(policy_index=pi, rule=rule, kind=kind))
                snap.rules_by_policy[pi].append(ref_index)
                buckets.setdefault((rule.field, rule.op), []).append(ref_index)

    for (fld, op), ref_indices in buckets.items():
        _build_group(snap, fld, op, ref_indices)

    return snap


def _check(rule: Rule, policy_id: str) -> None:
    if rule.field not in KNOWN_FIELDS:
        raise CompileError(
            f"[{policy_id}/{rule.rule_id}] 룰 엔진이 모르는 필드입니다: {rule.field!r}"
        )
    if rule.op in CMP_OPS and not _is_number(rule.value):
        raise CompileError(
            f"[{policy_id}/{rule.rule_id}] {rule.op} 의 기준값이 숫자가 아닙니다: {rule.value!r}"
        )
    if rule.op == "between" and not (isinstance(rule.value, list) and len(rule.value) == 2):
        raise CompileError(f"[{policy_id}/{rule.rule_id}] between 은 [하한, 상한] 이어야 합니다")
    if rule.op in ("in", "not_in") and not isinstance(rule.value, list):
        raise CompileError(f"[{policy_id}/{rule.rule_id}] {rule.op} 의 기준값은 목록이어야 합니다")
    if rule.field in LIST_VALUED_FIELDS and rule.op in ("==", "!="):
        raise CompileError(
            f"[{policy_id}/{rule.rule_id}] '{rule.field}' 는 사용자 값이 목록이라 "
            f"{rule.op} 의 의미가 모호합니다. in / not_in / contains 를 쓰세요"
        )


def _is_number(v: object) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _build_group(snap: Snapshot, fld: str, op: str, ref_indices: list[int]) -> None:
    refs = [snap.rule_refs[i] for i in ref_indices]
    policy_idx = np.array([r.policy_index for r in refs], dtype=np.int32)
    rule_ref = np.array(ref_indices, dtype=np.int32)

    if op in CMP_OPS:
        snap.cmp_groups.append(
            CmpGroup(
                field=fld,
                op=op,
                policy_idx=policy_idx,
                rule_ref=rule_ref,
                threshold=np.array([float(r.rule.value) for r in refs], dtype=np.float64),  # type: ignore[arg-type]
            )
        )
        return

    if op == "between":
        bounds = np.array([r.rule.value for r in refs], dtype=np.float64)
        snap.between_groups.append(
            BetweenGroup(
                field=fld,
                policy_idx=policy_idx,
                rule_ref=rule_ref,
                lo=bounds[:, 0],
                hi=bounds[:, 1],
            )
        )
        return

    if op == "exists":
        snap.exists_groups.append(
            ExistsGroup(
                field=fld,
                policy_idx=policy_idx,
                rule_ref=rule_ref,
                want=np.array([bool(r.rule.value) for r in refs], dtype=bool),
            )
        )
        return

    if op in MEMBER_OPS:
        member: dict[object, np.ndarray] = {}
        for pos, ref in enumerate(refs):
            for value in _rule_value_set(ref.rule):
                arr = member.get(value)
                if arr is None:
                    arr = np.zeros(len(refs), dtype=bool)
                    member[value] = arr
                arr[pos] = True
        snap.member_groups.append(
            MemberGroup(
                field=fld,
                op=op,
                policy_idx=policy_idx,
                rule_ref=rule_ref,
                member=member,
                negate=op in ("not_in", "!="),
            )
        )
        return

    raise CompileError(f"컴파일할 수 없는 연산자입니다: {op}")


def _rule_value_set(rule: Rule) -> list[object]:
    """룰이 허용(또는 배제)하는 값들. 교집합 판정의 한쪽 집합이 된다."""
    if rule.op in ("in", "not_in"):
        return list(rule.value)  # type: ignore[arg-type]
    # contains / == / != 는 원소가 1개인 집합이다
    return [rule.value]
