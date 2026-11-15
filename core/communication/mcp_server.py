from __future__ import annotations

import json
import logging
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from typing import Any, Literal

from mcp.server import MCPServer
from core_adapter import ClaudCoreAdapter
from mentor_tools import register_mentor_tools

ROOT = Path(__file__).resolve().parents[2]
DB_PATH = ROOT / "database" / "communication.db"
LOG_PATH = ROOT / "logs" / "communication-mcp.log"
MAX_MESSAGE_CHARS = 16000
MAX_TOPIC_CHARS = 2000
CURRENT_SCHEMA_VERSION = 1
MAX_EVENT_ID_CHARS = 128
MAX_TOOL_NAME_CHARS = 160
MAX_PROJECT_RELATIVE_PATH_CHARS = 512
MAX_ACTIVITY_DETAIL_CHARS = 4096
_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z")
_TOOL_NAME_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9_.:-]*\Z")

LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    filename=str(LOG_PATH),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    encoding="utf-8",
)

SERVER_INSTRUCTIONS = """You are connected to Claud AI's existing Core through a local MCP bridge.
When the user wants a discussion with Claud, call claud_start_discussion, then send the opening question with claud_send_message.
The tool invokes Claud's existing core/main.py response pipeline: reasoning prompt, saved-memory retrieval, knowledge grounding, local Ollama generation, self-critique and evaluation logging. ContextManager and CognitiveOrchestrator are also run in their current diagnostic/shadow mode; do not claim they are active execution planners.
For a two-way discussion, formulate your own substantive response to Claud and relay it with claud_send_message(sender='chatgpt'). Continue only while useful and within the turn limit. A tool call is required for each exchange; the bridge does not autonomously wake ChatGPT or send messages while the ChatGPT session is idle.
Mentor mode: when the user asks you to teach Claud, run a lesson, teach-back, counterexample, and unfamiliar-case test cycle. Only after successful evaluation, create a pending record with claud_propose_learning. Never approve a lesson unless the user explicitly sends the exact phrase APPROVE THIS LEARNING. Approved lessons are stored as Core memories; they do not update model weights. A user can intervene by asking you to relay their message with sender='user'. The communication database records inbound messages before Core processing, so the local Live Conversation Room can show the message and a Claud-thinking indicator during generation. Use claud_get_history to review the transcript and claud_end_discussion to stop. Never claim messages were exchanged unless the tools confirm it. Never send secrets or credentials unless explicitly requested and appropriate."""

mcp = MCPServer("Claud AI Core Communication Bridge", instructions=SERVER_INSTRUCTIONS)
_CORE: ClaudCoreAdapter | None = None


def get_core() -> ClaudCoreAdapter:
    global _CORE
    if _CORE is None:
        _CORE = ClaudCoreAdapter()
    return _CORE


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def db_connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA journal_mode = WAL")
    return con


def init_db() -> None:
    with db_connect() as con:
        con.execute("BEGIN IMMEDIATE")
        con.execute(
            """CREATE TABLE IF NOT EXISTS communication_sessions (
                session_id TEXT PRIMARY KEY,
                topic TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('active','completed','stopped')),
                max_turns INTEGER NOT NULL,
                turn_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )"""
        )
        con.execute(
            """CREATE TABLE IF NOT EXISTS communication_messages (
                message_id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                sender TEXT NOT NULL CHECK(sender IN ('user','chatgpt','claud')),
                content TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(session_id) REFERENCES communication_sessions(session_id)
                    ON DELETE CASCADE
            )"""
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_communication_messages_session "
            "ON communication_messages(session_id, message_id)"
        )
        # Append-only event stream used by the local Live Conversation Room.
        con.execute(
            """CREATE TABLE IF NOT EXISTS communication_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                sender TEXT,
                detail TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(session_id) REFERENCES communication_sessions(session_id)
                    ON DELETE CASCADE
            )"""
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_communication_events_session "
            "ON communication_events(session_id, event_id)"
        )
        # Additive link table: existing sessions/messages are not rewritten.
        con.execute(
            """CREATE TABLE IF NOT EXISTS communication_core_links (
                session_id TEXT PRIMARY KEY,
                core_conversation_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(session_id) REFERENCES communication_sessions(session_id)
                    ON DELETE CASCADE
            )"""
        )
        _apply_schema_migrations(con)


