from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _connect_core(get_core: Callable[[], Any]) -> sqlite3.Connection:
    core = get_core().core
    db_file = Path(core.DB_FILE)
    if not db_file.is_file():
        raise FileNotFoundError(f"Claud Core database not found: {db_file}")
    con = sqlite3.connect(str(db_file), timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout = 30000")
    return con


def _ensure_schema(con: sqlite3.Connection) -> None:
    con.execute(
        """CREATE TABLE IF NOT EXISTS mentor_learning_proposals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            topic TEXT NOT NULL,
            principle TEXT NOT NULL,
            rationale TEXT NOT NULL,
            example TEXT NOT NULL DEFAULT '',
            limitations TEXT NOT NULL DEFAULT '',
            validation_test TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending_review'
                CHECK(status IN ('pending_review','approved','rejected','revision_requested')),
            source TEXT NOT NULL DEFAULT 'chatgpt_mentor',
            reviewer_notes TEXT NOT NULL DEFAULT '',
            core_memory_id INTEGER,
            created_at TEXT NOT NULL,
            reviewed_at TEXT
        )"""
    )
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_mentor_learning_status "
        "ON mentor_learning_proposals(status, id DESC)"
    )


def register_mentor_tools(mcp: Any, get_core: Callable[[], Any]) -> None:
    """Register audited, approval-gated persistent learning tools on the local MCP server."""

    @mcp.tool()
    def claud_learning_status() -> str:
        """Show Claud's persistent mentor-learning status: pending proposals, approved lessons and stored learning memories."""
        try:
            with _connect_core(get_core) as con:
                _ensure_schema(con)
                counts = {
                    row["status"]: row["total"]
                    for row in con.execute(
                        "SELECT status, COUNT(*) AS total FROM mentor_learning_proposals GROUP BY status"
                    ).fetchall()
                }
                latest = con.execute(
                    "SELECT id, topic, status, created_at FROM mentor_learning_proposals ORDER BY id DESC LIMIT 5"
                ).fetchall()
                memory_count = con.execute(
                    "SELECT COUNT(*) FROM memories WHERE content LIKE ?", ("[CLAUD MENTOR LEARNING%",)
                ).fetchone()[0]
            lines = [
                "CLAUD MENTOR LEARNING STATUS",
                f"Pending review: {counts.get('pending_review', 0)}",
                f"Approved lessons: {counts.get('approved', 0)}",
                f"Rejected: {counts.get('rejected', 0)}",
                f"Revision requested: {counts.get('revision_requested', 0)}",
                f"Persistent mentor memories: {memory_count}",
                "Recent proposals:",
            ]
            lines.extend(
                f"- ID {r['id']} | {r['status']} | {r['topic']} | {r['created_at']}"
                for r in latest
            )
            return "\n".join(lines)
        except Exception as exc:
            return f"ERROR: learning status unavailable: {type(exc).__name__}: {exc}"

    @mcp.tool()
    def claud_propose_learning(
        topic: str,
        principle: str,
        rationale: str,
        example: str = "",
        limitations: str = "",
        validation_test: str = "",
    ) -> str:
        """Save a proposed lesson as PENDING REVIEW. Does not change Core memory until the user explicitly approves it."""
        fields = {
            "topic": (topic, 200),
            "principle": (principle, 3000),
            "rationale": (rationale, 4000),
            "example": (example, 3000),
            "limitations": (limitations, 3000),
            "validation_test": (validation_test, 2000),
        }
        normalized: dict[str, str] = {}
        for name, (value, maximum) in fields.items():
            if not isinstance(value, str):
                return f"ERROR: {name} must be text."
            value = value.strip()
            if name in {"topic", "principle", "rationale"} and not value:
                return f"ERROR: {name} cannot be empty."
            if len(value) > maximum:
                return f"ERROR: {name} exceeds {maximum} characters."
            normalized[name] = value

        try:
            with _connect_core(get_core) as con:
                _ensure_schema(con)
                existing = con.execute(
                    """SELECT id, status FROM mentor_learning_proposals
                       WHERE LOWER(topic)=LOWER(?) AND LOWER(principle)=LOWER(?)
                         AND status IN ('pending_review','approved')
                       ORDER BY id DESC LIMIT 1""",
                    (normalized["topic"], normalized["principle"]),
                ).fetchone()
                if existing:
                    return (
                        f"EXISTING LEARNING RECORD: ID {existing['id']} "
                        f"({existing['status']}). No duplicate created."
                    )
                cur = con.execute(
                    """INSERT INTO mentor_learning_proposals
                       (topic, principle, rationale, example, limitations,
                        validation_test, status, source, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, 'pending_review', 'chatgpt_mentor', ?)""",
                    (
                        normalized["topic"], normalized["principle"],
                        normalized["rationale"], normalized["example"],
                        normalized["limitations"], normalized["validation_test"], _now(),
                    ),
                )
                proposal_id = cur.lastrowid
            return (
                "LEARNING PROPOSAL SAVED — PENDING REVIEW\n"
                f"Proposal ID: {proposal_id}\nTopic: {normalized['topic']}\n"
                "This is not yet part of Claud's persistent Core memory. "
                "Ask the user to review it. Do not approve it without explicit user authorization."
            )
        except Exception as exc:
            return f"ERROR: could not save learning proposal: {type(exc).__name__}: {exc}"

    @mcp.tool()
    def claud_list_learning_proposals(status: str = "pending_review", limit: int = 10) -> str:
        """List mentor-learning proposals. status may be pending_review, approved, rejected, revision_requested, or all."""
        allowed = {"pending_review", "approved", "rejected", "revision_requested", "all"}
        if status not in allowed:
            return "ERROR: invalid status filter."
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 50:
            return "ERROR: limit must be an integer from 1 to 50."
        try:
            with _connect_core(get_core) as con:
                _ensure_schema(con)
                if status == "all":
                    rows = con.execute(
                        "SELECT * FROM mentor_learning_proposals ORDER BY id DESC LIMIT ?", (limit,)
                    ).fetchall()
                else:
                    rows = con.execute(
                        "SELECT * FROM mentor_learning_proposals WHERE status=? ORDER BY id DESC LIMIT ?",
                        (status, limit),
                    ).fetchall()
            if not rows:
                return f"No learning proposals found for status={status}."
            chunks = []
            for r in rows:
                chunks.append(
                    f"ID: {r['id']} | STATUS: {r['status']} | TOPIC: {r['topic']}\n"
                    f"Principle: {r['principle']}\nRationale: {r['rationale']}\n"
                    f"Example: {r['example'] or '(none)'}\n"
                    f"Limitations: {r['limitations'] or '(none)'}\n"
                    f"Validation test: {r['validation_test'] or '(none)'}\n"
                    f"Reviewer notes: {r['reviewer_notes'] or '(none)'}\n"
                    f"Core memory ID: {r['core_memory_id']}"
                )
            return "\n\n---\n\n".join(chunks)
        except Exception as exc:
            return f"ERROR: could not list learning proposals: {type(exc).__name__}: {exc}"

    @mcp.tool()
    def claud_review_learning(
        proposal_id: int,
        decision: str,
        reviewer_notes: str = "",
        user_confirmation: str = "",
    ) -> str:
        """Review a lesson as approved, rejected, or revision_requested. Approval requires the user's exact confirmation phrase: APPROVE THIS LEARNING."""
        if not isinstance(proposal_id, int) or isinstance(proposal_id, bool) or proposal_id <= 0:
            return "ERROR: proposal_id must be a positive integer."
        decision = decision.strip().lower() if isinstance(decision, str) else ""
        if decision not in {"approved", "rejected", "revision_requested"}:
            return "ERROR: decision must be approved, rejected, or revision_requested."
        reviewer_notes = reviewer_notes.strip() if isinstance(reviewer_notes, str) else ""
        if len(reviewer_notes) > 3000:
            return "ERROR: reviewer_notes exceeds 3000 characters."
        if decision == "approved" and (not isinstance(user_confirmation, str) or user_confirmation.strip() != "APPROVE THIS LEARNING"):
            return (
                "APPROVAL REQUIRED: Do not save this lesson to Core memory yet. "
                "The user must explicitly send the exact phrase: APPROVE THIS LEARNING."
            )

        try:
            with _connect_core(get_core) as con:
                _ensure_schema(con)
                row = con.execute(
                    "SELECT * FROM mentor_learning_proposals WHERE id=?", (proposal_id,)
                ).fetchone()
                if row is None:
                    return "ERROR: learning proposal not found."
                if row["status"] == "approved":
                    return f"Proposal {proposal_id} is already approved; Core memory ID: {row['core_memory_id']}."
                if row["status"] in {"rejected"}:
                    return f"Proposal {proposal_id} is already rejected; create a new proposal if needed."

                memory_id = None
                if decision == "approved":
                    memory_content = (
                        f"[CLAUD MENTOR LEARNING | proposal_id={proposal_id}]\n"
                        f"Topic: {row['topic']}\n"
                        f"Approved operational principle: {row['principle']}\n"
                        f"Rationale: {row['rationale']}\n"
                        f"Example: {row['example']}\n"
                        f"Limitations: {row['limitations']}\n"
                        f"Validation test: {row['validation_test']}\n"
                        "Provenance: lesson proposed during ChatGPT mentor session and explicitly approved by the user. "
                        "This is an approved learning guideline, not proof of a universal factual claim."
                    )
                    existing = con.execute(
                        "SELECT id FROM memories WHERE LOWER(content)=LOWER(?)",
                        (memory_content,),
                    ).fetchone()
                    if existing:
                        memory_id = int(existing["id"])
                    else:
                        cur = con.execute(
                            "INSERT INTO memories (content, created_at) VALUES (?, ?)",
                            (memory_content, _now()),
                        )
                        memory_id = int(cur.lastrowid)

                con.execute(
                    """UPDATE mentor_learning_proposals
                       SET status=?, reviewer_notes=?, core_memory_id=?, reviewed_at=?
                       WHERE id=?""",
                    (decision, reviewer_notes, memory_id, _now(), proposal_id),
                )
            if decision == "approved":
                return (
                    f"LEARNING APPROVED AND COMMITTED. Proposal ID: {proposal_id}. "
                    f"Core memory ID: {memory_id}. The record is now available to Claud's existing memory retrieval. "
                    "It remains a user-approved guideline, not an independently verified fact."
                )
            return f"Proposal {proposal_id} updated: {decision}. No Core memory was changed."
        except Exception as exc:
            return f"ERROR: learning review failed: {type(exc).__name__}: {exc}"
