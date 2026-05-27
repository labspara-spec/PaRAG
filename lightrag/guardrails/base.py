"""Base types for the PaRAG guardrail system.

Implements OWASP AI Testing Guide (AITG) controls:
  AITG-APP-01  Prompt Injection
  AITG-APP-02  Indirect Prompt Injection
  AITG-APP-03  Sensitive Data Leak
  AITG-APP-05  Unsafe Outputs
  AITG-APP-12  Toxic Output
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ViolationType(str, Enum):
    PROMPT_INJECTION = "prompt_injection"        # AITG-APP-01
    INDIRECT_INJECTION = "indirect_injection"    # AITG-APP-02
    SENSITIVE_DATA = "sensitive_data"            # AITG-APP-03
    UNSAFE_OUTPUT = "unsafe_output"              # AITG-APP-05
    TOXIC_OUTPUT = "toxic_output"                # AITG-APP-12
    BLOCKED_INTENT = "blocked_intent"            # intent classifier blocked


class GuardrailViolationError(Exception):
    """Raised when a guardrail blocks a request."""

    def __init__(
        self,
        violation_type: ViolationType,
        details: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.violation_type = violation_type
        self.details = details
        self.metadata: dict[str, Any] = metadata or {}
        super().__init__(f"Guardrail [{violation_type.value}]: {details}")

    def to_response(self) -> dict[str, Any]:
        return {
            "status": "blocked",
            "violation_type": self.violation_type.value,
            "message": self.details or f"Request blocked: {self.violation_type.value}.",
        }


@dataclass
class GuardResult:
    """Result from a single guard check."""

    passed: bool
    violation_type: ViolationType | None = None
    details: str = ""
    sanitized_content: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def ok(cls, sanitized: str | None = None) -> "GuardResult":
        return cls(passed=True, sanitized_content=sanitized)

    @classmethod
    def block(
        cls,
        violation_type: ViolationType,
        details: str = "",
        **metadata: Any,
    ) -> "GuardResult":
        return cls(
            passed=False,
            violation_type=violation_type,
            details=details,
            metadata=metadata,
        )

    def raise_if_blocked(self) -> None:
        if not self.passed and self.violation_type:
            raise GuardrailViolationError(self.violation_type, self.details, self.metadata)
