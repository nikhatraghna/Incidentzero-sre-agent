from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


class BudgetExceeded(RuntimeError):
    pass


class RuntimeBudgetExceeded(BudgetExceeded):
    """The wall-clock runtime budget (``max_runtime_seconds``) is spent."""


@dataclass
class BudgetManager:
    """Hard call budgets. Every model request (including retries and planning requests) and
    every simulator tool execution must be charged here *before* it happens.

    ``max_runtime_seconds`` is optional so existing ``BudgetManager(max_llm_calls=..,
    max_tool_calls=..)`` call sites keep working; when it is ``None`` the controller takes
    the runtime limit from ``configs/limits.json``.
    """

    max_llm_calls: int = 14
    max_tool_calls: int = 28
    llm_calls: int = 0
    tool_calls: int = 0
    max_runtime_seconds: float | None = None

    @classmethod
    def from_limits(cls, limits: dict[str, Any]) -> "BudgetManager":
        return cls(
            max_llm_calls=int(limits["max_llm_calls"]),
            max_tool_calls=int(limits["max_tool_calls"]),
            max_runtime_seconds=float(limits["max_runtime_seconds"]),
        )

    def consume_llm(self) -> None:
        if self.llm_calls >= self.max_llm_calls:
            raise BudgetExceeded("LLM-call budget exhausted")
        self.llm_calls += 1

    def consume_tool(self) -> None:
        if self.tool_calls >= self.max_tool_calls:
            raise BudgetExceeded("Tool-call budget exhausted")
        self.tool_calls += 1

    @property
    def remaining_llm(self) -> int:
        return self.max_llm_calls - self.llm_calls

    @property
    def remaining_tools(self) -> int:
        return self.max_tool_calls - self.tool_calls


class RuntimeBudget:
    """Wall-clock budget for one agent run.

    Time spent blocked on a *human* approver is excluded from the agent's runtime (it is
    not agent work and the agent cannot shorten it) but is still reported separately, so
    ``wall_seconds`` is the true end-to-end duration and ``elapsed`` is what the budget
    enforces. The clock is injectable so tests can simulate slow runs deterministically.
    """

    def __init__(self, max_seconds: float, clock: Callable[[], float] = time.monotonic) -> None:
        self.max_seconds = float(max_seconds)
        self.clock = clock
        self.started_at: float | None = None
        self.excluded_seconds = 0.0

    def start(self) -> None:
        self.started_at = self.clock()
        self.excluded_seconds = 0.0

    @property
    def wall_seconds(self) -> float:
        return 0.0 if self.started_at is None else max(0.0, self.clock() - self.started_at)

    @property
    def elapsed(self) -> float:
        return max(0.0, self.wall_seconds - self.excluded_seconds)

    @property
    def remaining(self) -> float:
        return self.max_seconds - self.elapsed

    def exclude(self, seconds: float) -> None:
        self.excluded_seconds += max(0.0, float(seconds))

    def check(self) -> None:
        if self.started_at is not None and self.elapsed >= self.max_seconds:
            raise RuntimeBudgetExceeded(
                f"Runtime budget exhausted ({self.elapsed:.1f}s of {self.max_seconds:.0f}s agent time used)"
            )
