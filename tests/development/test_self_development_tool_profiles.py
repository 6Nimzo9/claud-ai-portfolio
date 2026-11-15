import ast
import json
import sys
from types import SimpleNamespace
import types
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch


ROOT = Path(__file__).resolve().parents[2]
SELF_DEVELOPMENT_DIR = ROOT / "core" / "development" / "self_development"
sys.path.insert(0, str(SELF_DEVELOPMENT_DIR))

try:
    import requests
except ModuleNotFoundError:
    # The stream is mocked below; keep the test independent of the Ollama client package.
    sys.modules["requests"] = SimpleNamespace(post=None)

ENGINE_SOURCE = SELF_DEVELOPMENT_DIR / "self_development_engine.py"
engine_tree = ast.parse(ENGINE_SOURCE.read_text(encoding="utf-8"), filename=str(ENGINE_SOURCE))


def _is_startup_side_effect(node):
    if not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Call):
        return False
    function = node.value.func
    if isinstance(function, ast.Name) and function.id == "initialize":
        return True
    if isinstance(function, ast.Attribute) and isinstance(function.value, ast.Name):
        return function.value.id == "logging" and function.attr == "basicConfig"
    return (
        isinstance(function, ast.Attribute)
        and function.attr == "mkdir"
        and isinstance(function.value, ast.Attribute)
        and function.value.attr == "parent"
        and isinstance(function.value.value, ast.Name)
        and function.value.value.id == "LOG_PATH"
    )


# Import the actual module source while omitting only its application startup
# calls, which create the production workspace/log/database on import.
engine_tree.body = [node for node in engine_tree.body if not _is_startup_side_effect(node)]
ast.fix_missing_locations(engine_tree)
engine_module = types.ModuleType("self_development_engine")
engine_module.__file__ = str(ENGINE_SOURCE)
sys.modules[engine_module.__name__] = engine_module
exec(compile(engine_tree, str(ENGINE_SOURCE), "exec"), engine_module.__dict__)


class _Cursor:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row


class _Connection:
    def __init__(self, row):
        self.row = row

    def execute(self, *_args):
        return _Cursor(self.row)


class _DispatchConnection:
    def execute(self, sql, *_args):
        if sql.startswith("SELECT * FROM dev_tasks"):
            return _Cursor({"status": "queued", "agent_messages_json": "[]"})
        if "tool_calling_supported" in sql:
            return _Cursor({"tool_calling_supported": 1})
        if "SELECT action_count" in sql:
            return _Cursor({"action_count": 0})
        return _Cursor(None)


def _fake_db_for(row):
    @contextmanager
    def fake_db():
        yield _Connection(row)

    return fake_db


def _tool_names(specs):
    return [spec["function"]["name"] for spec in specs]


def _schema_shapes(specs):
    return [
        (spec["function"]["name"], spec["function"]["parameters"])
        for spec in specs
    ]


class _FakeOllamaResponse:
    status_code = 200
    text = ""

    def iter_lines(self, decode_unicode=True):
        del decode_unicode
        yield json.dumps({
            "message": {"content": "request complete"},
            "done": True,
            "done_reason": "stop",
        })

    def close(self):
        pass