def _apply_schema_migrations(con: sqlite3.Connection) -> None:
    """Apply additive communication database migrations in version order."""
    version = int(con.execute("PRAGMA user_version").fetchone()[0])
    if version > CURRENT_SCHEMA_VERSION:
        raise RuntimeError(
            f"Communication database schema {version} is newer than supported "
            f"schema {CURRENT_SCHEMA_VERSION}."
        )

    if version < 1:
        event_columns = {
            row[1] for row in con.execute("PRAGMA table_info(communication_events)")
        }
        if "source_event_id" not in event_columns:
            con.execute(
                "ALTER TABLE communication_events ADD COLUMN source_event_id TEXT"
            )
        con.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS "
            "idx_communication_events_source_event_id "
            "ON communication_events(source_event_id) "
            "WHERE source_event_id IS NOT NULL"
        )
        con.execute("PRAGMA user_version = 1")
    else:
        event_columns = {
            row[1] for row in con.execute("PRAGMA table_info(communication_events)")
        }
        if "source_event_id" not in event_columns:
            raise RuntimeError(
                "Communication database schema version is inconsistent: "
                "source_event_id is missing."
            )
        con.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS "
            "idx_communication_events_source_event_id "
            "ON communication_events(source_event_id) "
            "WHERE source_event_id IS NOT NULL"
        )


init_db()


def _valid_identifier(value: Any, max_length: int = MAX_EVENT_ID_CHARS) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= max_length
        and value == value.strip()
        and _IDENTIFIER_PATTERN.fullmatch(value) is not None
    )


