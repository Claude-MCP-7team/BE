"""스키마 계약면.

  PolicySchema     AI → BE  (계약면 C1, G1 10/02 Freeze)
  UserProfile      FE → BE
  JudgementResult  BE → FE  (계약면 C2, 10/07 확정)
"""

from app.schemas.judgement import (
    DISCLAIMER,
    JudgementResponse,
    JudgementResult,
    JudgementSummary,
    MatchedRule,
    UnknownRule,
    UnmatchedRule,
)
from app.schemas.policy import (
    Benefit,
    Conflict,
    Dept,
    Document,
    Meta,
    Period,
    PolicySchema,
    Quality,
    Rule,
    Source,
)
from app.schemas.user import Consent, Core, History, UserProfile, region_chain
from app.schemas.validate import (
    SchemaValidationError,
    SchemaViolation,
    assert_valid_policy,
    validate_judgement,
    validate_policy,
)

__all__ = [
    "DISCLAIMER",
    "Benefit",
    "Conflict",
    "Consent",
    "Core",
    "Dept",
    "Document",
    "History",
    "JudgementResponse",
    "JudgementResult",
    "JudgementSummary",
    "MatchedRule",
    "Meta",
    "Period",
    "PolicySchema",
    "Quality",
    "Rule",
    "SchemaValidationError",
    "SchemaViolation",
    "Source",
    "UnknownRule",
    "UnmatchedRule",
    "UserProfile",
    "assert_valid_policy",
    "region_chain",
    "validate_judgement",
    "validate_policy",
]
