"""Intent classifier — decides if a query requires RAG retrieval or direct LLM.

Uses a small/fast LLM (the `intent` role, typically a haiku-class model) to
classify each incoming query into one of three intents:

  rag     — query requires knowledge-base retrieval to answer accurately
  direct  — query can be answered directly by the LLM (general knowledge,
             math, coding, definitions, etc.)
  blocked — query is harmful, adversarial, or violates usage policy

Classification is performed in a single structured-JSON call and is designed
to be fast (<100 ms on a local endpoint).  Rule-based pre-screening catches
obvious cases without an LLM round-trip.

Integration:
  Called from GuardrailPipeline.classify_intent() before mode dispatch in
  LightRAG.aquery_llm().  If `rag`, the caller proceeds normally.  If
  `direct`, mode is overridden to `bypass`.  If `blocked`, a
  GuardrailViolationError is raised.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Awaitable, Literal

from .base import GuardrailViolationError, ViolationType


Intent = Literal["rag", "direct", "blocked"]


# ---------------------------------------------------------------------------
# Rule-based pre-screening (zero LLM cost)
# ---------------------------------------------------------------------------

# Queries that almost always need RAG (contain domain-specific retrieval cues)
_RAG_CUES: re.Pattern[str] = re.compile(
    r"\b(according\s+to|based\s+on|in\s+the\s+(document|report|file|database|kb|"
    r"knowledge\s+base)|what\s+does\s+the\s+(document|file|report)\s+say|"
    r"find\s+(me\s+)?(information|details?|data)\s+about|retrieve|lookup|search\s+for|"
    r"summarize\s+(the|this|our)|what\s+is\s+in\s+the|tell\s+me\s+about\s+our)\b",
    re.IGNORECASE,
)

# Queries that almost always do NOT need RAG (general / conversational)
_DIRECT_CUES: re.Pattern[str] = re.compile(
    r"^\s*(what\s+is\s+(a|an|the\s+definition|the\s+meaning|today|the\s+time|the\s+capital|"
    r"the\s+difference|the\s+best|the\s+largest|the\s+fastest|\d)|"
    r"how\s+(do|does|to|can)\s+I|translate\s+|calculate\s+|"
    r"write\s+a?\s+(poem|story|email|function|code|script|joke|list)|"
    r"hello|hi|hey|thanks|thank\s+you|"
    r"\d+\s*[\+\-\*\/]\s*\d)",
    re.IGNORECASE,
)

# Hard blocks — skip LLM classification entirely
_HARD_BLOCK: re.Pattern[str] = re.compile(
    r"\b(how\s+to\s+(make|build|synthesize)\s+(a\s+)?(bomb|drug|weapon|poison|malware)|"
    r"make\s+(a\s+)?(bomb|explosive|bioweapon)|"
    r"child\s+(porn|sexual)|kill\s+(myself|yourself|someone)|suicide\s+method|"
    r"DAN\s+mode|jailbreak\s+this|ignore\s+(all\s+)?instructions)\b",
    re.IGNORECASE,
)


def _rule_based_intent(query: str) -> Intent | None:
    """Return intent if rule-based screening is conclusive, else None."""
    if _HARD_BLOCK.search(query):
        return "blocked"
    if _RAG_CUES.search(query):
        return "rag"
    if _DIRECT_CUES.match(query):
        return "direct"
    return None


# ---------------------------------------------------------------------------
# LLM prompt — loaded lazily from lightrag/prompts/guardrails.xml
# ---------------------------------------------------------------------------

def _get_classify_system_prompt() -> str:
    from lightrag.prompts import load_prompts
    return load_prompts()["intent_classify_system_prompt"]


@dataclass
class IntentClassifier:
    """Classifies query intent using a two-phase approach.

    Args:
        llm_func: Async callable matching the LightRAG LLM function signature.
            Expected signature: ``(prompt, system_prompt=...) -> str``.
            Typically the wrapped ``intent`` role func.
        confidence_threshold: Minimum confidence to trust the LLM result.
            Below this, fall back to ``"rag"`` (safe default).
        default_intent: Fallback when LLM call fails or returns invalid JSON.
    """

    llm_func: Callable[..., Awaitable[Any]] | None = None
    confidence_threshold: float = 0.65
    default_intent: Intent = "rag"

    async def classify(self, query: str) -> Intent:
        # Phase 1: rule-based (free)
        intent = _rule_based_intent(query)
        if intent is not None:
            return intent

        # Phase 2: LLM-based
        if self.llm_func is None:
            return self.default_intent

        try:
            raw = await self.llm_func(
                query.strip(),
                system_prompt=_get_classify_system_prompt(),
            )
            result = self._parse_llm_result(raw)
            return result
        except Exception:
            return self.default_intent

    # ------------------------------------------------------------------

    def _parse_llm_result(self, raw: Any) -> Intent:
        if not isinstance(raw, str):
            return self.default_intent

        # Strip markdown code fences if present
        text = re.sub(r"```(?:json)?\s*(.*?)\s*```", r"\1", raw, flags=re.DOTALL).strip()

        # Try to extract JSON object
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            return self.default_intent

        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            return self.default_intent

        intent_raw = str(data.get("intent", "")).lower().strip()
        confidence = float(data.get("confidence", 0.0))

        if intent_raw not in ("rag", "direct", "blocked"):
            return self.default_intent

        if confidence < self.confidence_threshold and intent_raw != "blocked":
            return self.default_intent

        return intent_raw  # type: ignore[return-value]

    async def classify_and_raise_if_blocked(self, query: str) -> Intent:
        """Classify; raise GuardrailViolationError if intent is blocked."""
        intent = await self.classify(query)
        if intent == "blocked":
            raise GuardrailViolationError(
                ViolationType.BLOCKED_INTENT,
                "Query blocked by intent classifier.",
            )
        return intent
