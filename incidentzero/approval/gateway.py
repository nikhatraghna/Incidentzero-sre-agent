from __future__ import annotations

from typing import Any, Protocol


class ApprovalGateway(Protocol):
    def approve(self, action_name: str, arguments: dict[str, Any], justification: str) -> bool:
        ...


class ConsoleApprovalGateway:
    def approve(self, action_name: str, arguments: dict[str, Any], justification: str) -> bool:
        print("\n=== HUMAN APPROVAL REQUIRED ===")
        print("Action:", action_name)
        print("Arguments:", arguments)
        print("Justification:", justification)
        return input("Type APPROVE to continue: ").strip().upper() == "APPROVE"


class AlwaysApproveGateway:
    def approve(self, action_name: str, arguments: dict[str, Any], justification: str) -> bool:
        return True


class AlwaysDenyGateway:
    def approve(self, action_name: str, arguments: dict[str, Any], justification: str) -> bool:
        return False
