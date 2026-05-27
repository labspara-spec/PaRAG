"""madRAG Guardrails — OWASP AITG-compliant LLM safety controls.

Public surface:
  GuardrailPipeline   — main orchestrator; attach to madRAG via guardrails param
  GuardrailConfig     — flat config dataclass; build from env vars or dicts
  GuardrailViolationError  — raised on any guardrail block
  ViolationType       — enum of violation categories
  GuardResult         — individual guard check result
  InputGuard          — AITG-APP-01/03: input validation
  ContextGuard        — AITG-APP-02: indirect injection in retrieved context
  OutputGuard         — AITG-APP-05/12: unsafe / toxic output
  IntentClassifier    — intent-based RAG vs direct routing
"""

from .base import GuardResult, GuardrailViolationError, ViolationType
from .input_guard import InputGuard
from .context_guard import ContextGuard
from .output_guard import OutputGuard
from .intent_classifier import IntentClassifier, Intent
from .pipeline import GuardrailConfig, GuardrailPipeline

__all__ = [
    "GuardResult",
    "GuardrailViolationError",
    "ViolationType",
    "InputGuard",
    "ContextGuard",
    "OutputGuard",
    "IntentClassifier",
    "Intent",
    "GuardrailConfig",
    "GuardrailPipeline",
]
