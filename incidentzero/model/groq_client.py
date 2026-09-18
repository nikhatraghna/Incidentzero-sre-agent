from __future__ import annotations

import json
import os
from typing import Any

from groq import Groq

from incidentzero.domain.models import ModelReply, ToolCall
from .errors import PermanentModelError, TransientModelError

TRANSIENT_STATUS = {408, 409, 429, 500, 502, 503, 504}
_CONNECTION_ERROR_NAMES = {"APIConnectionError", "APITimeoutError"}


def _error_details(exc: Exception) -> dict[str, Any]:
    """Best-effort extraction of the provider's error object (shape differs across SDK versions)."""
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        inner = body.get("error", body)
        if isinstance(inner, dict):
            return inner
    response = getattr(exc, "response", None)
    if response is not None:
        try:
            data = response.json()
            inner = data.get("error", data) if isinstance(data, dict) else None
            if isinstance(inner, dict):
                return inner
        except Exception:
            pass
    return {}


def _retry_after_header(exc: Exception) -> float | None:
    headers = getattr(getattr(exc, "response", None), "headers", None)
    try:
        value = headers.get("retry-after") if headers is not None else None
        return float(value) if value is not None else None
    except (AttributeError, TypeError, ValueError):
        return None


def _is_connection_error(exc: Exception) -> bool:
    return (any(cls.__name__ in _CONNECTION_ERROR_NAMES for cls in type(exc).__mro__)
            or isinstance(exc, (TimeoutError, ConnectionError)))


class GroqModelClient:
    def __init__(self, api_key: str | None = None, model: str = "openai/gpt-oss-20b", *, timeout: float = 20.0) -> None:
        self.model = model
        # max_retries=0: the SDK would otherwise retry silently, sending requests the assignment's
        # LLM budget never sees. Every retry is made (and counted) by the controller's RetryPolicy.
        # The timeout bounds a hung request so the runtime budget stays meaningful.
        self.client = Groq(api_key=api_key or os.getenv("GROQ_API_KEY"), max_retries=0, timeout=timeout)

    def _translate_error(self, exc: Exception) -> Exception:
        status = getattr(exc, "status_code", None)
        details = _error_details(exc)
        if status in TRANSIENT_STATUS:
            err = TransientModelError(str(exc))
            err.kind, err.status_code, err.retry_after = "provider", status, _retry_after_header(exc)
            return err
        if status is None and _is_connection_error(exc):
            err = TransientModelError(f"network error: {exc}")
            err.kind = "network"  # a connection blip is retryable, not a configuration error
            return err
        if status == 400 and details.get("code") == "json_validate_failed":
            err = TransientModelError(f"structured output failed schema validation: {str(exc)[:200]}")
            err.kind = "invalid_output"
            return err
        err = PermanentModelError(str(exc))
        err.status_code = status
        return err

    def decide(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ModelReply:
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                tools=tools,
                tool_choice="auto",
                parallel_tool_calls=False,
                temperature=0.1,
                reasoning_effort="low",
            )
        except Exception as exc:  # provider-specific classes intentionally kept out of student code
            details = _error_details(exc)
            if getattr(exc, "status_code", None) == 400 and details.get("code") == "tool_use_failed":
                # Groq validates tool calls server-side and rejects malformed ones with HTTP 400.
                # That is invalid model output, not a configuration error: hand it to the
                # controller as a turn without an executable call so it can give corrective feedback.
                failed = str(details.get("failed_generation") or details.get("message") or exc)[:500]
                return ModelReply(content=f"[provider rejected an invalid tool call] {failed}", tool_calls=[],
                                  usage={}, finish_reason="tool_use_failed")
            raise self._translate_error(exc) from exc
        msg = response.choices[0].message
        calls: list[ToolCall] = []
        for call in (msg.tool_calls or []):
            try:
                args = json.loads(call.function.arguments)
            except Exception:
                args = {"__malformed_arguments__": call.function.arguments}
            calls.append(ToolCall(id=call.id, name=call.function.name, arguments=args))
        usage = {}
        if getattr(response, "usage", None):
            usage = {
                "prompt_tokens": getattr(response.usage, "prompt_tokens", 0) or 0,
                "completion_tokens": getattr(response.usage, "completion_tokens", 0) or 0,
                "total_tokens": getattr(response.usage, "total_tokens", 0) or 0,
            }
        return ModelReply(
            content=msg.content,
            tool_calls=calls,
            usage=usage,
            finish_reason=response.choices[0].finish_reason,
        )

    def structured(self, messages: list[dict[str, Any]], schema_name: str, schema: dict[str, Any]) -> dict[str, Any]:
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                response_format={
                    "type": "json_schema",
                    "json_schema": {"name": schema_name, "strict": True, "schema": schema},
                },
                temperature=0.1,
                reasoning_effort="low",
            )
        except Exception as exc:
            raise self._translate_error(exc) from exc
        content = response.choices[0].message.content or "{}"
        try:
            return json.loads(content)
        except json.JSONDecodeError as exc:
            err = TransientModelError(f"Model returned invalid structured JSON: {content[:160]}")
            err.kind = "invalid_output"
            raise err from exc
