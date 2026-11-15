"""
Claud AI - Context Manager Models
Version: 0.1.0

Defines request, source result, diagnostics, and build result.
This module does not access databases or execute operations.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ContextRequest:
    query: str
    conversation_id: Optional[int] = None
    include_conversation: bool = True
    include_memories: bool = True
    knowledge_mode: str = "none"
    conversation_limit: int = 12
    memory_limit: int = 5
    knowledge_limit: int = 8

    def validate(self):
        if not isinstance(self.query, str) or not self.query.strip():
            raise ValueError("Context query must be a non-empty string.")

        if self.conversation_id is not None:
            if (
                not isinstance(self.conversation_id, int)
                or isinstance(self.conversation_id, bool)
                or self.conversation_id < 1
            ):
                raise ValueError("conversation_id must be a positive integer.")

        for name in (
            "conversation_limit",
            "memory_limit",
            "knowledge_limit",
        ):
            value = getattr(self, name)
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or value < 1
            ):
                raise ValueError(f"{name} must be a positive integer.")

        allowed_modes = {"none", "lexical", "hybrid"}
        if self.knowledge_mode not in allowed_modes:
            raise ValueError(
                "knowledge_mode must be none, lexical, or hybrid."
            )


@dataclass
class ContextSourceResult:
    source: str
    status: str = "not_requested"
    items: List[Dict[str, Any]] = field(default_factory=list)
    method: Optional[str] = None
    error: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ContextDiagnostics:
    requested_sources: List[str] = field(default_factory=list)
    source_status: Dict[str, str] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    duplicate_count: int = 0
    execution_enabled: bool = False


@dataclass
class ContextBuildResult:
    context: Any
    sources: Dict[str, ContextSourceResult] = field(default_factory=dict)
    diagnostics: ContextDiagnostics = field(
        default_factory=ContextDiagnostics
    )
    status: str = "planned"
    metadata: Dict[str, Any] = field(default_factory=dict)


VERSION = "0.1.0"
