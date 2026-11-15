"""Additive SQLite persistence for UGFE using the existing memory database."""

import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import Evidence, Goal, GoalState
from .safety import redact, safe_summary


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _tokens(text: str) -> set[str]:
    stop = {"the", "and", "for", "with", "from", "that", "this", "what", "how", "about", "goal", "please", "into", "your"}
    return {word for word in re.findall(r"[a-z0-9]{3,}", text.casefold()) if word not in stop}


class GoalRepository:
    """Stores UGFE records in an existing SQLite file without replacing its data."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path).expanduser().resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    @contextmanager
    def _connection(self):
        con = sqlite3.connect(str(self.db_path), timeout=15)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON")
        con.execute("PRAGMA busy_timeout = 15000")
        try:
            with con:
                yield con
        finally:
            con.close()

    def initialize(self) -> None:
        with self._connection() as con:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS ugfe_goals (
                    goal_id TEXT PRIMARY KEY,
                    state TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    goal_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS ugfe_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    goal_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    from_state TEXT,
                    to_state TEXT,
                    details_json TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(goal_id) REFERENCES ugfe_goals(goal_id)
                );
                CREATE TABLE IF NOT EXISTS ugfe_actions (
                    action_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    goal_id TEXT NOT NULL,
                    step_id TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    input_summary TEXT NOT NULL,
                    risk_tier TEXT NOT NULL,
                    result_summary TEXT,
                    outcome TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL,
                    FOREIGN KEY(goal_id) REFERENCES ugfe_goals(goal_id)
                );
                CREATE TABLE IF NOT EXISTS knowledge (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    category TEXT NOT NULL,
                    content TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'unverified',
                    source TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS ugfe_evidence (
                    evidence_id TEXT PRIMARY KEY,
                    knowledge_id INTEGER NOT NULL,
                    topic TEXT NOT NULL,
                    title TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    publisher TEXT,
                    retrieved_at TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    independently_corroborated INTEGER NOT NULL,
                    classification TEXT NOT NULL,
                    acceptance_verified_at TEXT NOT NULL,
                    UNIQUE(source_id, content_hash),
                    FOREIGN KEY(knowledge_id) REFERENCES knowledge(id)
                );
                CREATE TABLE IF NOT EXISTS ugfe_knowledge_refs (
                    goal_id TEXT NOT NULL,
                    evidence_id TEXT NOT NULL,
                    linked_at TEXT NOT NULL,
                    PRIMARY KEY(goal_id, evidence_id),
                    FOREIGN KEY(goal_id) REFERENCES ugfe_goals(goal_id),
                    FOREIGN KEY(evidence_id) REFERENCES ugfe_evidence(evidence_id)
                );
                CREATE INDEX IF NOT EXISTS idx_ugfe_goals_updated ON ugfe_goals(updated_at);
                CREATE INDEX IF NOT EXISTS idx_ugfe_events_goal ON ugfe_events(goal_id, event_id);
            """)

    def save_goal(self, goal: Goal) -> None:
        document = json.dumps(
            goal.to_dict(include_evidence_text=goal.state != GoalState.COMPLETED),
            ensure_ascii=False, sort_keys=True,
        )
        with self._connection() as con:
            con.execute("""
                INSERT INTO ugfe_goals(goal_id, state, created_at, updated_at, goal_json)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(goal_id) DO UPDATE SET state=excluded.state,
                    updated_at=excluded.updated_at, goal_json=excluded.goal_json
            """, (goal.goal_id, goal.state.value, goal.created_at, goal.updated_at, document))

    def add_event(self, goal_id: str, event_type: str, from_state: str | None,
                  to_state: str | None, details: Any = None) -> None:
        clean_details = redact(details or {})
        rendered = json.dumps(clean_details, ensure_ascii=False, sort_keys=True, default=str)
        if len(rendered) > 10_000:
            rendered = json.dumps({"summary": safe_summary(clean_details, 1000)}, ensure_ascii=False)
        with self._connection() as con:
            con.execute("""
                INSERT INTO ugfe_events(goal_id,event_type,from_state,to_state,details_json,created_at)
                VALUES(?,?,?,?,?,?)
            """, (goal_id, event_type, from_state, to_state, rendered, _now()))

    def add_action(self, *, goal_id: str, step_id: str, tool_name: str,
                   input_summary: str, risk_tier: str, result_summary: str | None,
                   outcome: str, started_at: str, finished_at: str) -> None:
        with self._connection() as con:
            con.execute("""
                INSERT INTO ugfe_actions(goal_id,step_id,tool_name,input_summary,risk_tier,
                    result_summary,outcome,started_at,finished_at)
                VALUES(?,?,?,?,?,?,?,?,?)
            """, (goal_id, step_id, tool_name, safe_summary(input_summary), risk_tier,
                  safe_summary(result_summary) if result_summary is not None else None,
                  outcome, started_at, finished_at))

    def save_verified_evidence(self, goal_id: str, items: list[Evidence]) -> None:
        with self._connection() as con:
            for item in items:
                digest = item.content_hash or hashlib.sha256(item.excerpt.encode("utf-8")).hexdigest()
                row = con.execute("SELECT evidence_id,knowledge_id FROM ugfe_evidence WHERE source_id=? AND content_hash=?",
                                  (item.source_id, digest)).fetchone()
                if row:
                    evidence_id = row[0]
                    knowledge_id = row[1]
                else:
                    knowledge = con.execute(
                        "SELECT id FROM knowledge WHERE LOWER(TRIM(content))=LOWER(TRIM(?)) ORDER BY id LIMIT 1",
                        (item.excerpt,),
                    ).fetchone()
                    if knowledge:
                        knowledge_id = knowledge[0]
                    else:
                        now = _now()
                        cursor = con.execute("""
                            INSERT INTO knowledge(category,content,status,source,created_at,updated_at)
                            VALUES(?,?,'unverified',?,?,?)
                        """, (item.classification, item.excerpt, item.source_id, now, now))
                        knowledge_id = cursor.lastrowid
                    evidence_id = item.evidence_id
                con.execute("""
                    INSERT OR IGNORE INTO ugfe_evidence(
                        evidence_id,knowledge_id,topic,title,source_id,provider,publisher,
                        retrieved_at,content_hash,confidence,independently_corroborated,
                        classification,acceptance_verified_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (evidence_id, knowledge_id, item.topic, item.title, item.source_id,
                      item.provider, item.publisher, item.retrieved_at, digest, item.confidence,
                      int(item.independently_corroborated), item.classification, _now()))
                con.execute("INSERT OR IGNORE INTO ugfe_knowledge_refs(goal_id,evidence_id,linked_at) VALUES(?,?,?)",
                            (goal_id, evidence_id, _now()))

    def search_knowledge(self, topic: str, limit: int = 5) -> list[Evidence]:
        query_tokens = _tokens(topic)
        if not query_tokens:
            return []
        with self._connection() as con:
            rows = con.execute("""
                SELECT e.*, k.content FROM ugfe_evidence e
                JOIN knowledge k ON k.id=e.knowledge_id
                ORDER BY e.acceptance_verified_at DESC
            """).fetchall()
        ranked = []
        for row in rows:
            searchable = " ".join((row["topic"], row["title"], row["content"]))
            overlap = len(query_tokens & _tokens(searchable))
            threshold = 1 if len(query_tokens) == 1 else max(2, (len(query_tokens) + 2) // 3)
            if overlap >= threshold:
                ranked.append((overlap, row["acceptance_verified_at"], row))
        ranked.sort(key=lambda x: (x[0], x[1]), reverse=True)
        return [Evidence(
            title=r["title"], source_id=r["source_id"], provider=r["provider"],
            publisher=r["publisher"], retrieved_at=r["retrieved_at"], excerpt=r["content"],
            confidence=r["confidence"], independently_corroborated=bool(r["independently_corroborated"]),
            classification=r["classification"], content_hash=r["content_hash"], topic=r["topic"],
            evidence_id=r["evidence_id"],
        ) for _, _, r in ranked[:limit]]

    def inspect_existing_knowledge(self, topic: str, limit: int = 8) -> list[dict[str, Any]]:
        """Return lexical matches from existing knowledge and memories as references, not facts."""
        query_tokens = _tokens(topic)
        if not query_tokens:
            return []
        results = []
        with self._connection() as con:
            for table, id_col, fields in (
                ("knowledge", "id", "category,status,source,content"),
                ("memories", "id", "content"),
            ):
                try:
                    rows = con.execute(f"SELECT {id_col},{fields} FROM {table} ORDER BY {id_col} DESC").fetchall()
                except sqlite3.Error:
                    continue
                for row in rows:
                    content = row["content"]
                    overlap = query_tokens & _tokens(content)
                    threshold = 1 if len(query_tokens) == 1 else max(2, (len(query_tokens) + 2) // 3)
                    if len(overlap) < threshold:
                        continue
                    if table == "knowledge":
                        results.append({"record_type": table, "record_id": row["id"],
                                        "category": row["category"], "status": row["status"],
                                        "source": row["source"], "matched_terms": sorted(overlap)})
                    else:
                        results.append({"record_type": table, "record_id": row["id"],
                                        "category": "user_memory", "status": "context_only",
                                        "source": "existing memory store", "matched_terms": sorted(overlap)})
        results.sort(key=lambda item: (len(item["matched_terms"]), item["record_id"]), reverse=True)
        return results[:limit]

    def get_goal(self, goal_id: str) -> Goal:
        with self._connection() as con:
            row = con.execute("SELECT goal_json FROM ugfe_goals WHERE goal_id=?", (goal_id,)).fetchone()
        if row is None:
            raise KeyError(f"Unknown goal: {goal_id}")
        return self._hydrate_evidence(Goal.from_dict(json.loads(row[0])))

    def list_goals(self, limit: int = 50) -> list[Goal]:
        with self._connection() as con:
            rows = con.execute("SELECT goal_json FROM ugfe_goals ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
        return [self._hydrate_evidence(Goal.from_dict(json.loads(row[0]))) for row in rows]

    def list_recovery_goals(self) -> list[Goal]:
        """Return persisted goals that have not reached a terminal state."""
        with self._connection() as con:
            rows = con.execute(
                "SELECT goal_json FROM ugfe_goals WHERE state NOT IN (?, ?) ORDER BY updated_at DESC",
                (GoalState.COMPLETED.value, GoalState.BLOCKED.value),
            ).fetchall()
        goals = [self._hydrate_evidence(Goal.from_dict(json.loads(row[0]))) for row in rows]
        return [goal for goal in goals if goal.state not in {GoalState.COMPLETED, GoalState.BLOCKED}]

    def _hydrate_evidence(self, goal: Goal) -> Goal:
        missing = [item for item in goal.evidence if not item.excerpt]
        if not missing:
            return goal
        with self._connection() as con:
            for item in missing:
                row = con.execute("""
                    SELECT k.content FROM ugfe_evidence e
                    JOIN knowledge k ON k.id=e.knowledge_id
                    WHERE e.evidence_id=?
                """, (item.evidence_id,)).fetchone()
                if row:
                    item.excerpt = row[0]
        return goal

    def list_events(self, goal_id: str) -> list[dict[str, Any]]:
        with self._connection() as con:
            rows = con.execute("SELECT * FROM ugfe_events WHERE goal_id=? ORDER BY event_id", (goal_id,)).fetchall()
        return [{"event_id": r["event_id"], "event_type": r["event_type"],
                 "from_state": r["from_state"], "to_state": r["to_state"],
                 "details": json.loads(r["details_json"] or "{}"), "created_at": r["created_at"]} for r in rows]

    def list_actions(self, goal_id: str) -> list[dict[str, Any]]:
        with self._connection() as con:
            rows = con.execute("SELECT * FROM ugfe_actions WHERE goal_id=? ORDER BY action_id", (goal_id,)).fetchall()
        return [dict(row) for row in rows]
