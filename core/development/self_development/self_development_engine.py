from __future__ import annotations

import difflib
import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import requests

ROOT = Path(__file__).resolve().parents[3]
DEV_DIR = ROOT / "core" / "development" / "self_development"
DB_PATH = ROOT / "database" / "self_development.db"
CORE_DB = ROOT / "database" / "claud_memory.db"
COMM_DB = ROOT / "database" / "communication.db"
CONFIG_PATH = ROOT / "config" / "settings.json"
WORKSPACE = ROOT / "workspace" / "self_development"
BACKUP_ROOT = ROOT / "backups" / "self_development_promotions"
LOG_PATH = ROOT / "logs" / "self-development.log"
MAX_FILE_BYTES = 120_000
MAX_SOURCE_CHARS = 45_000
MAX_TASK_ACTIONS = 30
MAX_MODEL_TURNS = 3
OLLAMA_TIMEOUT = 180  # Retained for one-shot health/probe calls.
OLLAMA_CONNECT_TIMEOUT = 10
OLLAMA_STREAM_IDLE_TIMEOUT = 60
AGENT_NUM_PREDICT = 1024
AGENT_CONTEXT_CHAR_BUDGET = 7600
# Text-only tool envelopes are accepted only for this non-executing / workspace-safe subset.
# PowerShell execution and Core promotion always require native tool calls and their existing approval gates.
AGENT_TEXT_TOOL_FALLBACK_ALLOWLIST = frozenset({
    "list_workspace_files",
    "read_workspace_file",
    "read_project_source",
    "write_workspace_file",
    "run_python_compile",
    "validate_powershell_syntax",
})
AGENT_CONTEXT_MESSAGE_BUDGET = 16
AGENT_TOOL_RESULT_CHAR_LIMIT = 4500

ALLOWED_SOURCE_ROOTS = (
    "core/main.py",
    "core/reasoning/",
    "core/orchestrator/",
    "core/context/",
    "core/knowledge/",
    "core/development/",
    "tests/development/",
)
ALLOWED_SOURCE_SUFFIXES = {".py", ".ps1", ".md", ".toml", ".txt"}
ALLOWED_WORKSPACE_SUFFIXES = {
    ".py", ".ps1", ".md", ".json", ".toml", ".txt", ".yml", ".yaml"
}
ALLOWED_PROMOTION_PREFIXES = (
    "core/main.py",
    "core/reasoning/",
    "core/orchestrator/",
    "core/context/",
    "core/knowledge/",
    "core/development/",
    "tests/",
)
FORBIDDEN_PS_PATTERNS = [
    r"(?i)\b(?:Start-Process|Invoke-Expression|iex|Invoke-Command|Enter-PSSession|New-PSSession)\b",
    r"(?i)\b(?:Invoke-WebRequest|Invoke-RestMethod|Start-BitsTransfer|curl|wget)\b",
    r"(?i)\b(?:Set-ExecutionPolicy|Add-Type|New-Object|Register-ScheduledTask|Unregister-ScheduledTask)\b",
    r"(?i)\b(?:Set-MpPreference|Disable-ComputerRestore|Disable-WindowsOptionalFeature|netsh|sc\.exe|reg\.exe|schtasks\.exe)\b",
    r"(?i)\b(?:Get-Credential|ConvertTo-SecureString|Export-Clixml|cmdkey|vaultcmd)\b",
    r"(?i)(?:-Verb\s+RunAs|EncodedCommand|ExecutionPolicy\s+Bypass|HKLM:|HKCU:|Registry::|\\\\[^\\]+\\)",
    r"(?i)\[[A-Za-z_][A-Za-z0-9_.+`]*\]\s*::",
]

LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    filename=str(LOG_PATH),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    encoding="utf-8",
)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _canonical_relative(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("A relative path is required.")
    raw = value.strip().replace("\\", "/")
    if raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
        raise ValueError("Absolute paths are not allowed.")
    parts = [p for p in raw.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        raise ValueError("Path traversal is not allowed.")
    for part in parts:
        if any(ord(ch) < 32 or ch in '<>:"|?*' for ch in part) or part.endswith((".", " ")):
            raise ValueError("Invalid Windows path component or alternate-stream path.")
        device = part.split(".", 1)[0].rstrip(" ").upper()
        if device in {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$", "CLOCK$"} or re.fullmatch(r"(?:COM|LPT)(?:[1-9]|[Â¹Â²Â³])", device):
            raise ValueError("Reserved Windows device names are not permitted in workspace paths.")
    return "/".join(parts)


def _within(path: Path, base: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(base.resolve(strict=False))
        return True
    except (ValueError, OSError):
        return False


def _has_reparse_component(path: Path, stop_at: Path) -> bool:
    current = path
    stop = stop_at.resolve(strict=False)
    while True:
        try:
            if current.exists() and (current.is_symlink() or getattr(current, "is_junction", lambda: False)()):
                return True
            st = current.stat() if current.exists() else None
            attrs = getattr(st, "st_file_attributes", 0) if st else 0
            if attrs & getattr(__import__("stat"), "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
                return True
        except OSError:
            return True
        if current.resolve(strict=False) == stop or current.parent == current:
            break
        current = current.parent
    return False


def _workspace_path(relative: str, must_exist: bool = False) -> Path:
    rel = _canonical_relative(relative)
    path = WORKSPACE.joinpath(*rel.split("/"))
    if not _within(path, WORKSPACE):
        raise ValueError("Path escapes the self-development workspace.")
    if _has_reparse_component(path, WORKSPACE):
        raise ValueError("Symlinks/junctions are not allowed in workspace paths.")
    if path.suffix.lower() not in ALLOWED_WORKSPACE_SUFFIXES:
        raise ValueError(f"File extension is not permitted: {path.suffix}")
    # Windows hard links are not reparse points. Block existing multi-link files
    # so a workspace write cannot mutate another path through a hard-link alias.
    if path.exists() and path.is_file():
        try:
            if int(path.stat().st_nlink) > 1:
                raise ValueError("Hard-linked files are not permitted in the self-development workspace.")
        except OSError as exc:
            raise ValueError("Could not safely inspect workspace file metadata.") from exc
    if must_exist and not path.is_file():
        raise FileNotFoundError(f"Workspace file not found: {rel}")
    return path


def _source_path(relative: str) -> Path:
    rel = _canonical_relative(relative)
    key = rel.lower()
    if not any(key == root or key.startswith(root) for root in ALLOWED_SOURCE_ROOTS):
        raise ValueError("Source reads are limited to allowlisted Core and test source folders.")
    path = ROOT.joinpath(*rel.split("/"))
    if path.suffix.lower() not in ALLOWED_SOURCE_SUFFIXES:
        raise ValueError("Only source/documentation files can be read.")
    forbidden_parts = {".venv", ".venv_mcp", "__pycache__", "node_modules", "backups", "database", "logs", "models"}
    if any(part.lower() in forbidden_parts for part in path.parts):
        raise ValueError("This project path is excluded from source reading.")
    if not _within(path, ROOT) or _has_reparse_component(path, ROOT):
        raise ValueError("Source path is outside the permitted project tree or uses a reparse point.")
    if not path.is_file():
        raise FileNotFoundError(f"Source file not found: {rel}")
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError("Source file exceeds the 120 KB read limit.")
    return path


def _promotion_path(relative: str) -> Path:
    rel = _canonical_relative(relative)
    key = rel.lower()
    if not any(key == p or key.startswith(p) for p in ALLOWED_PROMOTION_PREFIXES):
        raise ValueError("Promotion destination must be inside approved Core source folders or tests/development.")
    if key.startswith("core/development/self_development/"):
        raise ValueError("Self-development controller and policy files cannot be promoted by the autonomous agent.")
    if key.startswith("core/communication/"):
        raise ValueError("Promoting MCP communication server files is blocked; use a separately reviewed installer.")
    path = ROOT.joinpath(*rel.split("/"))
    if path.suffix.lower() not in ALLOWED_WORKSPACE_SUFFIXES:
        raise ValueError("Promotion file type is not permitted.")
    if not _within(path, ROOT) or _has_reparse_component(path, ROOT):
        raise ValueError("Promotion path is outside the project or uses a reparse point.")
    return path


def _db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if _has_reparse_component(DB_PATH.parent, ROOT) or (DB_PATH.exists() and _has_reparse_component(DB_PATH, ROOT)):
        raise RuntimeError("Self-development database path contains a symlink/junction; database access refused.")
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    return con


def initialize() -> None:
    workspace_parent = ROOT / "workspace"
    if workspace_parent.exists() and (workspace_parent.is_symlink() or getattr(workspace_parent, "is_junction", lambda: False)()):
        raise RuntimeError("workspace directory must not be a symlink or junction.")
    if WORKSPACE.exists() and (WORKSPACE.is_symlink() or getattr(WORKSPACE, "is_junction", lambda: False)()):
        raise RuntimeError("The self-development workspace must not be a symlink or junction.")
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    (WORKSPACE / "README.txt").write_text(
        "Claud AI self-development workspace.\n"
        "Generated code and PowerShell scripts belong here first.\n"
        "Core promotion and custom PowerShell execution require user review.\n",
        encoding="utf-8",
    ) if not (WORKSPACE / "README.txt").exists() else None
    with _db() as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS dev_tasks (
                task_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                objective TEXT NOT NULL,
                acceptance_criteria TEXT NOT NULL,
                mentor_guidance TEXT NOT NULL DEFAULT '',
                communication_session_id TEXT,
                status TEXT NOT NULL,
                agent_messages_json TEXT NOT NULL DEFAULT '[]',
                action_count INTEGER NOT NULL DEFAULT 0,
                last_result TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS dev_actions (
                action_id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                action_type TEXT NOT NULL,
                detail TEXT NOT NULL,
                result TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(task_id) REFERENCES dev_tasks(task_id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS dev_approvals (
                approval_id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                approval_type TEXT NOT NULL,
                source_rel TEXT NOT NULL,
                destination_rel TEXT NOT NULL DEFAULT '',
                file_sha256 TEXT NOT NULL,
                destination_sha256 TEXT NOT NULL DEFAULT '',
                purpose TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                reviewed_at TEXT NOT NULL DEFAULT '',
                result TEXT NOT NULL DEFAULT '',
                FOREIGN KEY(task_id) REFERENCES dev_tasks(task_id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS dev_control (
                singleton_id INTEGER PRIMARY KEY CHECK(singleton_id=1),
                state TEXT NOT NULL,
                reason TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS dev_runtime (
                singleton_id INTEGER PRIMARY KEY CHECK(singleton_id=1),
                tool_calling_supported INTEGER NOT NULL DEFAULT 0,
                tool_calling_tested_at TEXT NOT NULL DEFAULT '',
                active_powershell_pid INTEGER NOT NULL DEFAULT 0,
                active_task_id TEXT NOT NULL DEFAULT ''
            );
            """
        )
        con.execute(
            "INSERT OR IGNORE INTO dev_control(singleton_id,state,reason,updated_at) VALUES(1,'enabled','Initial state; no task runs without an explicit start call.',?)",
            (now(),),
        )
        con.execute(
            "INSERT OR IGNORE INTO dev_runtime(singleton_id) VALUES(1)"
        )


def _event(task_id: str, event_type: str, detail: str, result: str = "") -> None:
    with _db() as con:
        con.execute(
            "INSERT INTO dev_actions(task_id,action_type,detail,result,created_at) VALUES(?,?,?,?,?)",
            (task_id, event_type, detail[:4000], result[:8000], now()),
        )
        row = con.execute(
            "SELECT communication_session_id FROM dev_tasks WHERE task_id=?", (task_id,)
        ).fetchone()
    if row and row["communication_session_id"]:
        _write_communication_event(row["communication_session_id"], event_type, detail, result)


def _write_communication_event(session_id: str, event_type: str, detail: str, result: str = "") -> None:
    if not COMM_DB.is_file():
        return
    try:
        with sqlite3.connect(COMM_DB, timeout=5) as con:
            exists = con.execute(
                "SELECT 1 FROM communication_sessions WHERE session_id=?", (session_id,)
            ).fetchone()
            if not exists:
                return
            ts = now()
            if "communication_events" in {
                row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }:
                con.execute(
                    "INSERT INTO communication_events(session_id,event_type,sender,detail,created_at) VALUES(?,?,?,?,?)",
                    (session_id, "dev_" + event_type[:60], "system", detail[:1000], ts),
                )
            if event_type in {
                "task_started", "agent_thinking", "list_workspace_files", "read_workspace_file",
                "read_project_source", "write_workspace_file", "run_python_compile",
                "validate_powershell_syntax", "run_safe_powershell", "powershell_safe_execute",
                "propose_powershell_execution", "propose_core_promotion", "mentor_feedback",
                "task_review_required", "task_stopped", "task_paused", "task_completed",
                "task_linked_to_session", "agent_stream_metrics", "agent_text_tool_request_normalized",
                "agent_text_tool_request_rejected", "tool_profile_selected", "agent_tool_call_received",
                "tool_dispatch_unexposed_rejected", "tool_dispatch_validated", "tool_dispatch_completed",
                "tool_dispatch_failed", "agent_stream_started", "agent_stream_progress", "agent_stream_complete",
                "agent_turn_no_action", "agent_turn_text_response",
            }:
                msg = f"[CLAUD SELF-DEVELOPMENT] {detail}"
                if result:
                    msg += f"\nResult: {result[:1500]}"
                con.execute(
                    "INSERT INTO communication_messages(session_id,sender,content,created_at) VALUES(?,?,?,?)",
                    (session_id, "claud", msg[:5000], ts),
                )
            con.execute(
                "UPDATE communication_sessions SET updated_at=? WHERE session_id=?",
                (ts, session_id),
            )
    except Exception:
        logging.exception("Could not mirror development event to Live Room")


def _get_control() -> dict[str, str]:
    with _db() as con:
        row = con.execute("SELECT state,reason,updated_at FROM dev_control WHERE singleton_id=1").fetchone()
    return dict(row) if row else {"state": "stopped", "reason": "Control record missing", "updated_at": ""}


def _require_enabled() -> None:
    control = _get_control()
    if control["state"] != "enabled":
        raise RuntimeError(f"Self-development is {control['state']}: {control['reason']}")


def set_control(state: str, reason: str = "") -> str:
    if state not in {"enabled", "paused", "stopped"}:
        raise ValueError("state must be enabled, paused, or stopped")
    with _db() as con:
        con.execute(
            "UPDATE dev_control SET state=?, reason=?, updated_at=? WHERE singleton_id=1",
            (state, reason[:1000], now()),
        )
        if state in {"paused", "stopped"}:
            row = con.execute("SELECT active_powershell_pid FROM dev_runtime WHERE singleton_id=1").fetchone()
            pid = int(row["active_powershell_pid"] or 0) if row else 0
        else:
            pid = 0
    if pid:
        _terminate_child_process(pid)
    return f"Self-development state set to {state}. {reason}".strip()


def _terminate_child_process(pid: int) -> None:
    if os.name != "nt" or pid <= 0:
        return
    try:
        # Only terminate a PowerShell child whose command line points into our workspace.
        query = (
            "$p=Get-CimInstance Win32_Process -Filter 'ProcessId=%d' -ErrorAction SilentlyContinue; "
            "if ($p -and $p.Name -match '^(powershell|pwsh)\\.exe$' -and "
            "$p.CommandLine -like '*self_development*') { Stop-Process -Id %d -Force -ErrorAction SilentlyContinue; 'STOPPED' } "
            "else { 'NOT_STOPPED_OR_NOT_OWNED' }" % (pid, pid)
        )
        subprocess.run(
            ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", query],
            capture_output=True, text=True, timeout=8, check=False,
        )
    except Exception:
        logging.exception("Could not terminate tracked PowerShell child %s", pid)


def _load_config() -> tuple[str, str]:
    if not CONFIG_PATH.is_file():
        raise FileNotFoundError(f"Core settings not found: {CONFIG_PATH}")
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
    raw_url = str(config.get("ollama_url", "http://127.0.0.1:11434")).rstrip("/")
    model = str(config.get("local_model", config.get("model", "llama3.2:latest"))).strip()
    try:
        parsed = urlsplit(raw_url)
        port = parsed.port
    except ValueError as exc:
        raise RuntimeError("Invalid local Ollama URL configuration.") from exc
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost"}
        or parsed.username is not None or parsed.password is not None
        or parsed.query or parsed.fragment
        or parsed.path not in {"", "/"}
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise RuntimeError("Self-development agent only permits a plain HTTP Ollama endpoint bound to 127.0.0.1 or localhost, without credentials, paths, redirects, or other hosts.")
    if not model or len(model) > 120 or any(ord(ch) < 32 for ch in model):
        raise RuntimeError("No valid local model is configured.")
    normalized_host = "127.0.0.1" if parsed.hostname == "127.0.0.1" else "localhost"
    ollama_url = f"http://{normalized_host}:{port or 80}"
    return ollama_url, model


def _is_admin() -> bool:
    if os.name != "nt":
        return False
    # The agent refuses PowerShell execution if its inherited Windows token is elevated.
    script = (
        "$p=New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent()); "
        "if ($p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { exit 10 } else { exit 0 }"
    )
    proc = subprocess.run(
        ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, timeout=8, check=False,
    )
    return proc.returncode == 10 or proc.returncode not in (0, 10)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _write_exclusive(path: Path, content: str) -> None:
    """Create a new staging file without following a pre-existing symlink."""
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    fd = os.open(str(path), flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def _copy_exclusive(source: Path, destination: Path) -> None:
    """Copy into a fresh file created with O_EXCL, never following an existing link."""
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    fd = os.open(str(destination), flags, 0o600)
    try:
        with source.open("rb") as src, os.fdopen(fd, "wb") as dst:
            shutil.copyfileobj(src, dst, length=1024 * 1024)
            dst.flush()
            os.fsync(dst.fileno())
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            destination.unlink(missing_ok=True)
        except OSError:
            pass
        raise


class SelfDevelopmentEngine:
    def __init__(self) -> None:
        initialize()
        self.ollama_url, self.model = _load_config()
        self._active_task_id = ""
        # The local agent uses one active task context for its tool handlers.
        # Serialize runs so two simultaneous MCP calls cannot cross-wire task IDs.
        self._run_lock = threading.Lock()
        self._tool_handlers = {
            "list_workspace_files": self.list_workspace_files,
            "read_workspace_file": self.read_workspace_file,
            "read_project_source": self.read_project_source,
            "write_workspace_file": self.write_workspace_file,
            "run_python_compile": self.run_python_compile,
            "validate_powershell_syntax": self.validate_powershell_syntax,
            "run_safe_powershell": self._agent_run_safe_powershell,
            "propose_powershell_execution": self._agent_propose_powershell_execution,
            "propose_core_promotion": self._agent_propose_core_promotion,
        }

    def status(self) -> dict[str, Any]:
        control = _get_control()
        with _db() as con:
            rt = con.execute("SELECT * FROM dev_runtime WHERE singleton_id=1").fetchone()
            counts = {
                row["status"]: row["n"]
                for row in con.execute("SELECT status,COUNT(*) n FROM dev_tasks GROUP BY status").fetchall()
            }
            pending = con.execute("SELECT COUNT(*) FROM dev_approvals WHERE status='pending' ").fetchone()[0]
            latest = con.execute("SELECT task_id,title,status,updated_at FROM dev_tasks ORDER BY created_at DESC LIMIT 1").fetchone()
        return {
            "state": control["state"], "state_reason": control["reason"],
            "active_task_id": rt["active_task_id"],
            "active_powershell_pid": int(rt["active_powershell_pid"] or 0),
            "model": self.model, "ollama_url": self.ollama_url,
            "tool_calling_supported": bool(rt["tool_calling_supported"]),
            "tool_calling_tested_at": rt["tool_calling_tested_at"],
            "workspace": str(WORKSPACE), "database": str(DB_PATH),
            "task_counts": counts, "pending_approvals": pending,
            "latest_task": dict(latest) if latest else None,
            "execution_policy": "workspace-only writes; Python compile and PowerShell syntax checks; strictly allowlisted non-destructive PowerShell may run non-admin inside workspace; custom/destructive PowerShell and Core promotion require local interactive user approval; no elevation",
        }

    def probe_tool_calling(self) -> str:
        _require_enabled()
        tool_schema = [{
            "type": "function",
            "function": {
                "name": "report_tool_capability",
                "description": "Report that this model can call a local function. This tool performs no action.",
                "parameters": {
                    "type": "object", "properties": {"ready": {"type": "boolean"}, "note": {"type": "string"}},
                    "required": ["ready", "note"],
                },
            },
        }]
        response = requests.post(
            f"{self.ollama_url}/api/chat",
            json={
                "model": self.model,
                "messages": [{"role": "user", "content": "Call report_tool_capability exactly once with ready=true and note='tool calling probe'. Do not answer in plain text."}],
                "tools": tool_schema, "stream": False,
                "options": {"temperature": 0, "num_ctx": 2048},
            }, timeout=60, allow_redirects=False,
        )
        if not 200 <= response.status_code < 300:
            raise RuntimeError(f"Local Ollama returned HTTP {response.status_code}; redirects are disabled.")
        message = response.json().get("message", {})
        calls = message.get("tool_calls") or []
        supported = bool(calls and calls[0].get("function", {}).get("name") == "report_tool_capability")
        with _db() as con:
            con.execute(
                "UPDATE dev_runtime SET tool_calling_supported=?, tool_calling_tested_at=? WHERE singleton_id=1",
                (1 if supported else 0, now()),
            )
        if supported:
            return f"TOOL CALLING: SUPPORTED\nModel: {self.model}\nOllama: {self.ollama_url}\nProbe tool call received successfully. No filesystem action was performed."
        return (
            f"TOOL CALLING: NOT CONFIRMED\nModel: {self.model}\n"
            "The model returned no matching tool call. No development task was run. "
            "Check the Ollama/model version or use ChatGPT mentor tools to stage files directly."
        )

    def start_task(
        self, title: str, objective: str, acceptance_criteria: str,
        mentor_guidance: str = "", communication_session_id: str = "",
    ) -> str:
        _require_enabled()
        fields = [("title", title, 160), ("objective", objective, 4000),
                  ("acceptance_criteria", acceptance_criteria, 4000), ("mentor_guidance", mentor_guidance, 4000)]
        for name, value, limit in fields:
            if not isinstance(value, str) or (name != "mentor_guidance" and not value.strip()):
                raise ValueError(f"{name} is required.")
            if len(value) > limit:
                raise ValueError(f"{name} exceeds {limit} characters.")
        if not isinstance(communication_session_id, str) or len(communication_session_id) > 80:
            raise ValueError("communication_session_id must be text up to 80 characters.")
        task_id = str(uuid.uuid4())
        ts = now()
        initial_messages = [
            {"role": "system", "content": self._agent_system_prompt()},
            {"role": "user", "content": self._task_prompt(title, objective, acceptance_criteria, mentor_guidance)},
        ]
        with _db() as con:
            con.execute(
                "INSERT INTO dev_tasks(task_id,title,objective,acceptance_criteria,mentor_guidance,communication_session_id,status,agent_messages_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (task_id, title.strip(), objective.strip(), acceptance_criteria.strip(), mentor_guidance.strip(), communication_session_id.strip(), "queued", json.dumps(initial_messages, ensure_ascii=False), ts, ts),
            )
        _event(task_id, "task_started", f"Mentored development task queued: {title.strip()}")
        return (
            f"DEVELOPMENT TASK CREATED\nTask ID: {task_id}\nTitle: {title.strip()}\n"
            "Status: queued\nWrites: self-development workspace only\n"
            "No Core file has been changed. Next: run claud_dev_run_task."
        )

    def _agent_system_prompt(self) -> str:
        return (
            "You are Claud AI's local Self-Development Agent, running under mentor guidance from ChatGPT. "
            "Your job is to learn by inspecting permitted source, designing a small improvement, writing code or PowerShell "
            "inside the self-development workspace, and validating it. You are not allowed to modify active Core files. "
            "You may only use the tools provided in this request. Never ask for administrator privileges, change Windows "
            "security settings, access credentials, use the network, alter startup tasks, or write outside the workspace. "
            "Use only exact tool names and JSON argument types from the supplied tool schemas; never invent tool names. "
            "Tool boundary: read_workspace_file is ONLY for D:\\ClaudAI\\workspace\\self_development files; "
            "read_project_source is ONLY for allowlisted Core/test source files and must NEVER be used for workspace paths. "
            "write_workspace_file is ONLY for workspace artifacts. A tool absent from the current task schema is unavailable. "
            "When an action is required, emit a native Ollama tool call in message.tool_calls. Never write a JSON-looking "
            "tool request in ordinary assistant content. Plain-text JSON is not a tool call and is not executed unless it "
            "passes the engine's strict compatibility parser for the explicitly safe workspace-only subset. "
            "Use one tool call per assistant turn. "
            "Use read_workspace_file only for files inside the self-development workspace, including mentor_baseline/*. "
            "Use read_project_source only for allowlisted Core/test source files, only when that tool is present in the current schema; "
            "NEVER send a workspace path such as mentor_baseline/* to read_project_source. "
            "Use write_workspace_file only for workspace artifacts. "
            "For run_python_compile, relative_paths MUST be a JSON array of strings, never a string containing a list. "
            "When the task names an exact workspace target path, read that exact path with read_workspace_file; if it does not exist, use write_workspace_file to create it. "
            "Do not list the workspace directory when the target path is already known. "
            "All generated files must be written under the workspace. Use Python compilation or PowerShell syntax validation "
            "only when the corresponding tool is present in the current schema. Use run_safe_powershell only when explicitly exposed. "
            "The local agent is never given approval, custom PowerShell execution, or Core-promotion tools. If an operation needs "
            "those permissions, stop and report that ChatGPT mentor and local user review are required; never simulate or invent such a tool call. "
            "Scripts that invoke external programs, dynamic evaluation, network, registry, credentials, startup changes, "
            "security-policy changes, or administrator operations are forbidden even with approval. "
            "Work incrementally. Keep actions small. Before retrying after a blocked/timeout result, inspect the existing workspace and checkpoint; never blindly repeat a write. Stop if the control state is paused or stopped. "
            "Do not claim a test passed unless the tool result confirms it. Do not claim model weights changed. "
            "Return a concise report of files created, checks performed, outstanding risks, and next steps."
        )

    @staticmethod
    def _task_prompt(title: str, objective: str, acceptance: str, guidance: str) -> str:
        return (
            f"TASK: {title}\nOBJECTIVE:\n{objective}\n\nACCEPTANCE CRITERIA:\n{acceptance}\n\n"
            f"MENTOR GUIDANCE:\n{guidance or '(none supplied)'}\n\n"
            "Follow the task-specific tool schema supplied for this task. Do not call tools that are absent from that schema. "
            "If an exact workspace target path is named, use the workspace reader for that path; if it does not exist, use "
            "the workspace writer. Do not list the workspace unless discovery is explicitly needed. Implement only the "
            "smallest requested step. Do not modify active Core files."
        )

    @staticmethod
    def _has_negative_tool_intent(text: str, target_pattern: str) -> bool:
        """Recognize explicit prohibitions around a tool or its action."""
        negative_prefix = r"\b(?:do not|don't|never|must not|mustn't|should not|shouldn't|avoid|exclude|without|no)\b"
        negative_suffix = r"\b(?:must not|mustn't|should not|shouldn't|not allowed|not exposed|not permitted|forbidden|prohibited|cannot be used|may not be used)\b"
        target = rf"(?:{target_pattern})"
        # Keep negation within its clause, while allowing exact tool bans across fields.
        between = r"[^.!?;\n]"
        return bool(
            re.search(rf"{negative_prefix}{between}{{0,120}}{target}", text)
            or re.search(rf"{target}{between}{{0,100}}{negative_suffix}", text)
        )

    def _model_tools(self, task_id: str) -> tuple[list[dict[str, Any]], str, frozenset[str]]:
        """Build the task-specific tool schema from affirmative intent."""
        with _db() as con:
            task = con.execute(
                "SELECT title,objective,acceptance_criteria,agent_messages_json FROM dev_tasks WHERE task_id=?",
                (task_id.strip(),),
            ).fetchone()
        if task is None:
            raise ValueError("Development task not found while selecting task-specific tools.")

        base_text = "\n".join(str(task[key] or "") for key in ("title", "objective", "acceptance_criteria"))
        try:
            history = json.loads(task["agent_messages_json"] or "[]")
        except (TypeError, ValueError):
            history = []
        # Include only explicit mentor feedback messages, not tool output or model-generated history.
        mentor_notes: list[str] = []
        if isinstance(history, list):
            mentor_notes = [
                str(item.get("content", ""))
                for item in history[-8:]
                if isinstance(item, dict)
                and item.get("role") == "user"
                and str(item.get("content", "")).startswith("MENTOR FEEDBACK / NEXT INSTRUCTIONS:")
            ][-3:]
        mentor_text = "\n".join(mentor_notes)
        task_text = base_text + "\n" + mentor_text
        normalized = task_text.lower().replace("\\", "/")

        source_path_pattern = (
            r"(?<![a-z0-9_])(?:core|tests)/[a-z0-9_./-]+\.(?:py|ps1|md|txt|toml)(?![a-z0-9_])"
        )
        source_target_pattern = (
            r"(?:read_project_source|"
            r"(?:read|inspect|review|audit)\s+(?:the\s+)?(?:(?:core|project)\s+)?"
            r"source(?:\s+(?:code|files?))?|source\s+(?:reads?|reading|read access)|"
            r"use\s+(?:the\s+)?(?:previously\s+)?recorded\s+source\s+evidence|"
            + source_path_pattern
            + r")"
        )
        source_path_requested = bool(re.search(
            r"\b(?:read|inspect|review|audit)\b[\s\S]{0,120}" + source_path_pattern,
            normalized,
        ))
        source_phrase_requested = bool(re.search(
            r"\b(?:read|inspect|review|audit)\b[\s\S]{0,80}\b(?:core|project )?source(?:\s+(?:code|files?))?\b|"
            r"\b(?:use|call|allow|enable|expose|grant)\b[\s\S]{0,40}\bread_project_source\b",
            normalized,
        ))
        source_read_prohibited = self._has_negative_tool_intent(
            normalized, source_target_pattern,
        )
        source_requested = (
            source_path_requested or source_phrase_requested
        ) and not source_read_prohibited

        write_target_pattern = (
            r"(?:write_workspace_file|"
            r"(?:create|write|edit|update|replace|author|implement|generate|revise|produce)\b"
            r"[\s\S]{0,100}\b(?:file|files|artifact|artifacts|document|documents|script|module|code|experiment|assessment|roadmap)\b)"
        )
        write_intent = bool(re.search(
            r"\b(?:create|write|edit|update|replace|author|implement|generate|revise|produce)\b"
            r"[\s\S]{0,120}\b(?:file|files|artifact|artifacts|document|documents|script|module|code|experiment|assessment|roadmap)\b",
            normalized,
        )) and not self._has_negative_tool_intent(normalized, write_target_pattern)

        python_compile_target_pattern = (
            r"(?:run_python_compile|py_compile|python compile|python syntax check|syntax compilation)"
        )
        python_compile_requested = bool(re.search(
            r"\b(?:run|execute|perform|use|call|allow|enable|expose|grant)\b"
            r"[\s\S]{0,60}\b(?:run_python_compile|py_compile|python compile|python syntax check|syntax compilation)\b|"
            r"\bcompile\b[\s\S]{0,60}\b(?:python|code|script)\b",
            normalized,
        )) and not self._has_negative_tool_intent(
            normalized, python_compile_target_pattern,
        )
        powershell_syntax_target_pattern = (
            r"(?:validate_powershell_syntax|powershell syntax|parse powershell|\.ps1)"
        )
        powershell_syntax_requested = bool(re.search(
            r"\b(?:parse|check|validate|run|perform)\b[\s\S]{0,80}"
            r"(?:\b(?:powershell syntax|powershell script)\b|\.ps1\b)|"
            r"\b(?:use|call|allow|enable|expose|grant)\b[\s\S]{0,40}\bvalidate_powershell_syntax\b",
            normalized,
        )) and not self._has_negative_tool_intent(
            normalized, powershell_syntax_target_pattern,
        )
        safe_powershell_target_pattern = (
            r"(?:run_safe_powershell|allowlisted non-destructive powershell|allowlisted powershell)"
        )
        safe_powershell_requested = bool(re.search(
            r"\b(?:run|execute|use|call|allow|enable|expose|grant)\b[\s\S]{0,60}"
            r"(?:\brun_safe_powershell\b|\ballowlisted (?:non-destructive )?powershell\b)",
            normalized,
        )) and not self._has_negative_tool_intent(
            normalized, safe_powershell_target_pattern,
        )
        listing_target_pattern = (
            r"(?:list_workspace_files|list\s+(?:the\s+)?(?:current\s+)?workspace\s+(?:files|artifacts|contents)|"
            r"workspace\s+listing|listing\s+workspace\s+files)"
        )
        explicit_listing_requested = bool(re.search(
            r"\b(?:list|inventory|discover)\b[\s\S]{0,80}\b(?:the\s+)?(?:current\s+)?workspace\s+(?:files|artifacts|contents)\b|"
            r"\b(?:use|call|allow|enable|expose|grant)\b[\s\S]{0,40}\blist_workspace_files\b",
            normalized,
        ))
        listing_prohibited = self._has_negative_tool_intent(
            normalized, listing_target_pattern,
        )
        workspace_listing_requested = explicit_listing_requested and not listing_prohibited

        allowed: list[str] = []
        if workspace_listing_requested:
            allowed.append("list_workspace_files")
        # Keep the workspace reader as the existing baseline capability.
        allowed.append("read_workspace_file")
        if write_intent:
            allowed.append("write_workspace_file")
        if source_requested:
            allowed.append("read_project_source")
        if python_compile_requested:
            allowed.append("run_python_compile")
        if powershell_syntax_requested:
            allowed.append("validate_powershell_syntax")
        if safe_powershell_requested:
            allowed.append("run_safe_powershell")

        descriptions: dict[str, dict[str, Any]] = {
            "list_workspace_files": {
                "name": "list_workspace_files",
                "description": "List files only inside D:\\ClaudAI\\workspace\\self_development. Use this to discover workspace artifacts; it does not read file contents.",
                "parameters": {"type": "object", "properties": {"relative_dir": {"type": "string"}}, "required": []},
            },
            "read_workspace_file": {
                "name": "read_workspace_file",
                "description": "Read one existing text file only from the self-development workspace. Use this for mentor_baseline/* and all other workspace paths. NEVER use read_project_source for a workspace file.",
                "parameters": {"type": "object", "properties": {"relative_path": {"type": "string"}}, "required": ["relative_path"]},
            },
            "write_workspace_file": {
                "name": "write_workspace_file",
                "description": "Create or update one text/code artifact only inside the self-development workspace. Arguments are exactly relative_path (workspace-relative string) and content (complete file text). Never use this to modify active Core files.",
                "parameters": {"type": "object", "properties": {"relative_path": {"type": "string"}, "content": {"type": "string"}}, "required": ["relative_path", "content"]},
            },
            "read_project_source": {
                "name": "read_project_source",
                "description": "Read one bounded source file only from allowlisted Core/test source folders such as core/ or tests/. This tool cannot read workspace files. For mentor_baseline/* use read_workspace_file instead.",
                "parameters": {"type": "object", "properties": {"relative_path": {"type": "string"}, "start_line": {"type": "integer", "minimum": 1}, "max_lines": {"type": "integer", "minimum": 1, "maximum": 220}}, "required": ["relative_path"]},
            },
            "run_python_compile": {
                "name": "run_python_compile",
                "description": "Run Python syntax compilation only (py_compile) for one or more .py files inside the self-development workspace. Does not execute Python code. relative_paths must be a JSON array of strings.",
                "parameters": {"type": "object", "properties": {"relative_paths": {"type": "array", "items": {"type": "string"}}}, "required": ["relative_paths"]},
            },
            "validate_powershell_syntax": {
                "name": "validate_powershell_syntax",
                "description": "Parse one .ps1 file inside the self-development workspace for syntax errors only. Does not execute the script.",
                "parameters": {"type": "object", "properties": {"relative_path": {"type": "string"}}, "required": ["relative_path"]},
            },
            "run_safe_powershell": {
                "name": "run_safe_powershell",
                "description": "Run only the strict allowlisted non-destructive PowerShell subset inside the self-development workspace as the current non-admin user. Never use for custom, destructive, network, external-process, registry, credential, startup, or administrator actions.",
                "parameters": {"type": "object", "properties": {"relative_path": {"type": "string"}}, "required": ["relative_path"]},
            },
        }
        tool_specs = [
            {"type": "function", "function": descriptions[name]}
            for name in allowed
        ]

        profile_parts = ["workspace_read"]
        if workspace_listing_requested:
            profile_parts.append("workspace_discovery")
        if write_intent:
            profile_parts.append("workspace_write")
        if source_requested:
            profile_parts.append("core_source_read")
        if python_compile_requested:
            profile_parts.append("python_compile")
        if powershell_syntax_requested:
            profile_parts.append("powershell_syntax")
        if safe_powershell_requested:
            profile_parts.append("safe_powershell")
        profile = "+".join(profile_parts)
        return tool_specs, profile, frozenset(allowed)

    @staticmethod
    def _tool_is_exposed(name: str, exposed_tool_names: set[str] | frozenset[str]) -> bool:
        return isinstance(name, str) and name in exposed_tool_names

    @staticmethod
    def _message_char_cost(message: dict[str, Any]) -> int:
        try:
            return len(json.dumps(message, ensure_ascii=False, separators=(",", ":")))
        except (TypeError, ValueError):
            return len(str(message))

    @staticmethod
    def _compact_message(message: dict[str, Any]) -> dict[str, Any]:
        """Bound message bodies while preserving tool-call structure and IDs."""
        compact = json.loads(json.dumps(message, ensure_ascii=False))
        role = compact.get("role", "")
        cap = AGENT_TOOL_RESULT_CHAR_LIMIT if role == "tool" else (3500 if role == "user" else 2500)
        content = compact.get("content")
        if isinstance(content, str) and len(content) > cap:
            compact["content"] = content[:cap] + f"\n[TRUNCATED: retained first {cap} characters for context control]"
        calls = compact.get("tool_calls")
        if isinstance(calls, list):
            for call in calls:
                fn = call.get("function") if isinstance(call, dict) else None
                if isinstance(fn, dict):
                    args = fn.get("arguments")
                    if isinstance(args, str) and len(args) > 1800:
                        fn["arguments"] = args[:1800] + "[TRUNCATED]"
        return compact

    def _prepare_model_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Keep task instructions and newest complete tool exchanges within a small context budget."""
        if not isinstance(messages, list) or not messages:
            raise ValueError("Agent message history is empty or invalid.")
        system_index = next((i for i, m in enumerate(messages) if m.get("role") == "system"), None)
        first_user_index = next((i for i, m in enumerate(messages) if m.get("role") == "user"), None)
        pinned_mentor_index = next((
            i for i in range(len(messages) - 1, -1, -1)
            if messages[i].get("role") == "user"
            and str(messages[i].get("content", "")).startswith("MENTOR FEEDBACK / NEXT INSTRUCTIONS:")
            and i != first_user_index
        ), None)

        base: list[dict[str, Any]] = []
        excluded: set[int] = set()
        if system_index is not None:
            base.append(self._compact_message(messages[system_index]))
            excluded.add(system_index)
        if first_user_index is not None:
            base.append(self._compact_message(messages[first_user_index]))
            excluded.add(first_user_index)
        pinned_mentor = None
        if pinned_mentor_index is not None:
            pinned_mentor = self._compact_message(messages[pinned_mentor_index])
            excluded.add(pinned_mentor_index)

        remaining = [m for i, m in enumerate(messages) if i not in excluded]
        groups: list[list[dict[str, Any]]] = []
        i = 0
        while i < len(remaining):
            block = [remaining[i]]
            current = remaining[i]
            i += 1
            if current.get("role") == "assistant" and current.get("tool_calls"):
                while i < len(remaining) and remaining[i].get("role") == "tool":
                    block.append(remaining[i])
                    i += 1
            groups.append(block)

        selected: list[list[dict[str, Any]]] = []
        used = sum(self._message_char_cost(m) for m in base)
        count = len(base)
        for block in reversed(groups):
            compact_block = [self._compact_message(m) for m in block]
            block_cost = sum(self._message_char_cost(m) for m in compact_block)
            if count + len(compact_block) > AGENT_CONTEXT_MESSAGE_BUDGET or used + block_cost > AGENT_CONTEXT_CHAR_BUDGET:
                # Try one tighter version of the newest exchange; omit older oversized blocks whole.
                if not selected:
                    tighter = []
                    for item in compact_block:
                        copy_item = dict(item)
                        if isinstance(copy_item.get("content"), str):
                            copy_item["content"] = copy_item["content"][:900] + "\n[CONTEXT WINDOW TRIMMED]"
                        tighter.append(copy_item)
                    tight_cost = sum(self._message_char_cost(m) for m in tighter)
                    if count + len(tighter) <= AGENT_CONTEXT_MESSAGE_BUDGET and used + tight_cost <= AGENT_CONTEXT_CHAR_BUDGET:
                        selected.append(tighter)
                        used += tight_cost
                        count += len(tighter)
                break
            selected.append(compact_block)
            used += block_cost
            count += len(compact_block)

        flattened = base + [item for block in reversed(selected) for item in block]
        if len(flattened) < len(messages):
            flattened.insert(len(base), {
                "role": "user",
                "content": "CONTEXT NOTE: Older interaction/tool history was omitted or shortened to fit the model context. "
                           "Trust current task instructions and retained recent tool results; inspect the workspace before repeating any write.",
            })
        if pinned_mentor is not None:
            flattened.append(pinned_mentor)
        return flattened

    def _validate_tool_arguments(self, name: str, args: Any) -> dict[str, Any]:
        """Reject malformed or invented tool arguments before invoking a handler."""
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError as exc:
                raise ValueError("Tool arguments must be a JSON object; do not use Python list/dict syntax.") from exc
        if not isinstance(args, dict):
            raise ValueError("Tool arguments must be a JSON object.")
        schemas = {
            "list_workspace_files": ({}, {"relative_dir": str}),
            "read_workspace_file": ({"relative_path": str}, {}),
            "read_project_source": ({"relative_path": str}, {"start_line": int, "max_lines": int}),
            "write_workspace_file": ({"relative_path": str, "content": str}, {}),
            "run_python_compile": ({"relative_paths": list}, {}),
            "validate_powershell_syntax": ({"relative_path": str}, {}),
            "run_safe_powershell": ({"relative_path": str}, {}),
            "propose_powershell_execution": ({"relative_path": str, "purpose": str}, {}),
            "propose_core_promotion": ({"source_relative_path": str, "destination_relative_path": str, "purpose": str}, {}),
        }
        if name not in schemas:
            raise ValueError(f"Tool '{name}' is not in the local self-development allowlist.")
        required, optional = schemas[name]
        missing = [key for key in required if key not in args]
        extras = [key for key in args if key not in required and key not in optional]
        if missing:
            raise ValueError("Missing required argument(s): " + ", ".join(missing))
        if extras:
            raise ValueError("Unexpected argument(s): " + ", ".join(extras))
        for key, expected in {**required, **optional}.items():
            if key not in args:
                continue
            value = args[key]
            if expected is int:
                if not isinstance(value, int) or isinstance(value, bool):
                    raise ValueError(f"Argument '{key}' must be an integer.")
            elif expected is list:
                if not isinstance(value, list) or not 1 <= len(value) <= 8 or not all(isinstance(x, str) for x in value):
                    raise ValueError("relative_paths must be a JSON array containing 1 to 8 strings.")
            elif not isinstance(value, expected):
                raise ValueError(f"Argument '{key}' must be {expected.__name__}.")
        if name == "read_project_source":
            if args.get("start_line", 1) < 1 or not 1 <= args.get("max_lines", 160) <= 220:
                raise ValueError("read_project_source line window is outside the allowed range.")
        return args

    @staticmethod
    def _looks_like_text_tool_request(content: str) -> bool:
        """Detect the observed JSON-as-text tool envelope without executing it."""
        text = str(content or "").strip()
        return (
            text.startswith("{")
            and bool(re.search(r'"name"\s*:', text))
            and bool(re.search(r'"parameters"\s*:', text))
        )

    def _parse_text_tool_request(
        self,
        content: str,
        done_reason: str = "",
        *,
        exposed_tool_names: set[str] | frozenset[str] | None = None,
    ) -> tuple[str, dict[str, Any]]:
        """Parse one exact text envelope, limited to safe workspace/read/validation tools.

        This is a compatibility path for models that serialize a tool request as text.
        It never accepts PowerShell execution, approval, promotion, or arbitrary commands.
        """
        text = str(content or "").strip()
        if not self._looks_like_text_tool_request(text):
            raise ValueError("Response is not a recognized JSON tool-request envelope.")
        if str(done_reason or "").lower() == "length":
            raise ValueError("Text tool request was truncated at the model output limit; no action was dispatched.")
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError("Text tool request is incomplete or invalid JSON; no action was dispatched.") from exc
        if not isinstance(payload, dict) or set(payload.keys()) != {"name", "parameters"}:
            raise ValueError("Text tool request must contain exactly 'name' and 'parameters'.")
        name = payload.get("name")
        if not isinstance(name, str) or name not in AGENT_TEXT_TOOL_FALLBACK_ALLOWLIST:
            raise ValueError(
                "Text fallback permits only the safe workspace/read/validation tools. "
                "PowerShell execution and Core promotion require native tool calls and existing approval controls."
            )
        args = self._validate_tool_arguments(name, payload.get("parameters"))
        if exposed_tool_names is not None and not self._tool_is_exposed(name, exposed_tool_names):
            raise ValueError(f"Tool '{name}' is not exposed by the current task-specific tool profile.")
        return name, args

    def _request_ollama_stream(self, task_id: str, messages: list[dict[str, Any]], turn: int) -> tuple[dict[str, Any], dict[str, Any]]:
        """Stream one bounded Ollama turn, recording progress without exposing chain-of-thought text."""
        model_messages = self._prepare_model_messages(messages)
        tool_specs, tool_profile, exposed_tool_names = self._model_tools(task_id)
        profile_note = (
            "CURRENT TASK TOOL PROFILE: " + tool_profile + ".\n"
            "Only these tools are available for this task: " + ", ".join(sorted(exposed_tool_names)) + ".\n"
            "Use read_workspace_file for workspace files such as mentor_baseline/*. Never use read_project_source for workspace paths. "
            "Use read_project_source only when an allowlisted Core/test source path is explicitly part of the task. "
            "If the required operation is not represented by an available tool, stop and report the limitation; do not invent a tool."
        )
        system_index = next((i for i, item in enumerate(model_messages) if item.get("role") == "system"), None)
        if system_index is not None:
            model_messages[system_index]["content"] = str(model_messages[system_index].get("content", "")) + "\n\n" + profile_note
        else:
            model_messages.insert(0, {"role": "system", "content": profile_note})
        _event(
            task_id,
            "tool_profile_selected",
            f"Selected task-specific tool profile '{tool_profile}'.",
            json.dumps({"profile": tool_profile, "available_tools": sorted(exposed_tool_names)}, ensure_ascii=False),
        )
        payload = {
            "model": self.model,
            "messages": model_messages,
            "tools": tool_specs,
            "stream": True,
            "keep_alive": "10m",
            "options": {
                "temperature": 0.2,
                "num_ctx": 4096,
                "num_predict": AGENT_NUM_PREDICT,
            },
        }
        _event(task_id, "agent_stream_started", f"Ollama streaming generation started (turn {turn}; messages={len(model_messages)}; chars={sum(self._message_char_cost(x) for x in model_messages)}).")
        content_parts: list[str] = []
        calls: dict[int, dict[str, Any]] = {}
        metrics: dict[str, Any] = {}
        chunk_count = 0
        char_count = 0
        last_progress = time.monotonic()
        response = None
        try:
            response = requests.post(
                f"{self.ollama_url}/api/chat", json=payload, stream=True,
                timeout=(OLLAMA_CONNECT_TIMEOUT, OLLAMA_STREAM_IDLE_TIMEOUT),
                allow_redirects=False,
            )
            if not 200 <= response.status_code < 300:
                body = getattr(response, "text", "")
                raise RuntimeError(f"Local Ollama returned HTTP {response.status_code}; redirects are disabled. {body[:500]}")
            for raw_line in response.iter_lines(decode_unicode=True):
                if not raw_line:
                    continue
                if isinstance(raw_line, bytes):
                    raw_line = raw_line.decode("utf-8", errors="replace")
                chunk = json.loads(raw_line)
                if chunk.get("error"):
                    raise RuntimeError(f"Ollama stream error: {str(chunk['error'])[:500]}")
                msg = chunk.get("message") or {}
                part = msg.get("content") or ""
                if part:
                    content_parts.append(str(part))
                    char_count += len(str(part))
                for idx, call in enumerate(msg.get("tool_calls") or []):
                    if not isinstance(call, dict):
                        continue
                    index = call.get("index", idx)
                    if not isinstance(index, int):
                        index = idx
                    target = calls.setdefault(index, {"type": "function", "function": {"name": "", "arguments": {}}})
                    fn = call.get("function") or {}
                    if fn.get("name"):
                        target["function"]["name"] = fn["name"]
                    incoming = fn.get("arguments")
                    if isinstance(incoming, dict):
                        existing = target["function"].get("arguments")
                        if not isinstance(existing, dict):
                            existing = {}
                        existing.update(incoming)
                        target["function"]["arguments"] = existing
                    elif isinstance(incoming, str):
                        existing = target["function"].get("arguments")
                        if isinstance(existing, str):
                            target["function"]["arguments"] = existing + incoming
                        elif isinstance(existing, dict) and not existing:
                            target["function"]["arguments"] = incoming
                        else:
                            target["function"]["arguments"] = incoming
                chunk_count += 1
                for key in ("prompt_eval_count", "prompt_eval_duration", "eval_count", "eval_duration", "load_duration", "total_duration", "done_reason"):
                    if key in chunk:
                        metrics[key] = chunk[key]
                if time.monotonic() - last_progress >= 5:
                    _event(task_id, "agent_stream_progress", f"Ollama is generating (turn {turn}; chunks={chunk_count}; output_chars={char_count}).")
                    last_progress = time.monotonic()
                if chunk.get("done") is True:
                    break
        finally:
            if response is not None:
                response.close()
        if not chunk_count:
            raise RuntimeError("Ollama stream ended without any response chunks.")
        tool_calls = [calls[idx] for idx in sorted(calls)]
        assistant = {"content": "".join(content_parts), "tool_calls": tool_calls}
        if not assistant["content"].strip() and not tool_calls:
            raise RuntimeError("Ollama stream returned neither assistant content nor tool calls.")
        metrics.update({
            "stream_chunks": chunk_count,
            "output_chars": char_count,
            "tool_profile": tool_profile,
            "exposed_tool_names": sorted(exposed_tool_names),
            "tool_call_count": len(tool_calls),
            "tool_call_names": [str(call.get("function", {}).get("name", "")) for call in tool_calls],
        })
        _event(
            task_id,
            "agent_stream_complete",
            (
                f"Ollama turn completed (turn {turn}; chunks={chunk_count}; "
                f"output_chars={char_count}; prompt_tokens={metrics.get('prompt_eval_count', 'n/a')}; "
                f"eval_tokens={metrics.get('eval_count', 'n/a')}; "
                f"done_reason={metrics.get('done_reason', 'unknown')}; "
                f"total_ms={round(int(metrics.get('total_duration', 0) or 0) / 1_000_000)})."
            ),
        )
        return assistant, metrics

    def run_task(self, task_id: str, max_model_turns: int = 3) -> str:
        if not self._run_lock.acquire(blocking=False):
            raise RuntimeError("Another self-development task is already running. Wait for it to finish or stop it first.")
        try:
            return self._run_task_locked(task_id, max_model_turns)
        finally:
            self._active_task_id = ""
            try:
                with _db() as con:
                    con.execute("UPDATE dev_runtime SET active_task_id='' WHERE singleton_id=1")
            except Exception:
                logging.exception("Could not clear active task indicator")
            self._run_lock.release()

    def _run_task_locked(self, task_id: str, max_model_turns: int = 5) -> str:
        _require_enabled()
        if not isinstance(max_model_turns, int) or isinstance(max_model_turns, bool) or not 1 <= max_model_turns <= MAX_MODEL_TURNS:
            raise ValueError(f"max_model_turns must be from 1 to {MAX_MODEL_TURNS}.")
        with _db() as con:
            task = con.execute("SELECT * FROM dev_tasks WHERE task_id=?", (task_id.strip(),)).fetchone()
            rt = con.execute("SELECT tool_calling_supported FROM dev_runtime WHERE singleton_id=1").fetchone()
        if task is None:
            raise ValueError("Development task not found.")
        if task["status"] in {"completed", "stopped", "blocked"}:
            return f"Task {task_id} cannot run from status={task['status']}."
        if not bool(rt["tool_calling_supported"]):
            raise RuntimeError("Local model tool calling has not been confirmed. Run claud_dev_probe_tool_calling first.")
        messages = json.loads(task["agent_messages_json"])
        self._active_task_id = task_id
        with _db() as con:
            con.execute("UPDATE dev_tasks SET status='running',updated_at=? WHERE task_id=?", (now(), task_id))
            con.execute("UPDATE dev_runtime SET active_task_id=? WHERE singleton_id=1", (task_id,))
        for turn in range(max_model_turns):
            _require_enabled()
            with _db() as con:
                task_now = con.execute("SELECT action_count FROM dev_tasks WHERE task_id=?", (task_id,)).fetchone()
            actions_used = int(task_now["action_count"])
            if actions_used >= MAX_TASK_ACTIONS:
                return self._finish_task_turn(task_id, messages, "Action budget reached; mentor review required.", "needs_mentor_review")
            _event(task_id, "agent_thinking", f"Claud local development agent is reasoning (model turn {turn + 1}/{max_model_turns}).")
            try:
                assistant, stream_metrics = self._request_ollama_stream(task_id, messages, turn + 1)
                metric_summary = {
                    key: stream_metrics.get(key)
                    for key in (
                        "prompt_eval_count", "eval_count", "done_reason",
                        "load_duration", "prompt_eval_duration", "eval_duration", "total_duration",
                        "stream_chunks", "output_chars", "tool_profile", "exposed_tool_names",
                        "tool_call_count", "tool_call_names",
                    )
                    if key in stream_metrics
                }
                _event(
                    task_id,
                    "agent_stream_metrics",
                    f"Recorded Ollama metrics for model turn {turn + 1}.",
                    json.dumps(metric_summary, ensure_ascii=False),
                )
                control_after_inference = _get_control()
                if control_after_inference["state"] != "enabled":
                    paused_status = "paused" if control_after_inference["state"] == "paused" else "stopped"
                    self._active_task_id = ""
                    with _db() as con:
                        con.execute("UPDATE dev_tasks SET status=?,updated_at=? WHERE task_id=?", (paused_status, now(), task_id))
                    _event(task_id, "task_stopped", f"Agent step halted after controller state became {paused_status}.")
                    return f"TASK HALTED: {paused_status.upper()}\nTask ID: {task_id}\nNo additional tool action was executed."
            except Exception as exc:
                logging.exception("Self-development agent model request failed")
                control_after_error = _get_control()
                if control_after_error["state"] != "enabled":
                    halted_status = "paused" if control_after_error["state"] == "paused" else "stopped"
                    self._save_agent_messages(task_id, messages)
                    with _db() as con:
                        con.execute("UPDATE dev_tasks SET status=?,updated_at=? WHERE task_id=?", (halted_status, now(), task_id))
                    _event(task_id, "task_paused" if halted_status == "paused" else "task_stopped", f"Model request ended after controller changed to {halted_status}.")
                    return f"TASK HALTED: {halted_status.upper()}\nTask ID: {task_id}\nNo subsequent tool action was performed."
                recovery_note = (
                    f"Model request failed: {type(exc).__name__}: {exc}. "
                    "Checkpoint preserved. After mentor feedback, resume only in a short increment; "
                    "inspect existing workspace artifacts before repeating any write."
                )
                _event(task_id, "model_request_interrupted", recovery_note)
                return self._finish_task_turn(task_id, messages, recovery_note, "blocked")
            calls = assistant.get("tool_calls") or []
            content = str(assistant.get("content") or "").strip()
            exposed_tool_names = frozenset(stream_metrics.get("exposed_tool_names") or [])
            dispatch_mode = "native_tool_call" if calls else "assistant_text"

            # Ollama-native tool calls arrive in message.tool_calls. A few model turns
            # have instead emitted {"name": ..., "parameters": ...} as ordinary text.
            # Only an exact, complete JSON envelope for the safe workspace subset is
            # eligible for normalization. Truncated/invalid envelopes are never run.
            if not calls and self._looks_like_text_tool_request(content):
                done_reason = str(stream_metrics.get("done_reason") or "")
                try:
                    text_tool_name, text_tool_args = self._parse_text_tool_request(
                        content,
                        done_reason,
                        exposed_tool_names=exposed_tool_names,
                    )
                except (TypeError, ValueError) as exc:
                    reason = f"{type(exc).__name__}: {exc}"
                    _event(
                        task_id,
                        "agent_text_tool_request_rejected",
                        f"Rejected JSON-as-text tool request on turn {turn + 1}: {reason}",
                        f"No tool was executed. output_chars={len(content)}; done_reason={done_reason or 'unknown'}.",
                    )
                    messages.append({"role": "assistant", "content": content[:AGENT_TOOL_RESULT_CHAR_LIMIT]})
                    messages.append({
                        "role": "user",
                        "content": (
                            "TOOL DISPATCH CORRECTION: Your previous response described a tool request as ordinary text. "
                            "No action was executed. Use a native tool call through message.tool_calls, not JSON in content. "
                            "Call exactly one registered tool with arguments matching its schema. Keep the call concise. "
                            "Do not attempt PowerShell execution or Core promotion through text fallback."
                        ),
                    })
                    self._save_agent_messages(task_id, messages)
                    if turn + 1 < max_model_turns:
                        continue
                    return self._finish_task_turn(
                        task_id,
                        messages,
                        f"Tool request was not dispatched: {reason}. No workspace action was executed.",
                        "needs_mentor_review",
                    )
                else:
                    _event(
                        task_id,
                        "agent_text_tool_request_normalized",
                        f"Normalized complete JSON-as-text request into safe tool call '{text_tool_name}'.",
                        "Arguments passed the existing tool schema validator; dispatch remains workspace-scoped.",
                    )
                    calls = [{
                        "type": "function",
                        "function": {"name": text_tool_name, "arguments": text_tool_args},
                    }]
                    dispatch_mode = "validated_text_fallback"
                    content = ""

            if not calls:
                report = content or "Model returned neither native tool calls nor a final report."
                messages.append({"role": "assistant", "content": report})
                return self._finish_task_turn(task_id, messages, report, "needs_mentor_review")
            messages.append({"role": "assistant", "content": content, "tool_calls": calls})
            for call in calls:
                _require_enabled()
                with _db() as con:
                    action_row = con.execute("SELECT action_count FROM dev_tasks WHERE task_id=?", (task_id,)).fetchone()
                if action_row is None or int(action_row["action_count"]) >= MAX_TASK_ACTIONS:
                    return self._finish_task_turn(task_id, messages, "Action budget reached; mentor review required.", "needs_mentor_review")
                fn = call.get("function", {}) if isinstance(call, dict) else {}
                name = fn.get("name", "") if isinstance(fn, dict) else ""
                args = fn.get("arguments", {}) if isinstance(fn, dict) else {}
                _event(
                    task_id,
                    "agent_tool_call_received",
                    f"Received {dispatch_mode} for tool '{str(name)[:120]}'.",
                    json.dumps({
                        "tool_name": str(name)[:120],
                        "dispatch_mode": dispatch_mode,
                        "tool_profile": stream_metrics.get("tool_profile", "unknown"),
                        "exposed_tools": sorted(exposed_tool_names),
                    }, ensure_ascii=False),
                )
                handler = self._tool_handlers.get(name)
                if not self._tool_is_exposed(name, exposed_tool_names):
                    result = f"DENIED: Tool '{str(name)[:120]}' was not exposed for this task profile. No handler was invoked. Use only tools listed in the current task schema."
                    _event(task_id, "tool_dispatch_unexposed_rejected", f"Rejected tool '{str(name)[:120]}' because it was not exposed for this task.")
                elif handler is None:
                    result = f"DENIED: Tool '{name}' is not in the local self-development allowlist. Use only the exact supplied tool names."
                    _event(task_id, "tool_call_validation_failed", f"Rejected unknown tool name: {str(name)[:120]}.")
                else:
                    try:
                        validated_args = self._validate_tool_arguments(name, args)
                    except (TypeError, ValueError) as exc:
                        logging.warning("Rejected malformed tool call %s: %s", name, exc)
                        result = f"TOOL ARGUMENT ERROR: {type(exc).__name__}: {exc}. Correct the arguments to match the advertised JSON schema; do not invent tool names or encode arrays as strings."
                        _event(task_id, "tool_call_validation_failed", f"Rejected malformed arguments for {name}: {str(exc)[:500]}.")
                    else:
                        _event(task_id, "tool_dispatch_validated", f"Validated arguments for exposed tool '{name}'.")
                        try:
                            result = str(handler(**validated_args))
                            args = validated_args
                            if result.startswith(("TOOL ERROR:", "ERROR:", "DENIED:", "TOOL ARGUMENT ERROR:")):
                                _event(task_id, "tool_dispatch_failed", f"Tool '{name}' returned an error.", result[:1500])
                            else:
                                _event(task_id, "tool_dispatch_completed", f"Tool '{name}' completed.", result[:1500])
                        except Exception as exc:
                            logging.exception("Self-development tool %s failed", name)
                            result = f"TOOL ERROR: {type(exc).__name__}: {exc}"
                            _event(task_id, "tool_dispatch_failed", f"Tool '{name}' raised {type(exc).__name__}.", result[:1500])
                messages.append({"role": "tool", "tool_name": name, "content": result[:AGENT_TOOL_RESULT_CHAR_LIMIT]})
                self._save_agent_messages(task_id, messages)
                self._record_task_action(task_id, name, json.dumps(args, ensure_ascii=False), result)
                _require_enabled()
            self._save_agent_messages(task_id, messages)
        return self._finish_task_turn(task_id, messages, "Model-turn budget reached. Ask the mentor to review and continue if appropriate.", "needs_mentor_review")

    def _save_agent_messages(self, task_id: str, messages: list[dict[str, Any]]) -> None:
        # Keep bounded history so the local agent cannot expand context indefinitely.
        with _db() as con:
            con.execute(
                "UPDATE dev_tasks SET agent_messages_json=?,updated_at=? WHERE task_id=?",
                (json.dumps(messages[-40:], ensure_ascii=False), now(), task_id),
            )

    def _record_task_action(self, task_id: str, name: str, detail: str, result: str) -> None:
        with _db() as con:
            con.execute("UPDATE dev_tasks SET action_count=action_count+1,updated_at=? WHERE task_id=?", (now(), task_id))
        _event(task_id, name, detail, result)

    def _finish_task_turn(self, task_id: str, messages: list[dict[str, Any]], report: str, status: str) -> str:
        self._save_agent_messages(task_id, messages)
        with _db() as con:
            con.execute("UPDATE dev_tasks SET status=?,last_result=?,updated_at=? WHERE task_id=?", (status, report[:12000], now(), task_id))
        event_name = "task_review_required" if status == "needs_mentor_review" else ("task_stopped" if status == "stopped" else "task_paused" if status == "paused" else "task_blocked")
        _event(task_id, event_name, f"Task step ended with status={status}", report)
        self._active_task_id = ""
        return f"TASK STEP FINISHED\nTask ID: {task_id}\nStatus: {status}\n\n{report}"

    def list_workspace_files(self, relative_dir: str = "") -> str:
        base = WORKSPACE
        if relative_dir.strip():
            rel = _canonical_relative(relative_dir)
            base = WORKSPACE.joinpath(*rel.split("/"))
            if not _within(base, WORKSPACE) or not base.is_dir() or _has_reparse_component(base, WORKSPACE):
                raise ValueError("Workspace directory not found or not permitted.")
        results = []
        for p in sorted(base.rglob("*")):
            if len(results) >= 100:
                break
            if p.is_file() and _within(p, WORKSPACE) and not _has_reparse_component(p, WORKSPACE):
                results.append(f"{p.relative_to(WORKSPACE).as_posix()} ({p.stat().st_size} bytes)")
        return "\n".join(results) if results else "Workspace is empty."

    def read_workspace_file(self, relative_path: str) -> str:
        path = _workspace_path(relative_path, must_exist=True)
        if path.stat().st_size > MAX_FILE_BYTES:
            raise ValueError("File exceeds read limit.")
        return path.read_text(encoding="utf-8-sig")

    def read_project_source(
        self, relative_path: str, start_line: int = 1, max_lines: int = 160,
    ) -> str:
        """Read a bounded line window from one allowlisted Core/test source file."""
        if not isinstance(start_line, int) or isinstance(start_line, bool) or start_line < 1:
            raise ValueError("start_line must be a positive integer.")
        if not isinstance(max_lines, int) or isinstance(max_lines, bool) or not 1 <= max_lines <= 220:
            raise ValueError("max_lines must be between 1 and 220.")
        path = _source_path(relative_path)
        if not path.is_file():
            raise ValueError("Provide one allowlisted source file, not a directory.")
        with path.open("r", encoding="utf-8-sig", errors="replace") as stream:
            lines = stream.readlines()
        total = len(lines)
        start = min(start_line, max(total, 1))
        end = min(total, start + max_lines - 1)
        selected = lines[start - 1:end]
        content = "".join(selected)
        if len(content) > MAX_SOURCE_CHARS:
            raise ValueError("Selected source window exceeds response limit; request fewer lines.")
        numbered = "".join(f"{idx:5d}: {line}" for idx, line in enumerate(selected, start=start))
        return (
            f"SOURCE: {relative_path}\nSHA256: {sha256_file(path)}\n"
            f"LINES: {start}-{end} of {total}\n\n{numbered}"
        )

    def write_workspace_file(self, relative_path: str, content: str) -> str:
        _require_enabled()
        if not isinstance(content, str) or len(content.encode("utf-8")) > MAX_FILE_BYTES:
            raise ValueError("Content must be text no larger than 120 KB.")
        if "\x00" in content:
            raise ValueError("NUL bytes are not allowed.")
        path = _workspace_path(relative_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if _has_reparse_component(path.parent, WORKSPACE):
            raise ValueError("Reparse points are not allowed in workspace paths.")
        temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            _write_exclusive(temp, content)
            # Re-resolve both paths, then serialize the final replace against Pause/Stop.
            path = _workspace_path(relative_path)
            if _has_reparse_component(path.parent, WORKSPACE):
                raise ValueError("Workspace path changed to a reparse point during write.")
            with _db() as con:
                con.execute("BEGIN IMMEDIATE")
                control = con.execute("SELECT state,reason FROM dev_control WHERE singleton_id=1").fetchone()
                if not control or control["state"] != "enabled":
                    raise RuntimeError(f"Self-development is {control['state'] if control else 'unavailable'}; staged file was not applied.")
                os.replace(temp, path)
        finally:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                logging.exception("Could not remove workspace staging file %s", temp)
        result = f"WROTE {path.relative_to(WORKSPACE).as_posix()} ({path.stat().st_size} bytes; SHA256 {sha256_file(path)})"
        return result

    def run_python_compile(self, relative_paths: list[str]) -> str:
        _require_enabled()
        if not isinstance(relative_paths, list) or not 1 <= len(relative_paths) <= 8:
            raise ValueError("Provide 1 to 8 workspace .py paths.")
        if _is_admin():
            raise PermissionError("Python validation refused: the MCP process is elevated. Restart ChatGPT Desktop without Run as administrator.")
        outputs = []
        for rel in relative_paths:
            path = _workspace_path(rel, must_exist=True)
            if path.suffix.lower() != ".py":
                raise ValueError("Only workspace .py files can be compiled.")
            proc = subprocess.run(
                [sys.executable, "-I", "-m", "py_compile", str(path)],
                cwd=str(WORKSPACE), capture_output=True, text=True, timeout=30, check=False,
            )
            outputs.append(f"{rel}: {'PASS' if proc.returncode == 0 else 'FAIL'}\n{(proc.stdout + proc.stderr).strip()}")
            if proc.returncode != 0:
                break
        return "\n\n".join(outputs)

    def validate_powershell_syntax(self, relative_path: str) -> str:
        # Syntax validation is read-only and may be used to inspect a draft while paused.
        path = _workspace_path(relative_path, must_exist=True)
        if path.suffix.lower() != ".ps1":
            raise ValueError("Only workspace .ps1 files can be syntax-checked.")
        powershell = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
        if not powershell:
            return "POWERSHELL UNAVAILABLE: Script was not executed. Install/enable Windows PowerShell or PowerShell 7 to validate syntax."
        path64 = __import__("base64").b64encode(str(path).encode("utf-8")).decode("ascii")
        command = (
            "$p=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('" + path64 + "')); "
            "$t=$null;$e=$null;[System.Management.Automation.Language.Parser]::ParseFile($p,[ref]$t,[ref]$e)|Out-Null; "
            "if($e.Count){$e|ForEach-Object{Write-Output $_.Message};exit 1}else{Write-Output 'SYNTAX_OK';exit 0}"
        )
        encoded = __import__("base64").b64encode(command.encode("utf-16le")).decode("ascii")
        proc = subprocess.run(
            [powershell, "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
            cwd=str(WORKSPACE), capture_output=True, text=True, timeout=20, check=False,
        )
        text = (proc.stdout + proc.stderr).strip()
        return f"{'PASS' if proc.returncode == 0 and 'SYNTAX_OK' in text else 'FAIL'}\n{text[:4000]}"

    def _check_safe_powershell_policy(self, path: Path) -> None:
        powershell = shutil.which("powershell.exe")
        if not powershell:
            raise RuntimeError("Windows PowerShell is required for the safe PowerShell AST check.")
        checker = DEV_DIR / "powershell_safe_check.ps1"
        if not checker.is_file():
            raise FileNotFoundError("Trusted safe PowerShell checker is missing.")
        proc = subprocess.run(
            [powershell, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(checker),
             "-TargetFile", str(path), "-WorkspaceRoot", str(WORKSPACE)],
            cwd=str(WORKSPACE), capture_output=True, text=True, timeout=20, check=False,
        )
        output = (proc.stdout + proc.stderr).strip()
        if proc.returncode != 0 or "SAFE_POLICY_OK" not in output:
            raise PermissionError("Safe PowerShell policy validation failed. " + output[:3000])

    def _agent_run_safe_powershell(self, relative_path: str) -> str:
        if not self._active_task_id:
            raise RuntimeError("No active development task context.")
        return self.run_safe_powershell(self._active_task_id, relative_path)

    def run_safe_powershell(self, task_id: str, relative_path: str) -> str:
        _require_enabled()
        if not isinstance(task_id, str) or not task_id.strip():
            raise ValueError("task_id is required.")
        with _db() as con:
            task = con.execute("SELECT status FROM dev_tasks WHERE task_id=?", (task_id,)).fetchone()
        if task is None:
            raise ValueError("Development task not found.")
        if task["status"] not in {"queued", "running"}:
            raise RuntimeError(f"Safe PowerShell is permitted only for queued/running tasks; current status={task['status']}.")
        script = _workspace_path(relative_path, must_exist=True)
        if script.suffix.lower() != ".ps1":
            raise ValueError("Only a workspace .ps1 file can be executed.")
        if _is_admin():
            raise PermissionError("Safe PowerShell execution is refused when ChatGPT/MCP runs with an elevated administrator token.")
        digest_before = sha256_file(script)
        self._check_safe_powershell_policy(script)
        if sha256_file(script) != digest_before:
            raise RuntimeError("Script changed during policy validation; no execution was started.")
        _require_enabled()
        powershell = shutil.which("powershell.exe")
        if not powershell:
            raise RuntimeError("Windows PowerShell (powershell.exe) was not found.")
        proc = subprocess.Popen(
            [powershell, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script)],
            cwd=str(WORKSPACE), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        with _db() as con:
            con.execute("UPDATE dev_runtime SET active_powershell_pid=? WHERE singleton_id=1", (proc.pid,))
        deadline = time.monotonic() + 30
        stdout = stderr = ""
        try:
            while True:
                control = _get_control()
                if control["state"] != "enabled":
                    proc.kill()
                    stdout, stderr = proc.communicate()
                    raise RuntimeError(f"Safe PowerShell was interrupted by controller state={control['state']}.")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    proc.kill()
                    stdout, stderr = proc.communicate()
                    raise TimeoutError("Safe PowerShell exceeded 30 seconds and was terminated.")
                try:
                    stdout, stderr = proc.communicate(timeout=min(0.25, remaining))
                    break
                except subprocess.TimeoutExpired:
                    continue
        finally:
            with _db() as con:
                con.execute("UPDATE dev_runtime SET active_powershell_pid=0 WHERE singleton_id=1")
        result = (stdout + ("\nSTDERR:\n" + stderr if stderr else "")).strip()[:10000]
        _event(task_id, "powershell_safe_execute", f"Executed allowlisted non-destructive PowerShell script: {relative_path}", result)
        return f"SAFE POWERSHELL FINISHED\nExit code: {proc.returncode}\n{result}"

    def _check_powershell_policy(self, path: Path) -> None:
        source = path.read_text(encoding="utf-8-sig", errors="replace")
        for pattern in FORBIDDEN_PS_PATTERNS:
            match = re.search(pattern, source)
            if match:
                raise PermissionError(
                    "PowerShell policy blocked a restricted operation at review time. "
                    f"Matched pattern: {match.group(0)!r}. Administrator elevation, security-policy, credential, registry, and network actions are not available to the autonomous agent."
                )
        powershell = shutil.which("powershell.exe")
        if not powershell:
            raise RuntimeError("Windows PowerShell is required for AST policy validation.")
        checker = DEV_DIR / "powershell_policy_check.ps1"
        if not checker.is_file():
            raise FileNotFoundError("Trusted PowerShell policy checker is missing.")
        proc = subprocess.run(
            [powershell, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(checker),
             "-TargetFile", str(path), "-WorkspaceRoot", str(WORKSPACE)],
            cwd=str(WORKSPACE), capture_output=True, text=True, timeout=20, check=False,
        )
        output = (proc.stdout + proc.stderr).strip()
        if proc.returncode != 0 or "POLICY_OK" not in output:
            raise PermissionError("PowerShell AST policy validation failed. " + output[:3000])

    def _agent_propose_powershell_execution(self, relative_path: str, purpose: str) -> str:
        if not self._active_task_id:
            raise RuntimeError("No active development task context.")
        return self.propose_powershell_execution(self._active_task_id, relative_path, purpose)

    def _agent_propose_core_promotion(self, source_relative_path: str, destination_relative_path: str, purpose: str) -> str:
        if not self._active_task_id:
            raise RuntimeError("No active development task context.")
        return self.propose_core_promotion(self._active_task_id, source_relative_path, destination_relative_path, purpose)

    def propose_powershell_execution(self, task_id: str, relative_path: str, purpose: str) -> str:
        _require_enabled()
        if not isinstance(purpose, str) or not purpose.strip() or len(purpose) > 2000:
            raise ValueError("Provide a clear purpose (max 2,000 characters).")
        with _db() as con:
            task = con.execute("SELECT status FROM dev_tasks WHERE task_id=?", (task_id,)).fetchone()
        if task is None:
            raise ValueError("Development task not found.")
        if task["status"] in {"completed", "stopped", "blocked"}:
            raise RuntimeError(f"Cannot propose execution for task status={task['status']}.")
        path = _workspace_path(relative_path, must_exist=True)
        if path.suffix.lower() != ".ps1":
            raise ValueError("Only a workspace .ps1 script can be proposed for execution.")
        self._check_powershell_policy(path)
        syntax = self.validate_powershell_syntax(relative_path)
        if not syntax.startswith("PASS"):
            raise RuntimeError("PowerShell syntax check did not pass; script execution proposal was not created. " + syntax)
        digest = sha256_file(path)
        with _db() as con:
            cur = con.execute(
                "INSERT INTO dev_approvals(task_id,approval_type,source_rel,destination_rel,file_sha256,purpose,status,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (task_id, "powershell_execution", path.relative_to(WORKSPACE).as_posix(), "", digest, purpose.strip(), "pending", now()),
            )
            approval_id = int(cur.lastrowid)
        return (
            f"POWERSHELL EXECUTION APPROVAL REQUIRED\nApproval ID: {approval_id}\n"
            f"Script: {relative_path}\nSHA256: {digest}\nPurpose: {purpose.strip()}\n"
            "Nothing has been executed. Review locally with Review-ClaudDevelopmentApproval.ps1. "
            "Execution is non-admin only, one-time, workspace working directory, max 90 seconds."
        )

    def propose_core_promotion(
        self, task_id: str, source_relative_path: str, destination_relative_path: str, purpose: str,
    ) -> str:
        _require_enabled()
        if not isinstance(purpose, str) or not purpose.strip() or len(purpose) > 2000:
            raise ValueError("Provide a clear promotion rationale.")
        with _db() as con:
            if con.execute("SELECT 1 FROM dev_tasks WHERE task_id=?", (task_id,)).fetchone() is None:
                raise ValueError("Development task not found.")
        source = _workspace_path(source_relative_path, must_exist=True)
        destination = _promotion_path(destination_relative_path)
        digest = sha256_file(source)
        dest_hash = sha256_file(destination) if destination.is_file() else ""
        with _db() as con:
            cur = con.execute(
                "INSERT INTO dev_approvals(task_id,approval_type,source_rel,destination_rel,file_sha256,destination_sha256,purpose,status,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (task_id, "core_promotion", source.relative_to(WORKSPACE).as_posix(), destination.relative_to(ROOT).as_posix(), digest, dest_hash, purpose.strip(), "pending", now()),
            )
            approval_id = int(cur.lastrowid)
        return (
            f"CORE PROMOTION APPROVAL REQUIRED\nApproval ID: {approval_id}\n"
            f"Source: {source_relative_path}\nDestination: {destination_relative_path}\n"
            f"Source SHA256: {digest}\nDestination SHA256: {dest_hash or '(new file)'}\n"
            f"Purpose: {purpose.strip()}\nNothing in Core has been changed. Review locally with Review-ClaudDevelopmentApproval.ps1."
        )

    def list_approvals(self, status: str = "pending", limit: int = 20) -> str:
        if status not in {"pending", "approved", "executing", "executed", "applying", "applied", "failed", "rejected", "all"}:
            raise ValueError("Invalid approval status.")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 50:
            raise ValueError("limit must be from 1 to 50.")
        with _db() as con:
            if status == "all":
                rows = con.execute("SELECT * FROM dev_approvals ORDER BY approval_id DESC LIMIT ?", (limit,)).fetchall()
            else:
                rows = con.execute("SELECT * FROM dev_approvals WHERE status=? ORDER BY approval_id DESC LIMIT ?", (status, limit)).fetchall()
        if not rows:
            return f"No approvals with status={status}."
        return "\n\n---\n\n".join(
            f"ID: {r['approval_id']} | TYPE: {r['approval_type']} | STATUS: {r['status']}\n"
            f"Source: {r['source_rel']}\nDestination: {r['destination_rel']}\n"
            f"SHA256: {r['file_sha256']}\nPurpose: {r['purpose']}"
            for r in rows
        )

    def approval_details(self, approval_id: int) -> dict[str, Any]:
        with _db() as con:
            row = con.execute("SELECT * FROM dev_approvals WHERE approval_id=?", (approval_id,)).fetchone()
        if row is None:
            raise ValueError("Approval record not found.")
        data = dict(row)
        if data["approval_type"] == "powershell_execution":
            p = _workspace_path(data["source_rel"], must_exist=True)
            data["source_content"] = p.read_text(encoding="utf-8-sig", errors="replace")
        elif data["approval_type"] == "core_promotion":
            src = _workspace_path(data["source_rel"], must_exist=True)
            dest = _promotion_path(data["destination_rel"])
            before = dest.read_text(encoding="utf-8-sig", errors="replace").splitlines(True) if dest.is_file() else []
            after = src.read_text(encoding="utf-8-sig", errors="replace").splitlines(True)
            data["diff"] = "".join(difflib.unified_diff(before, after, fromfile=str(dest), tofile=str(src)))
        return data

    def mark_approval(self, approval_id: int, approve: bool, confirmation: str) -> str:
        if not isinstance(approval_id, int) or isinstance(approval_id, bool) or approval_id <= 0:
            raise ValueError("approval_id must be a positive integer.")
        expected = f"APPROVE {approval_id}" if approve else f"REJECT {approval_id}"
        if confirmation.strip() != expected:
            raise ValueError(f"Confirmation mismatch. Type exactly: {expected}")
        with _db() as con:
            row = con.execute("SELECT * FROM dev_approvals WHERE approval_id=?", (approval_id,)).fetchone()
            if row is None:
                raise ValueError("Approval record not found.")
            if row["status"] != "pending":
                raise ValueError(f"Approval is already {row['status']}.")
            cur = con.execute(
                "UPDATE dev_approvals SET status=?,reviewed_at=?,result=? WHERE approval_id=? AND status='pending'",
                ("approved" if approve else "rejected", now(), "Locally approved by interactive PowerShell review." if approve else "Locally rejected by interactive PowerShell review.", approval_id),
            )
            if cur.rowcount != 1:
                raise RuntimeError("Approval state changed during review; no approval was recorded.")
        return f"Approval {approval_id}: {'APPROVED' if approve else 'REJECTED'} by local interactive review."

    def execute_approved_powershell(self, approval_id: int) -> str:
        _require_enabled()
        with _db() as con:
            row = con.execute("SELECT * FROM dev_approvals WHERE approval_id=?", (approval_id,)).fetchone()
        if row is None or row["approval_type"] != "powershell_execution":
            raise ValueError("PowerShell approval not found.")
        if row["status"] != "approved":
            raise PermissionError("PowerShell execution requires a previously approved local interactive review.")
        script = _workspace_path(row["source_rel"], must_exist=True)
        if sha256_file(script) != row["file_sha256"]:
            raise RuntimeError("Script changed after review. Approval is invalid; create a new proposal.")
        self._check_powershell_policy(script)
        if _is_admin():
            raise PermissionError("Execution refused because the ChatGPT/MCP process has an elevated administrator token.")
        _require_enabled()
        powershell = shutil.which("powershell.exe")
        if not powershell:
            raise RuntimeError("Windows PowerShell (powershell.exe) was not found.")
        # Reserve the one-time approval before starting the child process. A failed,
        # interrupted, or timed-out attempt consumes this approval; it cannot be replayed.
        with _db() as con:
            cur = con.execute(
                "UPDATE dev_approvals SET status='executing',reviewed_at=?,result=? WHERE approval_id=? AND status='approved'",
                (now(), "One-time execution reserved; waiting for process result.", approval_id),
            )
            if cur.rowcount != 1:
                raise PermissionError("Approval is no longer in approved state; a concurrent action may have consumed or revoked it.")
        proc = None
        stdout = stderr = ""
        try:
            _require_enabled()
            proc = subprocess.Popen(
                [powershell, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script)],
                cwd=str(WORKSPACE), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            with _db() as con:
                con.execute("UPDATE dev_runtime SET active_powershell_pid=? WHERE singleton_id=1", (proc.pid,))
            deadline = time.monotonic() + 90
            while True:
                control = _get_control()
                if control["state"] != "enabled":
                    proc.kill()
                    stdout, stderr = proc.communicate()
                    raise RuntimeError(f"Approved PowerShell was interrupted by controller state={control['state']}.")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    proc.kill()
                    stdout, stderr = proc.communicate()
                    raise TimeoutError("PowerShell script exceeded 90 seconds and was terminated.")
                try:
                    stdout, stderr = proc.communicate(timeout=min(0.25, remaining))
                    break
                except subprocess.TimeoutExpired:
                    continue
            result = (stdout + ("\nSTDERR:\n" + stderr if stderr else "")).strip()[:10000]
            with _db() as con:
                con.execute(
                    "UPDATE dev_approvals SET status='executed',result=? WHERE approval_id=? AND status='executing'",
                    (result[:4000], approval_id),
                )
            return f"POWERSHELL EXECUTION FINISHED\nExit code: {proc.returncode}\n{result}"
        except Exception as exc:
            with _db() as con:
                con.execute(
                    "UPDATE dev_approvals SET status='failed',result=? WHERE approval_id=? AND status='executing'",
                    (f"Execution attempt failed: {type(exc).__name__}: {exc}"[:4000], approval_id),
                )
            raise
        finally:
            with _db() as con:
                con.execute("UPDATE dev_runtime SET active_powershell_pid=0 WHERE singleton_id=1")

    def apply_approved_promotion(self, approval_id: int) -> str:
        _require_enabled()
        if _is_admin():
            raise PermissionError("Core promotion is refused when the ChatGPT/MCP process has an elevated administrator token. Restart ChatGPT Desktop normally.")
        with _db() as con:
            row = con.execute("SELECT * FROM dev_approvals WHERE approval_id=?", (approval_id,)).fetchone()
        if row is None or row["approval_type"] != "core_promotion":
            raise ValueError("Core promotion approval not found.")
        if row["status"] != "approved":
            raise PermissionError("Core promotion requires local interactive user approval first.")
        source = _workspace_path(row["source_rel"], must_exist=True)
        destination = _promotion_path(row["destination_rel"])
        if sha256_file(source) != row["file_sha256"]:
            raise RuntimeError("Source artifact changed after review. Approval is invalid; create a new proposal.")
        current_hash = sha256_file(destination) if destination.is_file() else ""
        if current_hash != row["destination_sha256"]:
            raise RuntimeError("Core destination changed after review. No file was applied; create a new proposal.")
        if _has_reparse_component(destination.parent, ROOT):
            raise RuntimeError("Core destination parent contains a reparse point; promotion aborted.")
        # Consume the one-time authorization before staging the replacement.
        with _db() as con:
            cur = con.execute(
                "UPDATE dev_approvals SET status='applying',reviewed_at=?,result=? WHERE approval_id=? AND status='approved'",
                (now(), "Approved Core promotion reserved; preparing atomic replacement.", approval_id),
            )
            if cur.rowcount != 1:
                raise PermissionError("Approval is no longer in approved state; a concurrent action may have consumed or revoked it.")
        temp = None
        try:
            _require_enabled()
            destination.parent.mkdir(parents=True, exist_ok=True)
            if not _within(destination, ROOT) or _has_reparse_component(destination.parent, ROOT):
                raise RuntimeError("Core destination parent changed or contains a reparse point; promotion aborted.")
            # Recheck both hashes immediately before preparing the atomic replacement.
            if sha256_file(source) != row["file_sha256"]:
                raise RuntimeError("Promotion source changed during review; no file was applied.")
            latest_dest_hash = sha256_file(destination) if destination.is_file() else ""
            if latest_dest_hash != row["destination_sha256"]:
                raise RuntimeError("Core destination changed during review; no file was applied.")
            backup_path = "(new file; no previous version)"
            if destination.exists():
                if _has_reparse_component(BACKUP_ROOT, ROOT):
                    raise RuntimeError("Promotion backup directory contains a symlink/junction; no Core change was applied.")
                BACKUP_ROOT.mkdir(parents=True, exist_ok=True)
                if _has_reparse_component(BACKUP_ROOT, ROOT):
                    raise RuntimeError("Promotion backup directory changed to a reparse point; no Core change was applied.")
                backup_dir = BACKUP_ROOT / (datetime.now().strftime("%Y%m%d_%H%M%S") + f"_approval_{approval_id}_{uuid.uuid4().hex[:8]}")
                backup_dir.mkdir(parents=True, exist_ok=False)
                rel_dest = destination.relative_to(ROOT)
                backup = backup_dir.joinpath(*rel_dest.parts)
                backup.parent.mkdir(parents=True, exist_ok=False)
                shutil.copy2(destination, backup)
                backup_path = str(backup)
            temp = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.pending")
            _copy_exclusive(source, temp)
            # Hold a short SQLite write lock while checking the stop state and swapping
            # the file. Emergency Stop either wins before this check or waits for this
            # one atomic promotion to finish; it cannot silently half-apply a file.
            with _db() as con:
                con.execute("BEGIN IMMEDIATE")
                control = con.execute("SELECT state FROM dev_control WHERE singleton_id=1").fetchone()
                approval = con.execute("SELECT status FROM dev_approvals WHERE approval_id=?", (approval_id,)).fetchone()
                if not control or control["state"] != "enabled":
                    raise RuntimeError("Self-development control changed before Core promotion; no file was applied.")
                if not approval or approval["status"] != "applying":
                    raise RuntimeError("Core promotion approval was revoked before apply.")
                if sha256_file(source) != row["file_sha256"]:
                    raise RuntimeError("Promotion source changed before atomic replacement.")
                os.replace(temp, destination)
                applied_hash = sha256_file(destination)
                con.execute(
                    "UPDATE dev_approvals SET status='applied',reviewed_at=?,result=? WHERE approval_id=? AND status='applying'",
                    (now(), f"Applied SHA256={applied_hash}", approval_id),
                )
            return f"CORE PROMOTION APPLIED\nDestination: {destination}\nSHA256: {applied_hash}\nBackup: {backup_path}"
        except Exception as exc:
            if temp is not None:
                try:
                    temp.unlink(missing_ok=True)
                except OSError:
                    logging.exception("Could not clean promotion staging file %s", temp)
            with _db() as con:
                con.execute(
                    "UPDATE dev_approvals SET status='failed',result=? WHERE approval_id=? AND status='applying'",
                    (f"Promotion failed: {type(exc).__name__}: {exc}"[:4000], approval_id),
                )
            raise

    def attach_task_to_session(self, task_id: str, communication_session_id: str) -> str:
        """Attach an existing development task to an active Live Room session.

        This only changes the task's monitoring link. It does not start or resume
        the task, alter its status, or change any Core/communication messages.
        """
        if not isinstance(task_id, str) or not task_id.strip():
            raise ValueError("task_id is required.")
        if not isinstance(communication_session_id, str) or not communication_session_id.strip():
            raise ValueError("communication_session_id is required.")
        task_id = task_id.strip()
        communication_session_id = communication_session_id.strip()
        if len(task_id) > 80 or len(communication_session_id) > 80:
            raise ValueError("Task and communication session IDs must be 80 characters or fewer.")
        if not COMM_DB.is_file():
            raise FileNotFoundError("Communication database is not available.")

        # Verify the destination session using a read-only connection.
        comm_uri = COMM_DB.resolve().as_uri() + "?mode=ro"
        with sqlite3.connect(comm_uri, uri=True, timeout=5) as comm:
            comm.row_factory = sqlite3.Row
            tables = {r[0] for r in comm.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
            if not {"communication_sessions", "communication_events"}.issubset(tables):
                raise RuntimeError("Communication database is missing its session/event tables.")
            session = comm.execute(
                "SELECT session_id,status FROM communication_sessions WHERE session_id=?",
                (communication_session_id,),
            ).fetchone()
            if session is None:
                raise ValueError("Communication session not found.")
            if session["status"] != "active":
                raise RuntimeError(f"Communication session must be active; current status={session['status']}.")

        with _db() as con:
            task = con.execute(
                "SELECT task_id,status,communication_session_id FROM dev_tasks WHERE task_id=?",
                (task_id,),
            ).fetchone()
            if task is None:
                raise ValueError("Development task not found.")
            current_link = (task["communication_session_id"] or "").strip()
            if current_link and current_link != communication_session_id:
                raise RuntimeError(
                    "Task is already linked to a different communication session; automatic reassignment is refused."
                )
            if current_link == communication_session_id:
                return (
                    "TASK ALREADY LINKED\n"
                    f"Task ID: {task_id}\nSession ID: {communication_session_id}\n"
                    f"Task status: {task['status']}\n"
                    "No task execution or status change was performed."
                )
            con.execute(
                "UPDATE dev_tasks SET communication_session_id=?,updated_at=? WHERE task_id=?",
                (communication_session_id, now(), task_id),
            )

        _event(
            task_id,
            "task_linked_to_session",
            f"Development task linked to Live Room session {communication_session_id}.",
            "Monitoring link created; task status and execution state were not changed.",
        )
        return (
            "TASK LINKED TO LIVE ROOM\n"
            f"Task ID: {task_id}\n"
            f"Session ID: {communication_session_id}\n"
            f"Task status: {task['status']}\n"
            "Development events from this point onward will be mirrored to the session.\n"
            "No task execution or status change was performed."
        )

    def add_mentor_feedback(self, task_id: str, feedback: str) -> str:
        _require_enabled()
        if not isinstance(task_id, str) or not task_id.strip():
            raise ValueError("task_id is required.")
        if not isinstance(feedback, str) or not feedback.strip() or len(feedback) > 6000:
            raise ValueError("Mentor feedback must be 1â€“6,000 characters.")
        with _db() as con:
            row = con.execute(
                "SELECT status,agent_messages_json FROM dev_tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            if row is None:
                raise ValueError("Development task not found.")
            if row["status"] not in {"queued", "needs_mentor_review", "blocked"}:
                raise RuntimeError(f"Mentor feedback cannot be added while task status={row['status']}.")
            messages = json.loads(row["agent_messages_json"] or "[]")
            messages.append({
                "role": "user",
                "content": "MENTOR FEEDBACK / NEXT INSTRUCTIONS:\n" + feedback.strip(),
            })
            con.execute(
                "UPDATE dev_tasks SET agent_messages_json=?,status='queued',mentor_guidance=?,updated_at=? WHERE task_id=?",
                (json.dumps(messages[-40:], ensure_ascii=False), feedback.strip()[:4000], now(), task_id),
            )
        _event(task_id, "mentor_feedback", "ChatGPT mentor provided feedback and queued the next development step.", feedback[:3000])
        return f"MENTOR FEEDBACK RECORDED\nTask ID: {task_id}\nStatus: queued\nThe local agent can continue when ChatGPT calls claud_dev_run_task. No task was started automatically."

    def list_tasks(self, status: str = "all", limit: int = 20) -> str:
        if status not in {"all", "queued", "running", "paused", "stopped", "blocked", "needs_mentor_review", "completed"}:
            raise ValueError("Invalid task status.")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 50:
            raise ValueError("limit must be from 1 to 50.")
        with _db() as con:
            if status == "all":
                rows = con.execute("SELECT * FROM dev_tasks ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
            else:
                rows = con.execute("SELECT * FROM dev_tasks WHERE status=? ORDER BY created_at DESC LIMIT ?", (status, limit)).fetchall()
        if not rows:
            return "No development tasks found."
        return "\n\n---\n\n".join(
            f"Task: {r['task_id']}\nTitle: {r['title']}\nStatus: {r['status']}\nCommunication session: {r['communication_session_id'] or 'not linked'}\nActions: {r['action_count']}/{MAX_TASK_ACTIONS}\nUpdated: {r['updated_at']}\nResult: {r['last_result'][:2000]}"
            for r in rows
        )

    def task_report(self, task_id: str) -> str:
        with _db() as con:
            task = con.execute("SELECT * FROM dev_tasks WHERE task_id=?", (task_id,)).fetchone()
            actions = con.execute("SELECT * FROM dev_actions WHERE task_id=? ORDER BY action_id ASC", (task_id,)).fetchall()
        if task is None:
            raise ValueError("Task not found.")
        return (
            f"TASK: {task['title']}\nID: {task_id}\nSTATUS: {task['status']}\n"
            f"COMMUNICATION SESSION: {task['communication_session_id'] or 'not linked'}\n"
            f"OBJECTIVE: {task['objective']}\nACCEPTANCE: {task['acceptance_criteria']}\n"
            f"ACTIONS: {task['action_count']}/{MAX_TASK_ACTIONS}\nLAST RESULT:\n{task['last_result']}\n\n"
            "ACTION LOG:\n" + "\n".join(
                f"[{a['created_at']}] {a['action_type']}: {a['detail']}\n{a['result'][:1500]}" for a in actions
            )
        )

    def complete_task(self, task_id: str, mentor_review: str) -> str:
        _require_enabled()
        if not isinstance(mentor_review, str) or len(mentor_review.strip()) < 20:
            raise ValueError("A meaningful mentor review (at least 20 characters) is required.")
        with _db() as con:
            cur = con.execute(
                "UPDATE dev_tasks SET status='completed',last_result=last_result || ?,updated_at=? WHERE task_id=? AND status='needs_mentor_review'",
                ("\n\nMENTOR REVIEW:\n" + mentor_review.strip(), now(), task_id),
            )
            if cur.rowcount == 0:
                return "Task not completed: it must first be in needs_mentor_review status."
        _event(task_id, "task_completed", "Mentor marked task complete after reviewing the result.", mentor_review)
        return f"Task {task_id} marked completed after mentor review."

    def approvals_cli(self, approval_id: int) -> str:
        data = self.approval_details(approval_id)
        if data["status"] != "pending":
            raise RuntimeError(f"Approval is not pending (status={data['status']}).")
        if data["approval_type"] == "powershell_execution":
            current = _workspace_path(data["source_rel"], must_exist=True)
            if sha256_file(current) != data["file_sha256"]:
                raise RuntimeError("PowerShell script changed after proposal. No approval was recorded; create a new proposal.")
        elif data["approval_type"] == "core_promotion":
            current_source = _workspace_path(data["source_rel"], must_exist=True)
            current_destination = _promotion_path(data["destination_rel"])
            if sha256_file(current_source) != data["file_sha256"]:
                raise RuntimeError("Promotion source changed after proposal. No approval was recorded; create a new proposal.")
            current_dest_hash = sha256_file(current_destination) if current_destination.is_file() else ""
            if current_dest_hash != data["destination_sha256"]:
                raise RuntimeError("Core destination changed after proposal. No approval was recorded; create a new proposal.")
        print("\n=== CLAUD DEVELOPMENT APPROVAL REVIEW ===")
        for key in ("approval_id", "approval_type", "status", "source_rel", "destination_rel", "purpose", "file_sha256", "destination_sha256"):
            print(f"{key}: {data.get(key, '')}")
        if data["approval_type"] == "powershell_execution":
            print("\n--- EXACT POWERSHELL SCRIPT (review this content) ---\n")
            print(data.get("source_content", ""))
            print("\n--- END SCRIPT ---")
            print("Execution policy: current user token only; elevated/admin token is rejected.")
        else:
            print("\n--- UNIFIED DIFF (workspace source -> Core destination) ---\n")
            print(data.get("diff", "(new file)"))
            print("\n--- END DIFF ---")
            print("The destination will be backed up before replacement.")
        print("\nThis approval applies only to the exact SHA256 shown above.")
        answer = input(f"Type exactly 'APPROVE {approval_id}' to approve, or 'REJECT {approval_id}' to reject: ").strip()
        if answer not in {f"APPROVE {approval_id}", f"REJECT {approval_id}"}:
            print("No approval recorded. No file was changed and no script was executed.")
            return "cancelled"
        return self.mark_approval(approval_id, answer.startswith("APPROVE"), answer)

    def emergency_stop(self, reason: str = "Emergency stop requested by user") -> str:
        result = set_control("stopped", reason)
        with _db() as con:
            con.execute("UPDATE dev_tasks SET status='stopped',updated_at=? WHERE status IN ('queued','running','paused','needs_mentor_review','blocked')", (now(),))
            # A stop invalidates both pending and previously approved-but-not-executed
            # operations. The user must create a fresh proposal after resuming.
            con.execute(
                "UPDATE dev_approvals SET status='rejected',reviewed_at=?,result=? WHERE status IN ('pending','approved','executing','applying')",
                (now(), "Invalidated by Emergency Stop; create a new proposal after resuming."),
            )
        return result + " New agent actions are blocked; all unexecuted approvals were invalidated. An in-flight Ollama generation may finish, but its next tool actions will be blocked."

    def pause(self, reason: str = "Paused by user") -> str:
        result = set_control("paused", reason)
        with _db() as con:
            con.execute("UPDATE dev_tasks SET status='paused',updated_at=? WHERE status IN ('queued','running')", (now(),))
        return result

    def resume(self) -> str:
        result = set_control("enabled", "Resumed by explicit user request")
        with _db() as con:
            con.execute("UPDATE dev_tasks SET status='queued',updated_at=? WHERE status='paused'", (now(),))
        return result + " Tasks remain queued; no task starts automatically."

    def run_approved_powershell(self, approval_id: int) -> str:
        return self.execute_approved_powershell(approval_id)


# Initialize once when imported by MCP or the local control scripts.
initialize()
