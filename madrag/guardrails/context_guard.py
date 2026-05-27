"""Context guardrail — AITG-APP-02 (Indirect Prompt Injection).

Scans retrieved document chunks for embedded instructions that attempt to
override the LLM's behavior after retrieval.  Called inside kg_query /
naive_query after context is assembled but before the LLM generation step.

Two-layer design:
  1. Rule-based: fast regex for known indirect injection phrases.
  2. LLM-based: optional semantic check for subtle injections.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Awaitable

from .base import GuardResult, ViolationType


# ---------------------------------------------------------------------------
# Patterns for indirect injection inside retrieved content (AITG-APP-02)
# ---------------------------------------------------------------------------

_INDIRECT_PATTERNS: list[re.Pattern[str]] = [
    # Classic "ignore previous" in document body
    re.compile(
        r"\b(ignore|disregard|forget|bypass|override)\s+(all\s+)?"
        r"(previous|prior|above|earlier|your|the)?\s*"
        r"(instructions?|prompts?|constraints?|system\s+prompt|guidelines?|rules?)\b",
        re.IGNORECASE,
    ),
    # Embedded instruction markers that mimic system/user turns
    re.compile(r"<\s*/?system\s*>|<\s*/?prompt\s*>", re.IGNORECASE),
    re.compile(r"\[SYSTEM\]|\[INST\]|\[\/INST\]|\[USER\]|\[ASSISTANT\]", re.IGNORECASE),
    # "New instructions" embedded in docs
    re.compile(
        r"\b(new|updated?)\s+(system\s+)?instructions?\s*[:\-]\s*\S",
        re.IGNORECASE,
    ),
    # Exfiltration attempt via retrieved context
    re.compile(
        r"\b(send|forward|transmit|email|exfiltrate|leak)\s+.{0,40}"
        r"\b(system\s+prompt|api\s+key|credentials?|passwords?|token)\b",
        re.IGNORECASE | re.DOTALL,
    ),
    # Role hijack embedded in doc
    re.compile(
        r"\bfrom\s+now\s+on\s+(you\s+are|act\s+as|behave\s+as|respond\s+as)\b",
        re.IGNORECASE,
    ),
    # SSRF / command injection in context
    re.compile(
        r"file://|gopher://|dict://|ldap://|tftp://|data:text/html",
        re.IGNORECASE,
    ),
]

_SUSPICIOUS_CHUNK_SCORE_THRESHOLD = 2  # flag if ≥ N patterns match in one chunk


@dataclass
class ContextGuard:
    """Scans retrieved context chunks for indirect prompt injection.

    Args:
        llm_check_enabled: Run LLM-based semantic check on flagged chunks.
        llm_safety_func: Async callable ``(chunk_text) -> GuardResult``.
        block_on_first_match: Raise on first chunk violation instead of
            scanning all chunks.
        extra_patterns: Additional compiled regex merged with built-in set.
    """

    llm_check_enabled: bool = False
    llm_safety_func: Callable[[str], Awaitable[GuardResult]] | None = None
    block_on_first_match: bool = False
    extra_patterns: list[re.Pattern[str]] = field(default_factory=list)

    async def check_context_string(self, context_str: str) -> GuardResult:
        """Check the fully-assembled context string (for kg_query hook)."""
        return await self._check_text(context_str, source="assembled_context")

    async def check_chunks(self, chunks: list[str]) -> GuardResult:
        """Check a list of raw chunk strings."""
        last_violation: GuardResult | None = None
        for i, chunk in enumerate(chunks):
            result = await self._check_text(chunk, source=f"chunk[{i}]")
            if not result.passed:
                if self.block_on_first_match:
                    return result
                last_violation = result
        return last_violation if last_violation is not None else GuardResult.ok()

    # ------------------------------------------------------------------

    async def _check_text(self, text: str, source: str) -> GuardResult:
        patterns = _INDIRECT_PATTERNS + self.extra_patterns
        matches: list[str] = []
        for pattern in patterns:
            m = pattern.search(text)
            if m:
                matches.append(m.group(0)[:80])

        if len(matches) >= _SUSPICIOUS_CHUNK_SCORE_THRESHOLD:
            details = f"Indirect injection in {source}: " + "; ".join(matches[:3])
            return GuardResult.block(
                ViolationType.INDIRECT_INJECTION,
                details,
                source=source,
                match_count=len(matches),
            )

        if matches and self.llm_check_enabled and self.llm_safety_func is not None:
            result = await self.llm_safety_func(text)
            if not result.passed:
                return GuardResult.block(
                    ViolationType.INDIRECT_INJECTION,
                    result.details or f"LLM flagged indirect injection in {source}.",
                    source=source,
                )

        return GuardResult.ok()
