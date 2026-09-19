"""LLM 호출 래퍼 — 구조화 출력 1회 호출을 하나의 함수로 감싼다.

호출하는 쪽(batch/agents)은 '시스템 프롬프트 + 사용자 텍스트 + JSON 스키마' 만 넘기고
dict 를 돌려받는다. SDK 종류·모델명·재시도·거부 처리는 여기서만 다룬다.

anthropic 패키지는 함수 안에서 import 한다. API 서버는 이 모듈을 실행하지 않고,
배치만 실행하므로 서버 이미지에 SDK 를 넣지 않아도 부팅이 깨지지 않게 하기 위해서다.

테스트는 `LLM` 프로토콜을 만족하는 가짜 객체를 넘긴다. 네트워크 없이 파이프라인 전체를
검증할 수 있어야 검증 로직(인용문 대조·병합) 자체를 테스트할 수 있다.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol

if TYPE_CHECKING:
    from anthropic.types import MessageParam, OutputConfigParam, TextBlockParam

_PROMPT_DIR = Path(__file__).parent / "prompts"

Effort = Literal["low", "medium", "high", "xhigh", "max"]

DEFAULT_MODEL = "claude-opus-5"
# 구조화 출력은 정책 1건당 수 KB 라 non-streaming 으로 충분하다. 상한만 넉넉히 둔다.
DEFAULT_MAX_TOKENS = 16000


class LLMError(RuntimeError):
    """모델이 유효한 구조화 응답을 내지 못했다 (거부·토큰 상한·JSON 파싱 실패)."""


class LLM(Protocol):
    def complete_json(self, *, system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
        """스키마를 만족하는 JSON 객체 1개를 돌려준다."""
        ...


def load_prompt(name: str) -> str:
    """prompts/<name>.md 를 읽는다. 프롬프트는 코드가 아니라 문서로 관리한다."""
    return (_PROMPT_DIR / f"{name}.md").read_text(encoding="utf-8")


class AnthropicLLM:
    """Claude API 구조화 출력 호출.

    시스템 프롬프트에 cache_control 을 붙인다. 배치가 같은 프롬프트로 수백 건을
    연속 호출하므로 프롬프트 토큰 대부분이 캐시에서 읽힌다.
    """

    def __init__(
        self,
        *,
        model: str | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        effort: Effort = "high",
    ) -> None:
        import anthropic

        self._anthropic = anthropic
        # API 키는 ANTHROPIC_API_KEY 환경변수 또는 `ant auth login` 프로필에서 SDK 가 읽는다.
        self._client = anthropic.Anthropic()
        self.model = model or os.environ.get("YPC_LLM_MODEL", DEFAULT_MODEL)
        self.max_tokens = max_tokens
        self.effort = effort
        self.last_usage: dict[str, int] = {}

    def complete_json(self, *, system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
        system_blocks: list[TextBlockParam] = [
            {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
        ]
        messages: list[MessageParam] = [{"role": "user", "content": user}]
        output_config: OutputConfigParam = {
            "effort": self.effort,
            "format": {"type": "json_schema", "schema": schema},
        }
        response = self._client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=system_blocks,
            messages=messages,
            output_config=output_config,
        )
        self.last_usage = {
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
            "cache_read_input_tokens": response.usage.cache_read_input_tokens or 0,
        }

        if response.stop_reason == "refusal":
            raise LLMError(f"모델이 응답을 거부했습니다 (request_id={response._request_id})")
        if response.stop_reason == "max_tokens":
            raise LLMError(f"출력이 max_tokens={self.max_tokens} 에서 잘렸습니다")

        text = next((b.text for b in response.content if b.type == "text"), None)
        if text is None:
            raise LLMError("응답에 text 블록이 없습니다")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise LLMError(f"JSON 파싱 실패: {e}") from e
        if not isinstance(data, dict):
            raise LLMError(f"JSON 객체가 아닙니다: {type(data).__name__}")
        return data
