"""Google Gemini provider adapter (google-genai SDK)."""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from typing import Any

_logger = logging.getLogger(__name__)

try:
    from google import genai
    from google.genai import errors as genai_errors
    from google.genai import types
except ImportError as exc:
    raise ImportError(
        "google-genai package not installed. "
        "Run: uv pip install 'sf-dev-agent[gemini]'"
    ) from exc

from sf_dev_agent.providers.base import LLMProvider, LLMResponse, ToolCall

DEFAULT_MODEL = "gemini-2.5-flash"


class GeminiProvider(LLMProvider):
    def __init__(self, model: str = DEFAULT_MODEL, api_key: str | None = None) -> None:
        self._model = model
        self._api_key = api_key  # None → SDK reads GOOGLE_API_KEY at first chat() call
        self._client: genai.Client | None = None  # created lazily

    def _get_client(self) -> genai.Client:
        if self._client is None:
            self._client = genai.Client(api_key=self._api_key)
        return self._client

    @property
    def model_name(self) -> str:
        return self._model

    def chat(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_tokens: int = 16384,
    ) -> LLMResponse:
        gemini_tools = (
            [types.Tool(function_declarations=[
                types.FunctionDeclaration(
                    name=t["name"],
                    description=t["description"],
                    parameters=t["parameters"],
                )
                for t in tools
            ])]
            if tools else None
        )

        config = types.GenerateContentConfig(
            system_instruction=system,
            tools=gemini_tools,
            max_output_tokens=max_tokens,
        )

        # Retry up to 3 times on transient 429s, honoring the API's retryDelay.
        max_attempts = 4
        for attempt in range(max_attempts):
            try:
                response = self._get_client().models.generate_content(
                    model=self._model,
                    contents=self._convert_messages(messages),
                    config=config,
                )
                break
            except genai_errors.ClientError as exc:
                is_429 = "429" in str(exc) or getattr(exc, "code", None) == 429
                is_zero_quota = "limit: 0" in str(exc)

                if is_429 and is_zero_quota:
                    raise RuntimeError(
                        f"Model '{self._model}' has no free-tier quota on this API key. "
                        "Enable billing at console.cloud.google.com or try a different model."
                    ) from exc

                # Daily project quota exhausted — retrying won't help until tomorrow.
                is_daily_limit = "PerDay" in str(exc) or "per_day" in str(exc).lower()
                if is_429 and is_daily_limit:
                    raise RuntimeError(
                        f"Daily free-tier quota exhausted for '{self._model}' on this Google AI project. "
                        "Options: (1) create a NEW Google AI Studio project with a fresh key, "
                        "(2) enable billing at aistudio.google.com, "
                        "(3) run with --mock-org to test the agent loop without LLM calls."
                    ) from exc

                if is_429 and attempt < max_attempts - 1:
                    retry_after = self._parse_retry_delay(exc)
                    _logger.warning(
                        "Gemini 429 rate limit (attempt %d/%d) — retrying in %ds",
                        attempt + 1, max_attempts, retry_after,
                    )
                    time.sleep(retry_after)
                    continue

                raise

        text_blocks: list[str] = []
        tool_calls: list[ToolCall] = []

        for candidate in response.candidates:
            for part in candidate.content.parts:
                if part.text:
                    text_blocks.append(part.text)
                elif part.function_call and part.function_call.name:
                    fc = part.function_call
                    input_data = dict(fc.args) if fc.args else {}
                    tool_calls.append(ToolCall(
                        id=f"gemini_{uuid.uuid4().hex[:12]}",
                        name=fc.name,
                        input=input_data,
                    ))

        return LLMResponse(
            text_blocks=text_blocks,
            tool_calls=tool_calls,
            stop_reason="tool_use" if tool_calls else "end_turn",
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_retry_delay(exc: Exception, default: int = 65) -> int:
        """Extract retryDelay from a Gemini 429 error response, plus a 5s buffer."""
        m = re.search(r'"retryDelay":\s*"(\d+)s"', str(exc))
        return int(m.group(1)) + 5 if m else default

    def _convert_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Convert internal Anthropic-like messages to Gemini content format.

        Gemini uses {"role": "user"|"model", "parts": [...]} with function_call
        and function_response parts for tool interactions.
        """
        # Pre-build tool_use_id → tool_name for function_response lookup.
        tool_id_to_name: dict[str, str] = {}
        for msg in messages:
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if block.get("type") == "tool_use":
                    tool_id_to_name[block["id"]] = block["name"]

        result: list[dict[str, Any]] = []

        for msg in messages:
            role = msg["role"]
            content = msg["content"]
            gemini_role = "model" if role == "assistant" else "user"

            if isinstance(content, str):
                result.append({"role": gemini_role, "parts": [{"text": content}]})
                continue

            parts: list[dict[str, Any]] = []
            for block in content:
                btype = block.get("type")
                if btype == "text":
                    parts.append({"text": block["text"]})
                elif btype == "tool_use":
                    parts.append({
                        "function_call": {
                            "name": block["name"],
                            "args": block["input"],
                        }
                    })
                elif btype == "tool_result":
                    tool_use_id = block["tool_use_id"]
                    fn_name = tool_id_to_name.get(tool_use_id, "unknown_function")
                    raw = block.get("content", "")
                    try:
                        response_data = json.loads(raw) if isinstance(raw, str) else raw
                    except (json.JSONDecodeError, TypeError):
                        response_data = {"result": str(raw)}
                    parts.append({
                        "function_response": {
                            "name": fn_name,
                            "response": response_data,
                        }
                    })

            if parts:
                result.append({"role": gemini_role, "parts": parts})

        return result
