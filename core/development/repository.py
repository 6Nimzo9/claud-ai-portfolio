import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .models import (
    TaskSpec, TaskStatus, DevelopmentPlan, ExecutionStep
)


class DevelopmentRepository:
    """SQLite persistence for Claud AI Development Engine."""

    def __init__(self, db_path=None):
        if db_path is None:
            project_root = Path(__file__).resolve().parents[2]
            db_path = project_root / "database" / "development_engine.db"

        self.db_path = Path(db_path).expanduser().resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    @contextmanager
    def _connection(self):
        conn = sqlite3.connect(str(self.db_path), timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 15000")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def initialize(self):
        with self._connection() as conn:
            conn.execute("PRAGMA journal_mode = WAL")

            conn.executescript("""
                CREATE TABLE IF NOT EXISTS development_tasks (
                    task_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    objective TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS development_plans (
                    plan_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL UNIQUE,
                    objective TEXT NOT NULL,
                    version TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(task_id)
                        REFERENCES development_tasks(task_id)
                        ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS development_steps (
                    step_id TEXT PRIMARY KEY,
                    plan_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL,
                    status TEXT NOT NULL,
                    FOREIGN KEY(plan_id)
                        REFERENCES development_plans(plan_id)
                        ON DELETE CASCADE,
                    UNIQUE(plan_id, sequence)
                );

                CREATE TABLE IF NOT EXISTS development_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    from_status TEXT,
                    to_status TEXT,
                    details TEXT,
                    created_at TEXT NOT NULL DEFAULT (
                        strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                    ),
                    FOREIGN KEY(task_id)
                        REFERENCES development_tasks(task_id)
                        ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_dev_tasks_status
                    ON development_tasks(status);

                CREATE INDEX IF NOT EXISTS idx_dev_events_task
                    ON development_events(task_id, event_id);
            """)

    def save_new_task(self, task, plan, events):
        with self._connection() as conn:
            conn.execute("""
                INSERT INTO development_tasks
                (task_id, title, objective, status, created_at)
                VALUES (?, ?, ?, ?, ?)
            """, (
                task.task_id, task.title, task.objective,
                task.status.value, task.created_at
            ))

            conn.execute("""
                INSERT INTO development_plans
                (plan_id, task_id, objective, version, created_at)
                VALUES (?, ?, ?, ?, ?)
            """, (
                plan.plan_id, plan.task_id, plan.objective,
                plan.version, plan.created_at
            ))

            for step in plan.steps:
                conn.execute("""
                    INSERT INTO development_steps
                    (step_id, plan_id, sequence, name, description, status)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (
                    step.step_id, plan.plan_id, step.sequence,
                    step.name, step.description, step.status
                ))

            for event in events:
                self._insert_event(conn, task.task_id, event)

    @staticmethod
    def _insert_event(conn, task_id, event):
        details = event.get("details")
        if details is not None:
            details = json.dumps(details, ensure_ascii=False)

        conn.execute("""
            INSERT INTO development_events
            (task_id, event_type, from_status, to_status, details)
            VALUES (?, ?, ?, ?, ?)
        """, (
            task_id,
            event["event_type"],
            event.get("from_status"),
            event.get("to_status"),
            details
        ))

    def update_task(self, task, event):
        with self._connection() as conn:
            cursor = conn.execute("""
                UPDATE development_tasks
                SET title = ?, objective = ?, status = ?
                WHERE task_id = ?
            """, (
                task.title, task.objective,
                task.status.value, task.task_id
            ))

            if cursor.rowcount != 1:
                raise KeyError(f"Task not found in database: {task.task_id}")

            self._insert_event(conn, task.task_id, event)

    def get_task(self, task_id):
        with self._connection() as conn:
            row = conn.execute("""
                SELECT * FROM development_tasks WHERE task_id = ?
            """, (task_id,)).fetchone()

        if row is None:
            raise KeyError(f"Task not found: {task_id}")

        return self._task_from_row(row)

    def get_plan(self, task_id):
        with self._connection() as conn:
            plan_row = conn.execute("""
                SELECT * FROM development_plans WHERE task_id = ?
            """, (task_id,)).fetchone()

            if plan_row is None:
                raise KeyError(f"Plan not found: {task_id}")

            step_rows = conn.execute("""
                SELECT * FROM development_steps
                WHERE plan_id = ?
                ORDER BY sequence
            """, (plan_row["plan_id"],)).fetchall()

        steps = [
            ExecutionStep(
                sequence=row["sequence"],
                name=row["name"],
                description=row["description"],
                step_id=row["step_id"],
                status=row["status"]
            )
            for row in step_rows
        ]

        return DevelopmentPlan(
            task_id=plan_row["task_id"],
            objective=plan_row["objective"],
            steps=steps,
            plan_id=plan_row["plan_id"],
            created_at=plan_row["created_at"],
            version=plan_row["version"]
        )

    def list_tasks(self):
        with self._connection() as conn:
            rows = conn.execute("""
                SELECT * FROM development_tasks
                ORDER BY created_at DESC
            """).fetchall()

        return [self._task_from_row(row) for row in rows]

    def list_events(self, task_id):
        with self._connection() as conn:
            rows = conn.execute("""
                SELECT * FROM development_events
                WHERE task_id = ?
                ORDER BY event_id
            """, (task_id,)).fetchall()

        return [
            {
                "event_id": row["event_id"],
                "task_id": row["task_id"],
                "event_type": row["event_type"],
                "from_status": row["from_status"],
                "to_status": row["to_status"],
                "details": (
                    json.loads(row["details"])
                    if row["details"] else None
                ),
                "created_at": row["created_at"]
            }
            for row in rows
        ]

    def load_all(self):
        tasks = self.list_tasks()
        plans = {}

        for task in tasks:
            try:
                plans[task.task_id] = self.get_plan(task.task_id)
            except KeyError:
                continue

        return tasks, plans

    @staticmethod
    def _task_from_row(row):
        return TaskSpec(
            title=row["title"],
            objective=row["objective"],
            task_id=row["task_id"],
            status=TaskStatus(row["status"]),
            created_at=row["created_at"]
        )
