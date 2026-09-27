"""enums.py 와 DB CHECK 제약의 값 집합이 일치하는지 검사한다.

한쪽만 바꾸면 코드는 멀쩡히 돌다가 배치의 INSERT 단계에서 죽는다.
그 실패는 야간 배치에서 터지므로 발견이 늦다. 여기서 미리 잡는다.
"""

import pathlib
import re

import pytest

from app.schemas import enums

MIGRATION = pathlib.Path(__file__).resolve().parents[2] / "db" / "migrations" / "0001_init.sql"
SQL = MIGRATION.read_text(encoding="utf-8")


def check_values(column: str) -> set[str]:
    """`column IN ('a','b')` 형태의 CHECK 제약에서 허용 값 집합을 뽑는다."""
    m = re.search(rf"CHECK\s*\(\s*{re.escape(column)}\s+IN\s*\(([^)]*)\)", SQL)
    if not m:
        pytest.fail(f"{column} 의 CHECK 제약을 {MIGRATION.name} 에서 찾지 못했습니다")
    return set(re.findall(r"'([^']*)'", m.group(1)))


def literal_values(tp) -> set[str]:
    return set(tp.__args__)


@pytest.mark.parametrize(
    "column,literal",
    [
        ("status", enums.PolicyStatus),
        ("category", enums.Category),
        ("authority_level", enums.AuthorityLevel),
        ("benefit_type", enums.BenefitType),
        ("budget_exhaust_risk", enums.BudgetRisk),
        ("kind", enums.RuleKind),
        ("op", enums.Operator),
        ("confidence", enums.Confidence),
        ("cross_check", enums.CrossCheck),
        ("conflict_type", enums.ConflictType),
        ("issue_channel", enums.IssueChannel),
        ("expected_verdict", enums.Verdict),
    ],
)
def test_DB_CHECK_와_리터럴_타입이_일치한다(column, literal):
    assert check_values(column) == literal_values(literal), (
        f"'{column}' 의 허용 값이 코드와 DB에서 다릅니다. "
        "app/schemas/enums.py 와 db/migrations/0001_init.sql 을 함께 고치세요"
    )


def test_source_quote_는_DB에서도_NOT_NULL_이다():
    """G1 게이트를 밸리데이터만 지키면, DB 직접 INSERT 경로로 우회된다."""
    assert "source_quote      TEXT NOT NULL" in SQL
    assert "policy_rule_quote_not_blank" in SQL


def test_상충_간선은_DB에서_정규화가_강제된다():
    assert "CONSTRAINT edge_normalized CHECK (policy_a < policy_b)" in SQL


def test_활성_스냅샷은_DB에서_유일성이_강제된다():
    assert "snapshot_active_uniq ON snapshot (is_active) WHERE is_active" in SQL


def test_개인정보_평문_컬럼이_없다():
    """조건값을 담는 평문 컬럼이 생기면 §8.3 최소수집 원칙이 무너진다."""
    session_ddl = SQL[SQL.index("CREATE TABLE user_session") : SQL.index("CREATE INDEX user_session_expiry_idx")]
    for forbidden in ("birth_date", "income", "education", "household", "address", "name", "tel"):
        assert forbidden not in session_ddl, (
            f"user_session 에 평문 '{forbidden}' 컬럼이 생겼습니다. "
            "조건값은 profile_ct(암호문)에만 들어가야 합니다"
        )


def test_세션_기본_보관기간은_90일이다():
    assert "INTERVAL '90 days'" in SQL
