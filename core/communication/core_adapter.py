from __future__ import annotations

import importlib
import json
import logging
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
CORE_DIR = ROOT / "core"
DB_FILE = ROOT / "database" / "claud_memory.db"


def _add_import_paths() -> None:
    for path in (str(ROOT), str(CORE_DIR)):
        while path in sys.path:
            sys.path.remove(path)
        sys.path.insert(0, path)


class ClaudCoreAdapter:
    """Adapter to Claud's existing Core response pipeline.

    The adapter does not implement a second LLM pipeline. It routes requests
    through core/main.py, which already handles reasoning prompts, memory
    retrieval, knowledge grounding, Ollama generation, self-critique and
    evaluation logging. CognitiveOrchestrator and ContextManager are invoked
    in shadow/diagnostic mode, matching their current Core implementation.
    """

    def __init__(self) -> None:
        _add_import_paths()
        self.core = importlib.import_module("main")
        self._context_manager_cls = None
        self._context_request_cls = None
        self._orchestrator_cls = None
        self._components_loaded = False

    def _load_diagnostic_components(self) -> None:
        if self._components_loaded:
            return
        _add_import_paths()
        context_module = importlib.import_module("core.context.context_manager")
        models_module = importlib.import_module("core.context.models")
        orchestrator_module = importlib.import_module(
            "core.orchestrator.cognitive_orchestrator"
        )
        self._context_manager_cls = context_module.ContextManager
        self._context_request_cls = models_module.ContextRequest
        self._orchestrator_cls = orchestrator_module.CognitiveOrchestrator
        self._components_loaded = True

    def healthcheck(self) -> dict[str, Any]:
        settings = self.core.load_settings()
        ollama_url = str(
            settings.get("ollama_url", "http://127.0.0.1:11434")
        ).rstrip("/")
        model = str(
            settings.get("local_model", settings.get("model", "llama3.2"))
        ).strip()
        if not model:
            raise RuntimeError("local_model tidak dikonfigurasi di settings.json")

        import requests

        response = requests.get(f"{ollama_url}/api/tags", timeout=10)
        response.raise_for_status()
        available = {
            str(item.get("name", ""))
            for item in response.json().get("models", [])
        }
        candidate_names = {model}
        if ":" not in model:
            candidate_names.add(f"{model}:latest")
        if not candidate_names.intersection(available):
            raise RuntimeError(
                f"Model Core '{model}' tidak ditemukan. Model tersedia: "
                + (", ".join(sorted(available)) if available else "(none)")
            )

        if not DB_FILE.is_file():
            raise FileNotFoundError(f"Core database tidak ditemukan: {DB_FILE}")

        with sqlite3.connect(DB_FILE.resolve().as_uri() + "?mode=ro", uri=True) as con:
            tables = {
                row[0]
                for row in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        required = {"conversations", "messages", "memories", "evaluations", "knowledge"}
        missing = required - tables
        if missing:
            raise RuntimeError(
                "Tabel Core database belum lengkap: " + ", ".join(sorted(missing))
            )

        # Import diagnostic components to verify they are loadable. They remain
        # non-executing/shadow components in the current Core architecture.
        self._load_diagnostic_components()

        return {
            "status": "READY",
            "core_version": settings.get("version", "unknown"),
            "model": model,
            "ollama_url": ollama_url,
            "database": str(DB_FILE),
            "response_pipeline": "core.main.generate_answer",
            "memory_retrieval": "core.main.retrieve_memories",
            "knowledge_grounding": "core.knowledge.grounding",
            "self_critique": "core.reasoning.self_critique",
            "context_manager": "available (diagnostic integration)",
            "cognitive_orchestrator": "available (shadow mode)",
        }

    def create_conversation(self, topic: str) -> int:
        # Verify Core database and runtime before creating a mapped conversation.
        self.healthcheck()
        conversation_id = self.core.create_conversation()
        title = ("MCP: " + topic.strip())[:160]
        with self.core.connect_db() as con:
            con.execute(
                "UPDATE conversations SET title = ? WHERE id = ?",
                (title, conversation_id),
            )
        return int(conversation_id)

    def _shadow_diagnostics(self, conversation_id: int, user_text: str) -> dict[str, Any]:
        """Run the existing context/orchestrator modules without changing routing."""
        result: dict[str, Any] = {
            "context_status": "unavailable",
            "orchestrator_status": "unavailable",
            "intent": None,
            "complexity": None,
            "risk_level": None,
            "strategy": None,
        }
        try:
            # Load diagnostic classes lazily before first use. This keeps
            # shadow diagnostics non-executing while avoiding NoneType calls.
            self._load_diagnostic_components()
            context_manager = self._context_manager_cls()
            request = self._context_request_cls(
                query=user_text,
                conversation_id=conversation_id,
                include_conversation=True,
                include_memories=True,
                knowledge_mode="lexical",
                conversation_limit=12,
                memory_limit=5,
                knowledge_limit=8,
            )
            built = context_manager.build(request)
            result["context_status"] = built.status
            result["context_source_status"] = dict(
                built.diagnostics.source_status
            )

            cognitive = self._orchestrator_cls().analyze(
                user_text,
                context=built.context,
            )
            result["orchestrator_status"] = cognitive.status
            result["intent"] = cognitive.assessment.intent
            result["complexity"] = cognitive.assessment.complexity
            result["risk_level"] = cognitive.assessment.risk_level
            result["strategy"] = cognitive.plan.strategy
        except Exception as exc:
            logging.exception("Shadow diagnostic failed")
            result["diagnostic_error"] = f"{type(exc).__name__}: {exc}"
        return result

    def respond(self, conversation_id: int, user_text: str) -> dict[str, Any]:
        if not isinstance(user_text, str) or not user_text.strip():
            raise ValueError("Pesan tidak boleh kosong.")

        settings = self.core.load_settings()
        started = time.perf_counter()
        diagnostics = self._shadow_diagnostics(conversation_id, user_text)
        route = self.core.classify_query(user_text)

        if route == "evaluation":
            answer = self.core.format_evaluation_report()
            critique_status = "deterministic_evaluation"
            draft = answer
            duration = 0.0
        elif route == "database":
            answer = self.core.database_only_answer(user_text)
            if answer is None:
                answer, critique_status, duration, draft = self.core.generate_answer(
                    settings, conversation_id, user_text
                )
            else:
                critique_status = "deterministic_database"
                draft = answer
                duration = 0.0
        elif route == "configuration":
            answer = self.core.deterministic_configuration_answer()
            critique_status = "deterministic_configuration"
            draft = answer
            duration = 0.0
        else:
            answer, critique_status, duration, draft = self.core.generate_answer(
                settings, conversation_id, user_text
            )

        # Persist the completed exchange atomically in Claud's actual Core DB.
        created_at = self.core.now()
        with self.core.connect_db() as con:
            con.execute(
                "INSERT INTO messages (conversation_id, role, content, created_at) "
                "VALUES (?, 'user', ?, ?)",
                (conversation_id, user_text, created_at),
            )
            con.execute(
                "INSERT INTO messages (conversation_id, role, content, created_at) "
                "VALUES (?, 'assistant', ?, ?)",
                (conversation_id, answer, created_at),
            )
            con.execute(
                """INSERT INTO evaluations (
                    conversation_id, user_question, draft_answer, final_answer,
                    critique_status, duration_seconds, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    conversation_id,
                    user_text,
                    draft,
                    answer,
                    critique_status,
                    float(duration),
                    created_at,
                ),
            )

        return {
            "answer": answer,
            "route": route,
            "critique_status": critique_status,
            "duration_seconds": round(
                max(float(duration), time.perf_counter() - started), 3
            ),
            "core_conversation_id": conversation_id,
            "diagnostics": diagnostics,
        }
