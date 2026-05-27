"""Output guardrail — AITG-APP-05 (Unsafe Outputs) + AITG-APP-12 (Toxic Output).

Two-layer design:
  1. Rule-based: regex for code injection, XSS, SSRF, and explicit harm keywords.
  2. LLM-based: optional semantic toxicity / safety check.

Covers OWASP AITG-APP-05:
  - Content-level risks (harmful instructions, violence, drugs, CSAM signals)
  - Application-level risks (XSS, SSRF, OS injection in generated output)
Covers OWASP AITG-APP-12:
  - Hate speech, explicit content, derogatory language
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Awaitable

from .base import GuardResult, ViolationType


# ---------------------------------------------------------------------------
# Application-level output risks (AITG-APP-05 §2)
# ---------------------------------------------------------------------------

_APPLICATION_RISKS: list[tuple[str, re.Pattern[str]]] = [
    # XSS / HTML injection
    ("xss", re.compile(r"<script[\s>]|javascript\s*:|on\w+\s*=", re.IGNORECASE)),
    # Server-side template injection
    ("ssti", re.compile(r"\{\{.*?\}\}|\{%.*?%\}", re.DOTALL)),
    # OS command injection patterns in generated code
    (
        "os_injection",
        re.compile(
            r"\b(os\.system|subprocess\.call|subprocess\.Popen|"
            r"exec\s*\(|eval\s*\(|__import__)\s*\(",
            re.IGNORECASE,
        ),
    ),
    # SSRF / internal resource access
    (
        "ssrf",
        re.compile(
            r"https?://(localhost|127\.\d+\.\d+\.\d+|0\.0\.0\.0|"
            r"169\.254\.\d+\.\d+|10\.\d+\.\d+\.\d+|192\.168\.\d+\.\d+)",
            re.IGNORECASE,
        ),
    ),
    ("file_path_leak", re.compile(r"file://[/\\]", re.IGNORECASE)),
    # SQL injection patterns in output
    (
        "sql_injection",
        re.compile(
            r"\b(DROP\s+TABLE|DELETE\s+FROM|INSERT\s+INTO|UNION\s+SELECT|"
            r"OR\s+1\s*=\s*1|AND\s+1\s*=\s*1)\b",
            re.IGNORECASE,
        ),
    ),
]

# ---------------------------------------------------------------------------
# Content-level risks (AITG-APP-05 §1 + AITG-APP-12)
# ---------------------------------------------------------------------------

_CONTENT_RISKS: list[tuple[str, re.Pattern[str]]] = [
    # CBRN / weapons of mass destruction (AITG-APP-05 payload §5)
    (
        "wmd",
        re.compile(
            r"\b(synthesize|produce|manufacture)\s+.{0,40}"
            r"\b(vx|sarin|novichok|nerve\s+agent|bioweapon|anthrax|ricin)\b",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
    # Bomb / explosive construction
    (
        "explosive",
        re.compile(
            r"\b(how\s+to\s+)?(make|build|construct|detonate)\s+.{0,30}"
            r"\b(bomb|explosive|iED|pipe\s+bomb)\b",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
    # CSAM (AITG-APP-05 payload §3) — any signal → immediate block
    (
        "csam",
        re.compile(
            r"\bchild\s+(porn|pornography|sexual|nude|naked|exploitation)\b|"
            r"\bminor\s+(sexual|porn|nude)\b",
            re.IGNORECASE,
        ),
    ),
    # Suicide method instruction (AITG-APP-05 payload §4)
    (
        "self_harm",
        re.compile(
            r"\b(painless\s+way\s+to\s+kill|method\s+to\s+commit\s+suicide|"
            r"how\s+to\s+end\s+(your|my)\s+life)\b",
            re.IGNORECASE,
        ),
    ),
]

# Hard-block patterns — these trigger block regardless of LLM check
_HARD_BLOCK_LABELS = {"csam", "wmd", "self_harm"}


@dataclass
class OutputGuard:
    """Validates LLM-generated output before delivery to the caller.

    Args:
        llm_check_enabled: Run LLM-based semantic toxicity check.
        llm_safety_func: Async callable ``(text) -> GuardResult``.
        check_application_risks: Scan for XSS / SSRF / injection (AITG-APP-05 §2).
        check_content_risks: Scan for harmful content (AITG-APP-05 §1, AITG-APP-12).
        extra_application_patterns: Additional compiled patterns.
        extra_content_patterns: Additional compiled content patterns.
    """

    llm_check_enabled: bool = False
    llm_safety_func: Callable[[str], Awaitable[GuardResult]] | None = None
    check_application_risks: bool = True
    check_content_risks: bool = True
    extra_application_patterns: list[tuple[str, re.Pattern[str]]] = field(default_factory=list)
    extra_content_patterns: list[tuple[str, re.Pattern[str]]] = field(default_factory=list)

    async def check(self, text: str) -> GuardResult:
        if not text:
            return GuardResult.ok()

        # 1. Application-level risk scan (AITG-APP-05 §2)
        if self.check_application_risks:
            result = self._check_application_risks(text)
            if not result.passed:
                return result

        # 2. Content-level risk scan (AITG-APP-05 §1, AITG-APP-12)
        if self.check_content_risks:
            result = self._check_content_risks(text)
            if not result.passed:
                return result

        # 3. LLM-based semantic check
        if self.llm_check_enabled and self.llm_safety_func is not None:
            result = await self.llm_safety_func(text)
            if not result.passed:
                return result

        return GuardResult.ok()

    # ------------------------------------------------------------------

    def _check_application_risks(self, text: str) -> GuardResult:
        patterns = _APPLICATION_RISKS + self.extra_application_patterns
        for label, pattern in patterns:
            m = pattern.search(text)
            if m:
                return GuardResult.block(
                    ViolationType.UNSAFE_OUTPUT,
                    f"Application-level risk [{label}]: '{m.group(0)[:80]}'.",
                    risk_label=label,
                )
        return GuardResult.ok()

    def _check_content_risks(self, text: str) -> GuardResult:
        patterns = _CONTENT_RISKS + self.extra_content_patterns
        for label, pattern in patterns:
            m = pattern.search(text)
            if m:
                violation = (
                    ViolationType.TOXIC_OUTPUT
                    if label not in _HARD_BLOCK_LABELS
                    else ViolationType.UNSAFE_OUTPUT
                )
                return GuardResult.block(
                    violation,
                    f"Content risk [{label}]: '{m.group(0)[:80]}'.",
                    risk_label=label,
                    hard_block=label in _HARD_BLOCK_LABELS,
                )
        return GuardResult.ok()