def _validated_project_relative_path(value: str | None) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_PROJECT_RELATIVE_PATH_CHARS
        or value != value.strip()
        or "\x00" in value
        or any(ord(char) < 32 for char in value)
    ):
        raise ValueError("project_relative_path is malformed or too long.")

    normalized = value.replace("\\", "/")
    windows_path = PureWindowsPath(value)
    parts = normalized.split("/")
    if (
        normalized.startswith("/")
        or windows_path.is_absolute()
        or windows_path.drive
        or ":" in value
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise ValueError("project_relative_path must stay within the ClaudAI project.")

    project_root = ROOT.resolve()
    candidate = project_root.joinpath(*parts).resolve(strict=False)
    try:
        candidate.relative_to(project_root)
    except ValueError as exc:
        raise ValueError(
            "project_relative_path must stay within the ClaudAI project."
        ) from exc
    return "/".join(parts)


@mcp.tool()
def record_codex_activity(
    communication_session_id: str,
    codex_session_id: str,
    codex_turn_id: str,
    tool_use_id: str,
    tool_name: str,
    event_type: Literal["codex_post_tool_use"],
    operation: Literal["read", "write", "execute", "other"],
    status: Literal["observed"],
    project_relative_path: str | None = None,
) -> str:
    """Record sanitized Codex post-tool-use metadata; it does not assert success."""
    identifiers = (
        ("communication_session_id", communication_session_id),
        ("codex_session_id", codex_session_id),
        ("codex_turn_id", codex_turn_id),
        ("tool_use_id", tool_use_id),
    )
    for field_name, value in identifiers:
        if not _valid_identifier(value):
            return f"ERROR: {field_name} is empty or malformed."
    if (
        not isinstance(tool_name, str)
        or not 1 <= len(tool_name) <= MAX_TOOL_NAME_CHARS
        or tool_name != tool_name.strip()
        or _TOOL_NAME_PATTERN.fullmatch(tool_name) is None
    ):
        return "ERROR: tool_name is empty, malformed, or too long."
    if not isinstance(event_type, str) or event_type != "codex_post_tool_use":
        return "ERROR: event_type must be 'codex_post_tool_use'."
    if not isinstance(operation, str) or operation not in {
        "read",
        "write",
        "execute",
        "other",
    }:
        return "ERROR: operation is not supported."
    if not isinstance(status, str) or status != "observed":
        return "ERROR: status must be 'observed'."
    try:
        relative_path = _validated_project_relative_path(project_relative_path)
    except ValueError as exc:
        return f"ERROR: {exc}"

    source_event_id = f"codex:{codex_session_id}:{tool_use_id}"
    timestamp = utc_now()
    metadata = {
        "source_event_id": source_event_id,
        "actor": "codex",
        "communication_session_id": communication_session_id,
        "codex_session_id": codex_session_id,
        "codex_turn_id": codex_turn_id,
        "tool_use_id": tool_use_id,
        "tool_name": tool_name,
        "operation": operation,
        "status": "observed",
        "timestamp": timestamp,
    }
    if relative_path is not None:
        metadata["project_relative_path"] = relative_path
    detail = json.dumps(metadata, ensure_ascii=True, separators=(",", ":"))
    if len(detail) > MAX_ACTIVITY_DETAIL_CHARS:
        return "ERROR: sanitized event metadata exceeds the storage limit."

    identity_fields = (
        "actor",
        "communication_session_id",
        "codex_session_id",
        "codex_turn_id",
        "tool_use_id",
        "tool_name",
        "operation",
        "status",
        "project_relative_path",
    )
    with db_connect() as con:
        con.execute("BEGIN IMMEDIATE")
        session = con.execute(
            "SELECT 1 FROM communication_sessions WHERE session_id = ?",
            (communication_session_id,),
        ).fetchone()
        if session is None:
            return "ERROR: communication session does not exist."

        duplicate = con.execute(
            "SELECT event_id, session_id, event_type, sender, detail "
            "FROM communication_events WHERE source_event_id = ?",
            (source_event_id,),
        ).fetchone()
        if duplicate is not None:
            try:
                prior_metadata = json.loads(duplicate["detail"] or "{}")
            except (TypeError, json.JSONDecodeError):
                prior_metadata = {}
            same_event = (
                duplicate["session_id"] == communication_session_id
                and duplicate["event_type"] == "codex_post_tool_use"
                and duplicate["sender"] == "codex"
                and all(prior_metadata.get(key) == metadata.get(key) for key in identity_fields)
            )
            if not same_event:
                return "ERROR: source event ID is already bound to different metadata."
            return (
                "ALREADY_RECORDED: observed post-tool-use event; "
                f"Event ID: {duplicate['event_id']}"
            )

        cursor = con.execute(
            "INSERT INTO communication_events "
            "(session_id, event_type, sender, detail, created_at, source_event_id) "
            "VALUES (?, 'codex_post_tool_use', 'codex', ?, ?, ?)",
            (communication_session_id, detail, timestamp, source_event_id),
        )
        event_id = int(cursor.lastrowid)
    return (
        "RECORDED: observed post-tool-use event; this does not assert that the "
        f"underlying operation succeeded. Event ID: {event_id}"
    )


@mcp.tool()
def claud_healthcheck() -> str:
    """Check MCP bridge and Claud's actual Core, database, local model, context and orchestrator modules."""
    try:
        report = get_core().healthcheck()
        lines = [
            "STATUS: READY",
            "MCP Bridge: online",
            f"Claud Core version: {report['core_version']}",
            f"Response pipeline: {report['response_pipeline']}",
            f"Ollama: reachable ({report['ollama_url']})",
            f"Core model: {report['model']}",
            f"Core database: {report['database']}",
            f"Memory retrieval: {report['memory_retrieval']}",
            f"Knowledge grounding: {report['knowledge_grounding']}",
            f"Self-critique: {report['self_critique']}",
            f"Context Manager: {report['context_manager']}",
            f"Cognitive Orchestrator: {report['cognitive_orchestrator']}",
            f"Communication database: {DB_PATH}",
            "Note: Orchestrator remains in shadow mode; it does not execute plans.",
        ]
        return "\n".join(lines)
    except Exception as exc:
        logging.exception("Core health check failed")
        return f"STATUS: NOT READY\n{type(exc).__name__}: {exc}"


@mcp.tool()
def claud_start_discussion(topic: str, max_turns: int = 8) -> str:
    """Create a discussion session linked to a separate conversation in Claud's Core database."""
    topic = topic.strip() if isinstance(topic, str) else ""
    if not topic:
        return "ERROR: Topic diskusi tidak boleh kosong."
    if len(topic) > MAX_TOPIC_CHARS:
        return f"ERROR: Topic maksimal {MAX_TOPIC_CHARS} karakter."
    if not isinstance(max_turns, int) or isinstance(max_turns, bool) or not 1 <= max_turns <= 20:
        return "ERROR: max_turns harus berupa bilangan bulat antara 1 dan 20."

    try:
        core_conversation_id = get_core().create_conversation(topic)
    except Exception as exc:
        logging.exception("Could not create Core conversation")
        return f"ERROR: Claud Core belum siap membuat percakapan. {type(exc).__name__}: {exc}"

    session_id = str(uuid.uuid4())
    now = utc_now()
    try:
        with db_connect() as con:
            con.execute(
                "INSERT INTO communication_sessions "
                "(session_id, topic, status, max_turns, turn_count, created_at, updated_at) "
                "VALUES (?, ?, 'active', ?, 0, ?, ?)",
                (session_id, topic, max_turns, now, now),
            )
            con.execute(
                "INSERT INTO communication_core_links "
                "(session_id, core_conversation_id, created_at) VALUES (?, ?, ?)",
                (session_id, core_conversation_id, now),
            )
            con.execute(
                "INSERT INTO communication_events "
                "(session_id, event_type, sender, detail, created_at) "
                "VALUES (?, 'session_started', 'system', ?, ?)",
                (session_id, f"Discussion started: {topic[:300]}", now),
            )
    except Exception:
        logging.exception("Could not persist MCP discussion session")
        raise

    return (
        "SESSION CREATED\n"
        f"Session ID: {session_id}\n"
        f"Core conversation ID: {core_conversation_id}\n"
        f"Topic: {topic}\nMaximum exchanges: {max_turns}\n"
        "Claud processing: existing Core pipeline\n"
        "Next: call claud_send_message with this session_id and the opening message."
    )


@mcp.tool()
def claud_send_message(session_id: str, message: str, sender: str = "chatgpt") -> str:
    """Send a ChatGPT or user message to Claud's existing Core pipeline and wait for its reply."""
    session_id = session_id.strip() if isinstance(session_id, str) else ""
    message = message.strip() if isinstance(message, str) else ""
    sender = sender.strip().lower() if isinstance(sender, str) else ""
    if not session_id or not message:
        return "ERROR: session_id dan message wajib diisi."
    if sender not in {"chatgpt", "user"}:
        return "ERROR: sender hanya boleh 'chatgpt' atau 'user'."
    if len(message) > MAX_MESSAGE_CHARS:
        return f"ERROR: Pesan terlalu panjang. Maksimum {MAX_MESSAGE_CHARS} karakter."

    with db_connect() as con:
        session = con.execute(
            "SELECT * FROM communication_sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        link = con.execute(
            "SELECT core_conversation_id FROM communication_core_links WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if session is None:
            return "ERROR: Session tidak ditemukan. Mulai session baru dengan claud_start_discussion."
        if session["status"] != "active":
            return f"ERROR: Session berstatus {session['status']}. Mulai session baru untuk melanjutkan."
        if session["turn_count"] >= session["max_turns"]:
            return "TURN LIMIT REACHED: Batas pertukaran sesi ini sudah tercapai."
        if link is None:
            return "ERROR: Session lama belum memiliki tautan ke Claud Core. Mulai session baru."
        core_conversation_id = int(link["core_conversation_id"])

    # Write the incoming message and processing events before the potentially slow
    # Core/Ollama call. The local viewer polls this database in read-only mode.
    received_at = utc_now()
    with db_connect() as con:
        con.execute(
            "INSERT INTO communication_messages "
            "(session_id, sender, content, created_at) VALUES (?, ?, ?, ?)",
            (session_id, sender, message, received_at),
        )
        con.execute(
            "INSERT INTO communication_events "
            "(session_id, event_type, sender, detail, created_at) "
            "VALUES (?, 'message_received', ?, ?, ?)",
            (session_id, sender, f"{sender} message received", received_at),
        )
        con.execute(
            "INSERT INTO communication_events "
            "(session_id, event_type, sender, detail, created_at) "
            "VALUES (?, 'claud_thinking', 'claud', ?, ?)",
            (session_id, "Claud Core is processing the message", received_at),
        )
        con.execute(
            "UPDATE communication_sessions SET updated_at = ? WHERE session_id = ?",
            (received_at, session_id),
        )

    try:
        result = get_core().respond(core_conversation_id, message)
        reply = result["answer"]
        if not isinstance(reply, str) or not reply.strip():
            raise RuntimeError("Claud Core tidak mengembalikan jawaban teks.")
    except Exception as exc:
        logging.exception("Core message exchange failed for session %s", session_id)
        failed_at = utc_now()
        with db_connect() as con:
            con.execute(
                "INSERT INTO communication_events "
                "(session_id, event_type, sender, detail, created_at) "
                "VALUES (?, 'claud_error', 'claud', ?, ?)",
                (session_id, f"{type(exc).__name__}: {str(exc)[:500]}", failed_at),
            )
            con.execute(
                "UPDATE communication_sessions SET updated_at = ? WHERE session_id = ?",
                (failed_at, session_id),
            )
        return (
            "CLAUD CORE REPLY FAILED\n"
            f"{type(exc).__name__}: {exc}\n"
            "Pesan masuk sudah tersimpan; balasan Claud belum tersedia. "
            "Periksa Core/Ollama lalu mulai pertukaran baru jika diperlukan."
        )

    now = utc_now()
    with db_connect() as con:
        current = con.execute(
            "SELECT status, turn_count, max_turns FROM communication_sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if current is None or current["status"] != "active":
            return "ERROR: Session dihentikan sebelum pertukaran dapat disimpan ke communication.db."
        if current["turn_count"] >= current["max_turns"]:
            return "TURN LIMIT REACHED: Jawaban Core selesai dibuat tetapi tidak dicatat pada sesi komunikasi."
        con.execute(
            "INSERT INTO communication_messages (session_id, sender, content, created_at) VALUES (?, 'claud', ?, ?)",
            (session_id, reply, now),
        )
        con.execute(
            "INSERT INTO communication_events "
            "(session_id, event_type, sender, detail, created_at) "
            "VALUES (?, 'claud_replied', 'claud', ?, ?)",
            (
                session_id,
                f"Reply completed; route={result.get('route')}; "
                f"self_critique={result.get('critique_status')}",
                now,
            ),
        )
        new_count = current["turn_count"] + 1
        new_status = "completed" if new_count >= current["max_turns"] else "active"
        con.execute(
            "UPDATE communication_sessions SET turn_count = ?, status = ?, updated_at = ? WHERE session_id = ?",
            (new_count, new_status, now, session_id),
        )

    diag = result.get("diagnostics", {})
    return (
        "CLAUD REPLIED\n"
        f"Session: {session_id}\n"
        f"Core conversation: {result.get('core_conversation_id')}\n"
        f"Exchange: {new_count}/{session['max_turns']}\n"
        f"Session status: {new_status}\n"
        f"Core route: {result.get('route')}\n"
        f"Self-critique: {result.get('critique_status')}\n"
        f"Cognitive diagnostics: {diag.get('orchestrator_status', 'unavailable')}\n"
        f"Context diagnostics: {diag.get('context_status', 'unavailable')}\n\n"
        f"CLAUD AI:\n{reply}"
    )


@mcp.tool()
def claud_get_history(session_id: str, limit: int = 30) -> str:
    """Retrieve recent messages from a persistent Claud discussion session."""
    session_id = session_id.strip() if isinstance(session_id, str) else ""
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
        return "ERROR: limit harus berupa bilangan bulat antara 1 dan 100."
    with db_connect() as con:
        session = con.execute(
            "SELECT * FROM communication_sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if session is None:
            return "ERROR: Session tidak ditemukan."
        rows = con.execute(
            "SELECT sender, content, created_at FROM communication_messages "
            "WHERE session_id = ? ORDER BY message_id DESC LIMIT ?",
            (session_id, limit),
        ).fetchall()
    lines = [
        f"Session: {session['session_id']}",
        f"Topic: {session['topic']}",
        f"Status: {session['status']} | Exchanges: {session['turn_count']}/{session['max_turns']}",
        "--- HISTORY (oldest to newest) ---",
    ]
    for row in reversed(rows):
        lines.append(f"[{row['created_at']}] {row['sender'].upper()}:\n{row['content']}")
    return "\n\n".join(lines)


@mcp.tool()
def claud_end_discussion(session_id: str) -> str:
    """Stop an active discussion. The Core conversation and message history are retained."""
    session_id = session_id.strip() if isinstance(session_id, str) else ""
    with db_connect() as con:
        stopped_at = utc_now()
        cur = con.execute(
            "UPDATE communication_sessions SET status = 'stopped', updated_at = ? "
            "WHERE session_id = ? AND status = 'active'",
            (stopped_at, session_id),
        )
        if cur.rowcount > 0:
            con.execute(
                "INSERT INTO communication_events "
                "(session_id, event_type, sender, detail, created_at) "
                "VALUES (?, 'discussion_stopped', 'system', 'Discussion stopped by request', ?)",
                (session_id, stopped_at),
            )
        if cur.rowcount == 0:
            row = con.execute(
                "SELECT status FROM communication_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                return "ERROR: Session tidak ditemukan."
            return f"Session tidak diubah; status saat ini: {row['status']}."
    return f"Discussion stopped. Core and communication history retained for session {session_id}."


register_mentor_tools(mcp, get_core)

if __name__ == "__main__":
    # MCP stdio reserves stdout for protocol traffic; logs go to application log directory.
    mcp.run(transport="stdio")
