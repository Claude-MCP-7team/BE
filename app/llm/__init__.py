"""🟡 C 레이어 — AI 역할과의 경계면.

prompts/ 는 AI 역할이 소유한다. 결정론 레이어(engine/solver/planner)는 이 패키지를
import 할 수 없다 (ruff banned-api). 배치와 API 계층만 호출한다.
"""
