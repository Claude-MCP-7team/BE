"""공유 리터럴 타입.

DB CHECK 제약(db/migrations/0001_init.sql)과 값 집합이 1:1로 일치해야 한다.
한쪽만 바꾸면 배치가 INSERT 단계에서 죽는다. 변경 시 반드시 양쪽을 함께 수정할 것.
"""

from typing import Literal

# --- 정책 -------------------------------------------------------------------
PolicyStatus = Literal["draft", "published", "unpublished", "expired"]
Category = Literal["job", "housing", "education", "welfare", "participation"]
AuthorityLevel = Literal["central", "province", "local"]
BenefitType = Literal["cash_lump", "cash_monthly", "loan", "voucher", "service"]
BudgetRisk = Literal["low", "medium", "high"]

# --- 룰 ---------------------------------------------------------------------
RuleKind = Literal["eligibility", "exclusion"]
Operator = Literal[
    "==", "!=", ">", ">=", "<", "<=", "between", "in", "not_in", "contains", "exists"
]

# --- 신뢰도 · 판정 ----------------------------------------------------------
Confidence = Literal["CONFIRMED", "ESTIMATED", "NEEDS_REVIEW"]
EdgeConfidence = Literal["CONFIRMED", "ESTIMATED"]
Verdict = Literal["ELIGIBLE", "INELIGIBLE", "NEEDS_INFO"]
CrossCheck = Literal["AGREE", "DISAGREE", "SKIPPED"]

# --- 상충 -------------------------------------------------------------------
ConflictType = Literal["explicit_policy", "category_overlap", "same_authority"]

# --- 사용자 -----------------------------------------------------------------
EmploymentStatus = Literal["employed", "job_seeking", "student", "founder", "neet"]
Education = Literal[
    "middle_or_below",
    "high_school_enrolled",
    "high_school_graduated",
    "university_enrolled",
    "university_graduated",
    "graduate_school",
]
MaritalStatus = Literal["single", "married", "divorced", "widowed"]
IncomeBasis = Literal["self_only", "household_incl_parents", "household_excl_parents"]

# --- 서류 -------------------------------------------------------------------
IssueChannel = Literal["online", "offline", "both"]

# --- 룰이 참조할 수 있는 사용자 필드 ----------------------------------------
# 룰 엔진이 평가할 수 있는 필드의 전체 집합. 여기 없는 field 는 컴파일 거부된다.
# (알 수 없는 필드를 조용히 통과시키면 "부적격인데 적격으로 판정"이 발생한다)
KNOWN_FIELDS: frozenset[str] = frozenset(
    {
        "age",
        "region_code",
        "residence_months_continuous",
        "education",
        "employment_status",
        "employment_months",
        "marital_status",
        "household_size",
        "household_income_ratio_median",
        "received_policy_ids",
        "similar_program_participation_2y",
    }
)

# 시간 경과만으로 충족될 수 있는 필드 → 충족 예상일 계산 대상 (PRD §7.2)
TIME_SATISFIABLE_FIELDS: frozenset[str] = frozenset(
    {"age", "residence_months_continuous", "employment_months"}
)
