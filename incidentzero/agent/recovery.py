from __future__ import annotations

import re
import time
from collections.abc import Callable
from typing import TypeVar

from incidentzero.model.errors import TransientModelError

T = TypeVar("T")

_TRY_AGAIN = re.compile(r"try again in\s*((?:\d+(?:\.\d+)?\s*(?:ms|h|m|s)\s*)+)", re.IGNORECASE)
_RETRY_AFTER = re.compile(r"retry[- ]after[\"']?\s*[:=]?\s*(\d+(?:\.\d+)?)", re.IGNORECASE)
_DURATION_PART = re.compile(r"(\d+(?:\.\d+)?)\s*(ms|h|m|s)", re.IGNORECASE)
_UNIT_SECONDS = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}


def retry_after_seconds(error: BaseException | None) -> float | None:
    """Server-suggested wait before retrying, if the provider gave one.

    Uses an explicit numeric ``retry_after`` attribute (the Groq adapter copies the
    Retry-After header onto the error) or a hint inside the message, e.g. Groq's
    "Please try again in 2.5s" / "try again in 1m3.2s" / "try again in 450ms".
    """
    if error is None:
        return None
    hint = getattr(error, "retry_after", None)
    if isinstance(hint, (int, float)) and not isinstance(hint, bool) and hint >= 0:
        return float(hint)
    text = str(error)
    match = _TRY_AGAIN.search(text)
    if match:
        return sum(float(v) * _UNIT_SECONDS[u.lower()] for v, u in _DURATION_PART.findall(match.group(1)))
    match = _RETRY_AFTER.search(text)
    if match:
        return float(match.group(1))
    return None


class RetryPolicy:
    """Bounded retry for ``TransientModelError`` only (R5).

    * ``max_attempts`` counts attempts, not retries: 3 means one call plus at most two retries
      (``configs/limits.json: max_consecutive_model_retries``).
    * Waits grow exponentially (``base_delay * multiplier**(n-1)``) and are capped at
      ``max_delay``. A provider Retry-After hint may lengthen a wait, never beyond the cap.
    * When ``time_remaining`` is supplied and the next wait would overrun the runtime budget,
      the last transient error is re-raised instead of sleeping.
    * ``PermanentModelError``, schema/validation problems, budget errors and every other
      exception propagate on the first occurrence; they are never retried here, and this
      class never executes tools, so an unsafe tool action cannot be retried through it.

    The caller charges the LLM budget inside ``fn`` so that every attempt is counted.
    """

    def __init__(
        self,
        max_attempts: int = 3,
        sleeper: Callable[[float], None] = time.sleep,
        *,
        base_delay: float = 0.5,
        multiplier: float = 2.0,
        max_delay: float = 8.0,
        time_remaining: Callable[[], float] | None = None,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        self.max_attempts = max_attempts
        self.sleeper = sleeper
        self.base_delay = base_delay
        self.multiplier = multiplier
        self.max_delay = max_delay
        self.time_remaining = time_remaining
        self.attempts_used = 0  # attempts made by the most recent call_model()

    def backoff_delay(self, failed_attempt: int, error: BaseException | None = None) -> float:
        delay = self.base_delay * (self.multiplier ** max(0, failed_attempt - 1))
        hint = retry_after_seconds(error)
        if hint is not None:
            delay = max(delay, hint)
        return min(delay, self.max_delay)

    def call_model(self, fn: Callable[[], T]) -> T:
        attempt = 0
        while True:
            attempt += 1
            self.attempts_used = attempt
            try:
                return fn()
            except TransientModelError as exc:
                if attempt >= self.max_attempts:
                    raise
                delay = self.backoff_delay(attempt, exc)
                if self.time_remaining is not None and self.time_remaining() <= delay:
                    raise
                self.sleeper(delay)
