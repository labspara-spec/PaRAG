"""GuardrailPipeline — orchestrates all guard layers for a query lifecycle.

Call sequence inside madRAG.aquery_llm():

  1. pipeline.check_input(query)
     → AITG-APP-01 prompt injection, AITG-APP-03 sensitive data
     → raises GuardrailViolationError on violation

  2. pipeline.classify_intent(query, param)
     → intent = rag | direct | blocked
     → raises on blocked; returns "bypass" param overrides for "direct"

  3. [inside operate.kg_query / naive_query via global_config hook]
     pipeline.context_guard_func(context_str)
     → AITG-APP-02 indirect injection in retrieved chunks
     → raises GuardrailViolationError on violation

  4. pipeline.check_output(response_text)
     → AITG-APP-05 unsafe output, AITG-APP-12 toxic output
     → raises GuardrailViolationError on violation
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable

from .base import GuardResult, GuardrailViolationError, ViolationType
from .input_guard import InputGuard
from .context_guard import ContextGuard
from .output_guard import OutputGuard
from .intent_classifier import IntentClassifier, Intent


@dataclass
class GuardrailConfig:
    """Flat configuration for building a GuardrailPipeline from env vars / dicts."""

    # Feature flags
    input_guard_enabled: bool = True
    context_guard_enabled: bool = True
    output_guard_enabled: bool = True
    intent_classification_enabled: bool = False  # disabled by default (adds latency)

    # Input guard options
    input_max_length: int = 32_768
    input_pii_check: bool = True
    input_llm_check: bool = False

    # Context guard options
    context_llm_check: bool = False
    context_block_on_first_match: bool = False

    # Output guard options
    output_application_risks: bool = True
    output_content_risks: bool = True
    output_llm_check: bool = False

    # Intent classifier options
    intent_confidence_threshold: float = 0.65
    intent_default: str = "rag"


@dataclass
class GuardrailPipeline:
    """Aggregates all guards and exposes a simple async API for madRAG.

    Args:
        input_guard: InputGuard instance (or None to skip).
        context_guard: ContextGuard instance (or None to skip).
        output_guard: OutputGuard instance (or None to skip).
        intent_classifier: IntentClassifier instance (or None to skip).
    """

    input_guard: InputGuard | None = None
    context_guard: ContextGuard | None = None
    output_guard: OutputGuard | None = None
    intent_classifier: IntentClassifier | None = None

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def from_config(
        cls,
        cfg: GuardrailConfig,
        *,
        intent_llm_func: Callable[..., Awaitable[Any]] | None = None,
        llm_safety_func: Callable[[str], Awaitable[GuardResult]] | None = None,
    ) -> "GuardrailPipeline":
        """Build a pipeline from a flat config object."""
        input_guard = (
            InputGuard(
                max_length=cfg.input_max_length,
                pii_check_enabled=cfg.input_pii_check,
                llm_check_enabled=cfg.input_llm_check,
                llm_safety_func=llm_safety_func if cfg.input_llm_check else None,
            )
            if cfg.input_guard_enabled
            else None
        )

        context_guard = (
            ContextGuard(
                llm_check_enabled=cfg.context_llm_check,
                llm_safety_func=llm_safety_func if cfg.context_llm_check else None,
                block_on_first_match=cfg.context_block_on_first_match,
            )
            if cfg.context_guard_enabled
            else None
        )

        output_guard = (
            OutputGuard(
                llm_check_enabled=cfg.output_llm_check,
                llm_safety_func=llm_safety_func if cfg.output_llm_check else None,
                check_application_risks=cfg.output_application_risks,
                check_content_risks=cfg.output_content_risks,
            )
            if cfg.output_guard_enabled
            else None
        )

        intent_classifier = (
            IntentClassifier(
                llm_func=intent_llm_func,
                confidence_threshold=cfg.intent_confidence_threshold,
                default_intent=cfg.intent_default,  # type: ignore[arg-type]
            )
            if cfg.intent_classification_enabled
            else None
        )

        return cls(
            input_guard=input_guard,
            context_guard=context_guard,
            output_guard=output_guard,
            intent_classifier=intent_classifier,
        )

    # ------------------------------------------------------------------
    # Phase 1 — Input
    # ------------------------------------------------------------------

    async def check_input(self, query: str) -> None:
        """Validate input; raise GuardrailViolationError on violation."""
        if self.input_guard is None:
            return
        result = await self.input_guard.check(query)
        result.raise_if_blocked()

    # ------------------------------------------------------------------
    # Phase 2 — Intent classification
    # ------------------------------------------------------------------

    async def classify_intent(self, query: str) -> Intent:
        """Classify query intent; raise on blocked; return intent string."""
        if self.intent_classifier is None:
            return "rag"
        return await self.intent_classifier.classify_and_raise_if_blocked(query)

    # ------------------------------------------------------------------
    # Phase 3 — Context (hook for operate.py)
    # ------------------------------------------------------------------

    async def check_context(self, context_str: str) -> None:
        """Check assembled context string; raise on indirect injection."""
        if self.context_guard is None:
            return
        result = await self.context_guard.check_context_string(context_str)
        result.raise_if_blocked()

    def get_context_guard_func(
        self,
    ) -> Callable[[str], Awaitable[None]] | None:
        """Return an async callable suitable for injection into global_config."""
        if self.context_guard is None:
            return None
        return self.check_context

    # ------------------------------------------------------------------
    # Phase 4 — Output
    # ------------------------------------------------------------------

    async def check_output(self, response: str) -> str:
        """Validate LLM output; raise on violation; return (possibly same) text."""
        if self.output_guard is None:
            return response
        result = await self.output_guard.check(response)
        result.raise_if_blocked()
        return result.sanitized_content if result.sanitized_content is not None else response
