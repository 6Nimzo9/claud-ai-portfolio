import importlib.util
import json
import logging
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing, contextmanager
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[2]
COMMUNICATION_DIR = PROJECT_ROOT / "core" / "communication"
SERVER_PATH = COMMUNICATION_DIR / "mcp_server.py"
sys.dont_write_bytecode = True
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(COMMUNICATION_DIR) not in sys.path:
    sys.path.insert(0, str(COMMUNICATION_DIR))

# mcp_server.py initializes its database on import. Redirect that initialization
# to a temporary database so importing the module never opens the production DB.
_bootstrap_temp = tempfile.TemporaryDirectory(prefix="codex_activity_import_")
_bootstrap_db = Path(_bootstrap_temp.name) / "communication.db"
_sqlite_connect = sqlite3.connect
_bootstrap_connections = []


def _redirect_production_connect(database, *args, **kwargs):
    normalized = str(database).replace("\\", "/").lower()
    is_bootstrap_connection = normalized.endswith("/database/communication.db")
    if is_bootstrap_connection:
        database = str(_bootstrap_db)
    connection = _sqlite_connect(database, *args, **kwargs)
    if is_bootstrap_connection:
        _bootstrap_connections.append(connection)
    return connection


_module_spec = importlib.util.spec_from_file_location(
    "_codex_activity_test_mcp_server", SERVER_PATH
)
if _module_spec is None or _module_spec.loader is None:
    raise RuntimeError("Could not load the communication MCP server for tests.")
mcp_server = importlib.util.module_from_spec(_module_spec)
sys.modules[_module_spec.name] = mcp_server
with mock.patch.object(sqlite3, "connect", side_effect=_redirect_production_connect):
    with mock.patch.object(logging, "basicConfig"):
        _module_spec.loader.exec_module(mcp_server)

# sqlite3.Connection.__exit__ commits or rolls back; it does not close the handle.
for _bootstrap_connection in list(_bootstrap_connections):
    _bootstrap_connection.close()
_bootstrap_connections.clear()
mcp_server.DB_PATH = _bootstrap_db

# Track and deterministically close every connection opened through the server helper.
_original_db_connect = mcp_server.db_connect
_open_test_connections = []


@contextmanager
def _tracked_db_connect():
    connection = _original_db_connect()
    _open_test_connections.append(connection)
    try:
        # Preserve sqlite3.Connection context semantics (commit/rollback),
        # then close the handle explicitly for Windows temporary DB cleanup.
        with connection:
            yield connection
    finally:
        try:
            connection.close()
        finally:
            try:
                _open_test_connections.remove(connection)
            except ValueError:
                pass


unittest.addModuleCleanup(_bootstrap_temp.cleanup)


