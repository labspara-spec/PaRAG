"""Input guardrail — AITG-APP-01 (Prompt Injection) + AITG-APP-03 (Sensitive Data).

Two-layer design:
  1. Rule-based: fast regex patterns, zero LLM cost.
  2. LLM-based: semantic safety check (optional, controlled by llm_check_enabled).

Rule patterns are drawn directly from OWASP AITG-APP-01 test payloads:
  - Role-play / persona hijack
  - Context reset / ignore-previous instructions
  - System-prompt override
  - Obfuscation / token smuggling markers
  - Known jailbreak tokens (DAN, developer-mode, etc.)

PII patterns (AITG-APP-03):
  - SSN, credit-card, passport, API-key/token formats
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Awaitable

from .base import GuardResult, ViolationType


# ---------------------------------------------------------------------------
# Pattern sets
# ---------------------------------------------------------------------------

_INJECTION_PATTERNS: list[re.Pattern[str]] = [
    # Context reset / ignore-previous
    re.compile(
        r"\b(ignore|disregard|forget|override|bypass)\s+(all\s+)?"
        r"(previous|prior|above|earlier|your|the)?\s*"
        r"(instructions?|prompts?|constraints?|guidelines?|rules?|context)\b",
        re.IGNORECASE,
    ),
    # "New instructions" style override
    re.compile(
        r"\b(new\s+(system\s+)?instructions?|updated?\s+(system\s+)?prompts?)\s*[:\-]",
        re.IGNORECASE,
    ),
    # Role-play / persona takeover
    re.compile(
        r"\b(pretend|act|behave|imagine|roleplay|role-play|you\s+are\s+now|"
        r"from\s+now\s+on\s+(you\s+are|act\s+as)|you\s+will\s+now\s+act)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(pretend|act|behave)\s+(you\s+are|to\s+be|as\s+(if|though))\b",
        re.IGNORECASE,
    ),
    # System prompt injection marker
    re.compile(r"<\s*/?system\s*>", re.IGNORECASE),
    re.compile(r"\[SYSTEM\]|\[INST\]|\[\/INST\]", re.IGNORECASE),
    # DAN / jailbreak tokens (AITG-APP-01 payload §2)
    re.compile(
        r"\b(DAN|jailbreak|developer\s+mode|god\s+mode|no\s+restrictions?\s+mode|"
        r"unrestricted\s+mode|prompt\s+injection)\b",
        re.IGNORECASE,
    ),
    # Grandma / hypothetical bypass cues
    re.compile(
        r"\b(hypothetically|theoretically|for\s+educational\s+purposes?\s+only|"
        r"in\s+a\s+story|in\s+fiction|fictional\s+context)\b.*"
        r"\b(how\s+to|step[s\s]+by\s+step|instruct|detail|explain)\b",
        re.IGNORECASE | re.DOTALL,
    ),
    # Obfuscation / token smuggling markers
    re.compile(r"base64\s*:|rot13\s*:|hex\s+encode", re.IGNORECASE),
]

_SENSITIVE_DATA_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # US Social Security Number
    ("SSN", re.compile(r"\b\d{3}[-\s]?\d{2}[-\s]?\d{4}\b")),
    # Credit card (Luhn check is skipped for speed — pattern is sufficient signal)
    ("credit_card", re.compile(r"\b(?:4\d{12}(?:\d{3})?|5[1-5]\d{14}|3[47]\d{13}|6011\d{12})\b")),
    # API key / token heuristic (high-entropy 30+ char alphanumeric strings with key context)
    (
        "api_key",
        re.compile(
            r"(?:api[_\-\s]?key|api[_\-\s]?token|access[_\-\s]?token|"
            r"secret[_\-\s]?key|bearer\s+token)\s*[=:\s]+\s*[A-Za-z0-9_\-\.]{20,}",
            re.IGNORECASE,
        ),
    ),
    # Private key header
    ("private_key", re.compile(r"-----BEGIN\s+(?:RSA\s+)?PRIVATE\s+KEY-----")),
    # Password in URL
    ("password_url", re.compile(r"https?://[^:]+:[^@]+@", re.IGNORECASE)),
]

_MAX_INPUT_LENGTH = 32_768  # characters


# ---------------------------------------------------------------------------
# Guard
# ---------------------------------------------------------------------------


@dataclass
class InputGuard:
    """Validates user input before any LLM or RAG call.

    Args:
        max_length: Maximum allowed input character length.
        llm_check_enabled: When True, borderline inputs are verified by the
            LLM-based safety function (``llm_safety_func``).
        llm_safety_func: Async callable ``(text) -> GuardResult``.  Only
            called when ``llm_check_enabled=True`` and rule-based checks pass.
        pii_check_enabled: When True, scan for PII / sensitive credentials.
        extra_injection_patterns: Additional compiled regex patterns merged
            with the built-in set.
    """

    max_length: int = _MAX_INPUT_LENGTH
    llm_check_enabled: bool = False
    llm_safety_func: Callable[[str], Awaitable[GuardResult]] | None = None
    pii_check_enabled: bool = True
    extra_injection_patterns: list[re.Pattern[str]] = field(default_factory=list)

    async def check(self, text: str) -> GuardResult:
        """Run all enabled input checks; return first violation or ok."""
        # 1. Length guard
        if len(text) > self.max_length:
            return GuardResult.block(
                ViolationType.PROMPT_INJECTION,
                f"Input exceeds max length ({len(text)} > {self.max_length}).",
                rule="max_length",
            )

        # 2. Rule-based injection detection (AITG-APP-01)
        result = self._rule_check_injection(text)
        if not result.passed:
            return result

        # 3. PII / sensitive data (AITG-APP-03)
        if self.pii_check_enabled:
            result = self._rule_check_pii(text)
            if not result.passed:
                return result

        # 4. Optional LLM semantic safety check
        if self.llm_check_enabled and self.llm_safety_func is not None:
            result = await self.llm_safety_func(text)
            if not result.passed:
                return result

        return GuardResult.ok()

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _rule_check_injection(self, text: str) -> GuardResult:
        patterns = _INJECTION_PATTERNS + self.extra_injection_patterns
        for pattern in patterns:
            m = pattern.search(text)
            if m:
                return GuardResult.block(
                    ViolationType.PROMPT_INJECTION,
                    f"Prompt injection pattern detected: '{m.group(0)[:80]}'.",
                    rule=pattern.pattern[:60],
                )
        return GuardResult.ok()

    def _rule_check_pii(self, text: str) -> GuardResult:
        for label, pattern in _SENSITIVE_DATA_PATTERNS:
            if pattern.search(text):
                return GuardResult.block(
                    ViolationType.SENSITIVE_DATA,
                    f"Sensitive data pattern detected: {label}.",
                    rule=label,
                )
        return GuardResult.ok()
