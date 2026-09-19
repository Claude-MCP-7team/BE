"""A1→A2→검증 오케스트레이션 (AI 역할).

공고문 자유 텍스트를 PolicySchema 로 옮기는 단계. 흐름은 한 방향이다:

  텍스트 조립(text.py) → LLM 구조화(structure.py) → 인용문 원문 대조 → 병합 → validate_policy

원칙: LLM 이 낸 것 중 원문에서 글자 그대로 찾을 수 없는 인용문이 붙은 항목은 통째로
버린다. 근거 없는 룰은 DB 제약(NOT NULL source_quote)이 아니라 여기서 먼저 걸러야
"왜 버려졌는지"가 리포트에 남는다.
"""