class TestCodexActivityIngestion(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="codex_activity_test_")
        self.database_path = Path(self.temp_dir.name) / "communication.db"
        self.original_db_path = mcp_server.DB_PATH
        self.original_db_connect = mcp_server.db_connect
        self.addCleanup(self._cleanup_test_resources)

        mcp_server.db_connect = _tracked_db_connect
        mcp_server.DB_PATH = self.database_path
        mcp_server.init_db()
        with mcp_server.db_connect() as con:
            con.execute(
                "INSERT INTO communication_sessions "
                "(session_id, topic, status, max_turns, turn_count, created_at, updated_at) "
                "VALUES (?, ?, 'active', 8, 0, ?, ?)",
                ("session_test_1", "test session", "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
            )

    def _cleanup_test_resources(self):
        close_errors = []
        for connection in list(_open_test_connections):
            try:
                connection.close()
            except Exception as exc:
                close_errors.append(exc)
            finally:
                try:
                    _open_test_connections.remove(connection)
                except ValueError:
                    pass

        mcp_server.DB_PATH = self.original_db_path
        mcp_server.db_connect = self.original_db_connect
        self.temp_dir.cleanup()

        if close_errors:
            raise RuntimeError(
                "One or more SQLite test connections could not be closed."
            ) from close_errors[0]

    def event_args(self, **overrides):
        args = {
            "communication_session_id": "session_test_1",
            "codex_session_id": "codex_session_123",
            "codex_turn_id": "turn_456",
            "tool_use_id": "call_789",
            "tool_name": "apply_patch",
            "event_type": "codex_post_tool_use",
            "operation": "write",
            "status": "observed",
            "project_relative_path": "workspace/example.txt",
        }
        args.update(overrides)
        return args

    def read_events(self):
        with mcp_server.db_connect() as con:
            return con.execute(
                "SELECT event_id, session_id, event_type, sender, detail, created_at, source_event_id "
                "FROM communication_events ORDER BY event_id"
            ).fetchall()

    def test_valid_event_insertion(self):
        result = mcp_server.record_codex_activity(**self.event_args())
        self.assertIn("RECORDED: observed post-tool-use event", result)
        self.assertIn("does not assert", result)
        rows = self.read_events()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["session_id"], "session_test_1")
        self.assertEqual(row["event_type"], "codex_post_tool_use")
        self.assertEqual(row["sender"], "codex")
        self.assertEqual(row["source_event_id"], "codex:codex_session_123:call_789")
        metadata = json.loads(row["detail"])
        self.assertEqual(metadata["actor"], "codex")
        self.assertEqual(metadata["status"], "observed")
        self.assertEqual(metadata["project_relative_path"], "workspace/example.txt")
        self.assertEqual(metadata["timestamp"], row["created_at"])

    def test_unknown_communication_session_rejected(self):
        result = mcp_server.record_codex_activity(
            **self.event_args(communication_session_id="missing_session")
        )
        self.assertEqual(result, "ERROR: communication session does not exist.")
        self.assertEqual(self.read_events(), [])

    def test_missing_identifier_rejected(self):
        for field in (
            "communication_session_id",
            "codex_session_id",
            "codex_turn_id",
            "tool_use_id",
        ):
            with self.subTest(field=field):
                result = mcp_server.record_codex_activity(**self.event_args(**{field: ""}))
                self.assertIn(f"ERROR: {field} is empty or malformed.", result)
        self.assertEqual(self.read_events(), [])

    def test_path_traversal_rejected(self):
        result = mcp_server.record_codex_activity(
            **self.event_args(project_relative_path="..\\outside.txt")
        )
        self.assertIn("must stay within the ClaudAI project", result)
        self.assertEqual(self.read_events(), [])

    def test_absolute_path_rejected(self):
        for path in ("D:\\ClaudAI\\core\\main.py", "/etc/passwd", "\\\\server\\share\\x"):
            with self.subTest(path=path):
                result = mcp_server.record_codex_activity(
                    **self.event_args(project_relative_path=path)
                )
                self.assertIn("must stay within the ClaudAI project", result)
        self.assertEqual(self.read_events(), [])

    def test_field_length_limits(self):
        long_tool = mcp_server.record_codex_activity(
            **self.event_args(tool_name="a" * (mcp_server.MAX_TOOL_NAME_CHARS + 1))
        )
        long_id = mcp_server.record_codex_activity(
            **self.event_args(tool_use_id="a" * (mcp_server.MAX_EVENT_ID_CHARS + 1))
        )
        long_path = mcp_server.record_codex_activity(
            **self.event_args(
                project_relative_path="a" * (mcp_server.MAX_PROJECT_RELATIVE_PATH_CHARS + 1)
            )
        )
        self.assertIn("tool_name is empty, malformed, or too long", long_tool)
        self.assertIn("tool_use_id is empty or malformed", long_id)
        self.assertIn("malformed or too long", long_path)
        self.assertEqual(self.read_events(), [])

    def test_duplicate_event_is_idempotent(self):
        args = self.event_args()
        first = mcp_server.record_codex_activity(**args)
        second = mcp_server.record_codex_activity(**args)
        self.assertIn("RECORDED:", first)
        self.assertIn("ALREADY_RECORDED:", second)
        self.assertEqual(first.rsplit("Event ID: ", 1)[1], second.rsplit("Event ID: ", 1)[1])
        self.assertEqual(len(self.read_events()), 1)

    def test_existing_legacy_event_remains_readable_after_migration(self):
        legacy_path = Path(self.temp_dir.name) / "legacy_communication.db"
        with closing(sqlite3.connect(legacy_path)) as legacy:
            legacy.executescript(
                """
                CREATE TABLE communication_sessions (
                    session_id TEXT PRIMARY KEY,
                    topic TEXT NOT NULL,
                    status TEXT NOT NULL,
                    max_turns INTEGER NOT NULL,
                    turn_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE communication_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    sender TEXT,
                    detail TEXT,
                    created_at TEXT NOT NULL
                );
                INSERT INTO communication_sessions VALUES
                    ('legacy_session', 'legacy', 'active', 8, 0, 't0', 't0');
                INSERT INTO communication_events
                    (session_id, event_type, sender, detail, created_at)
                    VALUES ('legacy_session', 'legacy_event', 'system', 'preserve me', 't1');
                """
            )
        mcp_server.DB_PATH = legacy_path

        mcp_server.init_db()

        with mcp_server.db_connect() as con:
            row = con.execute(
                "SELECT event_id, event_type, detail, source_event_id "
                "FROM communication_events WHERE event_type = 'legacy_event'"
            ).fetchone()
            version = con.execute("PRAGMA user_version").fetchone()[0]
        self.assertEqual(version, 1)
        self.assertEqual(row["event_id"], 1)
        self.assertEqual(row["detail"], "preserve me")
        self.assertIsNone(row["source_event_id"])

    def test_actor_cannot_be_forged_by_caller(self):
        with self.assertRaises(TypeError):
            mcp_server.record_codex_activity(**self.event_args(actor="user"))
        result = mcp_server.record_codex_activity(**self.event_args())
        self.assertIn("RECORDED:", result)
        self.assertEqual(self.read_events()[0]["sender"], "codex")

    def test_raw_commands_prompts_and_responses_are_not_accepted_or_stored(self):
        raw_values = {
            "command": "RAW_COMMAND_SENTINEL",
            "prompt": "RAW_PROMPT_SENTINEL",
            "tool_response": "RAW_RESPONSE_SENTINEL",
        }
        with self.assertRaises(TypeError):
            mcp_server.record_codex_activity(**self.event_args(**raw_values))
        mcp_server.record_codex_activity(**self.event_args())
        stored = " ".join(row["detail"] or "" for row in self.read_events())
        for sentinel in raw_values.values():
            self.assertNotIn(sentinel, stored)


if __name__ == "__main__":
    unittest.main()