class TestSelfDevelopmentToolProfiles(unittest.TestCase):
    def select(self, objective="", acceptance="", mentor_feedback=(), title="Profile test"):
        messages = [
            {
                "role": "user",
                "content": "MENTOR FEEDBACK / NEXT INSTRUCTIONS: " + note,
            }
            for note in mentor_feedback
        ]
        row = {
            "title": title,
            "objective": objective,
            "acceptance_criteria": acceptance,
            "agent_messages_json": json.dumps(messages),
        }
        engine = object.__new__(engine_module.SelfDevelopmentEngine)
        with patch.object(engine_module, "_db", _fake_db_for(row)):
            return engine._model_tools("test-task")

    def assert_profile(self, expected, **task):
        specs, profile, exposed = self.select(**task)
        self.assertEqual(_tool_names(specs), expected)
        self.assertEqual(set(exposed), set(expected))
        return specs, profile, exposed

    def test_positive_workspace_write_exposes_writer(self):
        specs, _, _ = self.assert_profile(
            ["read_workspace_file", "write_workspace_file"],
            objective="Create the workspace file mentor_baseline/example.md.",
        )
        self.assertEqual(
            _schema_shapes(specs),
            [
                ("read_workspace_file", {
                    "type": "object",
                    "properties": {"relative_path": {"type": "string"}},
                    "required": ["relative_path"],
                }),
                ("write_workspace_file", {
                    "type": "object",
                    "properties": {
                        "relative_path": {"type": "string"},
                        "content": {"type": "string"},
                    },
                    "required": ["relative_path", "content"],
                }),
            ],
        )

    def test_positive_workspace_read_exposes_reader(self):
        self.assert_profile(
            ["read_workspace_file"],
            objective="Read mentor_baseline/example.md with the workspace reader.",
        )

    def test_positive_listing_action_exposes_listing_tool(self):
        self.assert_profile(
            ["list_workspace_files", "read_workspace_file"],
            objective="List the workspace files before continuing.",
        )

    def test_positive_source_action_exposes_source_tool(self):
        self.assert_profile(
            ["read_workspace_file", "read_project_source"],
            objective="Inspect core/main.py for the relevant source behavior.",
        )

    def test_tool_names_alone_do_not_authorize_optional_tools(self):
        self.assert_profile(
            ["read_workspace_file"],
            objective=(
                "Names only: list_workspace_files, read_project_source, "
                "write_workspace_file, run_python_compile, "
                "validate_powershell_syntax, run_safe_powershell."
            ),
        )

    def test_negative_listing_name_in_acceptance_does_not_expose_listing(self):
        self.assert_profile(
            ["read_workspace_file", "write_workspace_file"],
            objective="Create a workspace file at mentor_baseline/example.md.",
            acceptance="Must not expose list_workspace_files.",
        )

    def test_negative_source_name_in_acceptance_does_not_expose_source(self):
        self.assert_profile(
            ["read_workspace_file", "write_workspace_file"],
            objective="Create a workspace file at mentor_baseline/example.md.",
            acceptance="read_project_source must not be exposed.",
        )

    def test_core_modification_prohibition_does_not_block_source_reading(self):
        self.assert_profile(
            ["read_workspace_file", "read_project_source"],
            objective=(
                "Do not modify Core files. Use read_project_source to inspect "
                "core/main.py."
            ),
        )

    def test_explicit_source_tool_prohibition_blocks_source_reading(self):
        self.assert_profile(
            ["read_workspace_file"],
            objective=(
                "Do not use read_project_source. Inspect only workspace files."
            ),
        )

    def test_negative_tool_names_in_acceptance_do_not_authorize_tools(self):
        self.assert_profile(
            ["read_workspace_file"],
            acceptance=(
                "Must not expose list_workspace_files. "
                "read_project_source must not be exposed."
            ),
        )

    def test_explicit_positive_requests_lose_to_explicit_prohibitions(self):
        self.assert_profile(
            ["read_workspace_file", "write_workspace_file"],
            objective=(
                "List the workspace files, inspect core/main.py, and create a "
                "workspace file at mentor_baseline/example.md."
            ),
            acceptance=(
                "Must not expose list_workspace_files. "
                "read_project_source must not be exposed."
            ),
        )

    def test_negative_tool_instructions_in_task_text_take_precedence(self):
        self.assert_profile(
            ["read_workspace_file", "write_workspace_file"],
            objective=(
                "Create a workspace file at mentor_baseline/example.md. Do not use "
                "list_workspace_files or read_project_source."
            ),
        )

    def test_negative_tool_instructions_in_mentor_feedback_take_precedence(self):
        self.assert_profile(
            ["read_workspace_file", "write_workspace_file"],
            objective=(
                "List the workspace files, inspect core/main.py, and create a "
                "workspace file at mentor_baseline/example.md."
            ),
            mentor_feedback=(
                "Do not expose list_workspace_files. "
                "read_project_source must not be exposed.",
            ),
        )

    def test_ambiguous_task_does_not_add_discovery_or_source_tools(self):
        self.assert_profile(
            ["read_workspace_file"],
            objective="Handle this task carefully.",
        )

    def test_text_fallback_rejects_tool_excluded_from_profile(self):
        engine = object.__new__(engine_module.SelfDevelopmentEngine)
        specs, _, exposed = self.select(
            objective="Create a workspace file at mentor_baseline/example.md."
        )
        self.assertEqual(
            _tool_names(specs),
            ["read_workspace_file", "write_workspace_file"],
        )
        for name, parameters in (
            ("list_workspace_files", {}),
            ("read_project_source", {"relative_path": "core/main.py"}),
        ):
            with self.subTest(name=name):
                request = json.dumps({"name": name, "parameters": parameters})
                with self.assertRaisesRegex(ValueError, "not exposed"):
                    engine._parse_text_tool_request(
                        request,
                        exposed_tool_names=exposed,
                    )

    def test_native_dispatch_rejects_tool_excluded_from_profile(self):
        @contextmanager
        def fake_dispatch_db():
            yield _DispatchConnection()

        engine = object.__new__(engine_module.SelfDevelopmentEngine)
        engine._active_task_id = ""
        specs, profile, exposed = self.select(
            objective="Create a workspace file at mentor_baseline/example.md."
        )
        self.assertEqual(
            _tool_names(specs),
            ["read_workspace_file", "write_workspace_file"],
        )
        handler = MagicMock(return_value="unexpected invocation")
        engine._tool_handlers = {"list_workspace_files": handler}
        engine._request_ollama_stream = MagicMock(return_value=(
            {
                "content": "",
                "tool_calls": [{
                    "function": {"name": "list_workspace_files", "arguments": {}},
                }],
            },
            {
                "exposed_tool_names": sorted(exposed),
                "tool_profile": profile,
            },
        ))
        with (
            patch.object(engine_module, "_db", fake_dispatch_db),
            patch.object(engine_module, "_event") as event,
            patch.object(engine_module, "_require_enabled"),
            patch.object(engine_module, "_get_control", return_value={"state": "enabled"}),
            patch.object(engine, "_save_agent_messages"),
            patch.object(engine, "_record_task_action"),
            patch.object(engine, "_finish_task_turn", return_value="needs mentor review"),
        ):
            engine._run_task_locked("test-task", max_model_turns=1)

        handler.assert_not_called()
        self.assertIn(
            "tool_dispatch_unexposed_rejected",
            [call.args[1] for call in event.call_args_list],
        )

    def test_repeated_ollama_turns_rebuild_same_restricted_schema(self):
        row = {
            "title": "Exact-target workspace write",
            "objective": (
                "Create the workspace file at "
                "mentor_baseline/profile_test.md."
            ),
            "acceptance_criteria": (
                "Must not expose list_workspace_files. "
                "read_project_source must not be exposed."
            ),
            "agent_messages_json": "[]",
        }
        engine = object.__new__(engine_module.SelfDevelopmentEngine)
        engine.ollama_url = "http://127.0.0.1:11434"
        engine.model = "llama3.2"
        messages = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "task"},
        ]
        captured_payloads = []

        def capture_post(_url, *, json, **_kwargs):
            captured_payloads.append(json)
            return _FakeOllamaResponse()

        with (
            patch.object(engine_module, "_db", _fake_db_for(row)),
            patch.object(engine_module, "_event"),
            patch.object(engine_module.requests, "post", side_effect=capture_post),
            patch.object(engine, "_model_tools", wraps=engine._model_tools) as selector,
        ):
            for turn in (1, 2):
                engine._request_ollama_stream("test-task", messages, turn)

        self.assertEqual(selector.call_count, 2)
        schemas = [payload["tools"] for payload in captured_payloads]
        self.assertEqual(len(schemas), 2)
        self.assertEqual(
            [_tool_names(schema) for schema in schemas],
            [
                ["read_workspace_file", "write_workspace_file"],
                ["read_workspace_file", "write_workspace_file"],
            ],
        )
        self.assertEqual(schemas[0], schemas[1])


if __name__ == "__main__":
    unittest.main()
