"""역질문 큐 스키마 (US-03, S3 화면).

'이 답변으로 N개 정책 판정이 완료됩니다'라는 문구가 화면에 나가는데,
그 N 이 부정확하면 사용자를 속이는 것이 된다. 그래서 두 숫자를 구분한다.

  resolves  이 필드가 '마지막 남은 미확인'인 정책 수 → 답하면 판정이 끝난다
  affects   이 필드를 참조하는 정책 수 → 답해도 다른 미확인이 남아 있을 수 있다

화면 문구에는 resolves 만 쓴다. affects 를 쓰면 미확인이 2개인 정책까지 세어
"5개가 완료됩니다" 해놓고 답하면 3개만 끝나는 일이 생긴다.
"""

from __future__ import annotations

import msgspec


class Question(msgspec.Struct, kw_only=True, forbid_unknown_fields=True):
    field: str
    text: str

    resolves: int = 0
    affects: int = 0

    # 답을 고르는 형태. 예/아니오 또는 선택지로 제한한다 (PRD §7.3)
    answer_type: str = "boolean"  # boolean | number | choice
    choices: list[str] = msgspec.field(default_factory=list)

    # 근거 — 이 질문이 어느 공고 문구에서 나왔는지
    source_quote: str = ""
    source_policy_ids: list[str] = msgspec.field(default_factory=list)


class QuestionQueue(msgspec.Struct, kw_only=True, forbid_unknown_fields=True):
    snapshot_version: str
    questions: list[Question]

    # 큐에 담지 못한 나머지. 상한(10개) 때문에 잘린 게 있는지 알려준다.
    total_unresolved_fields: int = 0
    needs_info_policies: int = 0
