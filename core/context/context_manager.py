"""
Claud AI - Context Manager
Version: 0.1.0

Coordinates conversation, memory, and knowledge adapters.
Builds CognitiveContext without modifying Core or databases.
"""

from typing import Any, Dict, List

from core.orchestrator.models import CognitiveContext

from .models import (
    ContextRequest,
    ContextSourceResult,
    ContextDiagnostics,
    ContextBuildResult,
    VERSION as MODELS_VERSION,
)

from .conversation_adapter import ConversationAdapter
from .memory_adapter import MemoryAdapter
from .knowledge_adapter import KnowledgeAdapter


VERSION = "0.1.0"


class ContextManager:
    """Builds a unified cognitive context from isolated data sources."""

    def __init__(
        self,
        conversation_adapter=None,
        memory_adapter=None,
        knowledge_adapter=None,
    ):
        self.conversation_adapter = (
            conversation_adapter or ConversationAdapter()
        )
        self.memory_adapter = memory_adapter or MemoryAdapter()
        self.knowledge_adapter = knowledge_adapter or KnowledgeAdapter()

    @staticmethod
    def _deduplicate(items, identity_fields):
        """
        Preserve first occurrence and remove duplicate records.
        Returns (unique_items, duplicate_count).
        """
        unique = []
        seen = set()
        duplicate_count = 0

        if not isinstance(items, list):
            raise TypeError("Adapter items must be a list.")

        for item in items:
            if not isinstance(item, dict):
                raise TypeError("Every adapter item must be a dictionary.")

            identity = tuple(item.get(field) for field in identity_fields)

            # Records with incomplete identities are retained.
            if any(value is None for value in identity):
                unique.append(item)
                continue

            try:
                hash(identity)
            except TypeError:
                unique.append(item)
                continue

            if identity in seen:
                duplicate_count += 1
                continue

            seen.add(identity)
            unique.append(item)

        return unique, duplicate_count

    @staticmethod
    def _error_result(source, error):
        return ContextSourceResult(
            source=source,
            status="error",
            items=[],
            error=f"{type(error).__name__}: {error}",
            metadata={
                "execution_enabled": False,
            },
        )

    def build(self, request: ContextRequest) -> ContextBuildResult:
        if not isinstance(request, ContextRequest):
            raise TypeError(
                "request must be a ContextRequest instance."
            )

        request.validate()

        conversation_messages = []
        memory_items = []
        knowledge_items = []

        sources: Dict[str, ContextSourceResult] = {}
        diagnostics = ContextDiagnostics(
            execution_enabled=False,
        )

        # --------------------------------------------------
        # Conversation retrieval
        # --------------------------------------------------

        if request.include_conversation and request.conversation_id is not None:
            diagnostics.requested_sources.append("conversation")

            try:
                conversation_messages = (
                    self.conversation_adapter.get_recent_messages(
                        conversation_id=request.conversation_id,
                        limit=request.conversation_limit,
                    )
                )

                conversation_messages, duplicates = (
                    self._deduplicate(
                        conversation_messages,
                        ("role", "content", "created_at"),
                    )
                )

                diagnostics.duplicate_count += duplicates

                sources["conversation"] = ContextSourceResult(
                    source="conversation",
                    status=(
                        "success"
                        if conversation_messages
                        else "no_results"
                    ),
                    items=conversation_messages,
                    method="recent_messages",
                    metadata={
                        "version": getattr(
                            self.conversation_adapter,
                            "VERSION",
                            "unknown",
                        ),
                        "result_count": len(conversation_messages),
                        "execution_enabled": False,
                    },
                )

            except Exception as error:
                sources["conversation"] = self._error_result(
                    "conversation", error
                )
                diagnostics.errors.append(
                    f"conversation: {type(error).__name__}: {error}"
                )

        elif request.include_conversation:
            sources["conversation"] = ContextSourceResult(
                source="conversation",
                status="not_requested",
                method="skipped",
                metadata={
                    "reason": "conversation_id_not_provided",
                    "execution_enabled": False,
                },
            )

            diagnostics.warnings.append(
                "Conversation retrieval skipped: conversation_id not provided."
            )

        else:
            sources["conversation"] = ContextSourceResult(
                source="conversation",
                status="not_requested",
                method="disabled",
                metadata={
                    "execution_enabled": False,
                },
            )

        # --------------------------------------------------
        # Memory retrieval
        # --------------------------------------------------

        if request.include_memories:
            diagnostics.requested_sources.append("memories")

            try:
                memory_items = self.memory_adapter.search(
                    query=request.query,
                    limit=request.memory_limit,
                )

                memory_items, duplicates = self._deduplicate(
                    memory_items,
                    ("id",),
                )

                diagnostics.duplicate_count += duplicates

                sources["memories"] = ContextSourceResult(
                    source="memories",
                    status=(
                        "success"
                        if memory_items
                        else "no_results"
                    ),
                    items=memory_items,
                    method="lexical_memory_search",
                    metadata={
                        "version": getattr(
                            self.memory_adapter,
                            "VERSION",
                            "unknown",
                        ),
                        "result_count": len(memory_items),
                        "execution_enabled": False,
                    },
                )

            except Exception as error:
                sources["memories"] = self._error_result(
                    "memories", error
                )
                diagnostics.errors.append(
                    f"memories: {type(error).__name__}: {error}"
                )

        else:
            sources["memories"] = ContextSourceResult(
                source="memories",
                status="not_requested",
                method="disabled",
                metadata={
                    "execution_enabled": False,
                },
            )

        # --------------------------------------------------
        # Knowledge retrieval
        # --------------------------------------------------

        if request.knowledge_mode == "none":
            sources["knowledge"] = ContextSourceResult(
                source="knowledge",
                status="not_requested",
                method="disabled",
                metadata={"execution_enabled": False},
            )
        else:
            diagnostics.requested_sources.append("knowledge")

            try:
                knowledge_result = self.knowledge_adapter.retrieve(
                    query=request.query,
                    mode=request.knowledge_mode,
                    limit=request.knowledge_limit,
                )

                if not isinstance(knowledge_result, ContextSourceResult):
                    raise TypeError(
                        "KnowledgeAdapter must return ContextSourceResult."
                    )

                allowed_statuses = {"success", "no_results", "error"}
                if knowledge_result.status not in allowed_statuses:
                    raise ValueError(
                        f"Invalid knowledge status: {knowledge_result.status!r}"
                    )

                if not isinstance(knowledge_result.items, list):
                    raise TypeError(
                        "Knowledge result items must be a list."
                    )

                knowledge_result.items, duplicates = self._deduplicate(
                    knowledge_result.items,
                    ("id",),
                )

                diagnostics.duplicate_count += duplicates
                sources["knowledge"] = knowledge_result

                if knowledge_result.status == "error":
                    diagnostics.errors.append(
                        "knowledge: " + (
                            knowledge_result.error or "Unknown error"
                        )
                    )

            except Exception as error:
                sources["knowledge"] = self._error_result(
                    "knowledge", error
                )
                diagnostics.errors.append(
                    f"knowledge: {type(error).__name__}: {error}"
                )

        # --------------------------------------------------
        # Build unified CognitiveContext
        # --------------------------------------------------

        knowledge_items = sources["knowledge"].items

        context = CognitiveContext(
            conversation_id=request.conversation_id,
            recent_messages=conversation_messages,
            memories=memory_items,
            knowledge_evidence=knowledge_items,
        )

        diagnostics.source_status = {
            name: result.status
            for name, result in sources.items()
        }

        # --------------------------------------------------
        # Determine overall status
        # --------------------------------------------------

        requested_count = len(diagnostics.requested_sources)

        successful_sources = sum(
            1
            for name in diagnostics.requested_sources
            if sources[name].status in {"success", "no_results"}
        )

        failed_sources = sum(
            1
            for name in diagnostics.requested_sources
            if sources[name].status == "error"
        )

        invalid_sources = [
            name
            for name in diagnostics.requested_sources
            if sources[name].status not in {
                "success", "no_results", "error"
            }
        ]

        if invalid_sources:
            for name in invalid_sources:
                diagnostics.errors.append(
                    f"{name}: invalid source status "
                    f"{sources[name].status!r}"
                )

        if requested_count == 0:
            overall_status = "empty"
        elif invalid_sources:
            overall_status = "partial_failure" if successful_sources else "failed"
        elif failed_sources == 0:
            overall_status = "success"
        elif successful_sources > 0:
            overall_status = "partial_failure"
        else:
            overall_status = "failed"

        return ContextBuildResult(
            context=context,
            sources=sources,
            diagnostics=diagnostics,
            status=overall_status,
            metadata={
                "version": VERSION,
                "models_version": MODELS_VERSION,
                "requested_source_count": requested_count,
                "successful_source_count": successful_sources,
                "failed_source_count": failed_sources,
                "execution_enabled": False,
            },
        )
