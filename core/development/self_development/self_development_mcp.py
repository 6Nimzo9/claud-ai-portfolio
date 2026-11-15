from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any

from mcp.server import MCPServer
from self_development_engine import SelfDevelopmentEngine

ROOT = Path(__file__).resolve().parents[3]
LOG_PATH = ROOT / "logs" / "self-development-mcp.log"
LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    filename=str(LOG_PATH), level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s", encoding="utf-8",
)

INSTRUCTIONS = """You are ChatGPT operating as the authorized mentor for Claud AI's local self-development agent.

MENTOR WORKFLOW:
1. Start with claud_dev_status and claud_dev_probe_tool_calling. Do not assume local tool calling works until the probe confirms it.
2. Diagnose one bounded development objective at a time. Use claud_dev_start_task with explicit acceptance criteria and, when a mentor discussion exists, pass its communication_session_id so activity is visible in Claud Live Room.
   For an existing task without a communication_session_id, use claud_dev_attach_task_to_session with an active Live Room session before adding feedback or running it. Verify the link with claud_dev_task_report.
3. Call claud_dev_run_task in bounded increments. Inspect the task report and generated workspace artifacts. Give Claud corrective feedback using claud_dev_add_mentor_feedback, then continue the same task when useful.
4. Local agent writes only inside D:\\ClaudAI\\workspace\\self_development. It may read only allowlisted Core/test source files.
5. Python validation uses py_compile only; it does not execute generated Python. PowerShell syntax checks parse only; they do not execute a script.
6. Strictly allowlisted, non-destructive PowerShell may run automatically only inside the self-development workspace as the current non-admin user. The safe policy blocks overwrites of existing files, deletion, moving/copying, variables, loops, dynamic code, external commands, network, registry, credentials, startup, security-policy, administrator operations, wildcards in write paths, and reparse-point paths.
7. Workspace-only deletion/move/copy/overwrite scripts and promotion into active Core are gated by a local interactive review and an additional static policy. The user must run Review-ClaudDevelopmentApproval.ps1 and approve the exact content. Never call claud_dev_execute_approved_powershell or claud_dev_apply_approved_promotion before the user has completed that local approval and explicitly asked to proceed. Scripts invoking external programs or dynamic evaluation are forbidden even with approval.
8. All PowerShell execution is refused if the MCP process is elevated. Approved scripts are revalidated at execution time and run only as the current non-admin user from the self-development workspace.
9. Never approve a change or lesson on the user's behalf. ChatGPT is the mentor, not the owner of the machine.
10. If the user asks to pause or stop learning/development, call claud_dev_pause or claud_dev_emergency_stop immediately. Do not resume until the user explicitly asks.
11. Do not claim code was promoted, PowerShell was executed, or tests passed unless the corresponding tool confirms it.

The local model remains Llama 3.2. This mechanism creates staged code, procedures, and verified experience; it does not retrain model weights. Existing Claud Core 0.6.0 and the original claud_ai MCP server are kept separate and are not modified by this server."""

mcp = MCPServer("Claud AI Autonomous Development", instructions=INSTRUCTIONS)
_ENGINE: SelfDevelopmentEngine | None = None


def engine() -> SelfDevelopmentEngine:
    global _ENGINE
    if _ENGINE is None:
        _ENGINE = SelfDevelopmentEngine()
    return _ENGINE


def _call(fn, *args, **kwargs) -> str:
    try:
        return str(fn(*args, **kwargs))
    except Exception as exc:
        logging.exception("Self-development MCP tool failed: %s", getattr(fn, "__name__", "tool"))
        return f"ERROR: {type(exc).__name__}: {exc}"


@mcp.tool()
def claud_dev_status() -> str:
    """Show self-development control state, local model, workspace, tool-calling status, tasks and pending approvals."""
    try:
        return json.dumps(engine().status(), ensure_ascii=False, indent=2)
    except Exception as exc:
        logging.exception("Self-development status failed")
        return f"ERROR: {type(exc).__name__}: {exc}"


@mcp.tool()
def claud_dev_probe_tool_calling() -> str:
    """Safely test whether the configured local Ollama model can call a no-op tool. No files are changed."""
    return _call(engine().probe_tool_calling)


@mcp.tool()
def claud_dev_start_task(
    title: str,
    objective: str,
    acceptance_criteria: str,
    mentor_guidance: str = "",
    communication_session_id: str = "",
) -> str:
    """Create a bounded mentored self-development task. No agent run starts until claud_dev_run_task is called."""
    return _call(
        engine().start_task, title, objective, acceptance_criteria,
        mentor_guidance, communication_session_id,
    )


@mcp.tool()
def claud_dev_attach_task_to_session(task_id: str, communication_session_id: str) -> str:
    """Attach an existing task to an active communication session for Live Room monitoring. This does not run or requeue the task."""
    return _call(engine().attach_task_to_session, task_id, communication_session_id)


@mcp.tool()
def claud_dev_add_mentor_feedback(task_id: str, feedback: str) -> str:
    """Append ChatGPT mentor teaching/correction to a queued or review-needed task. It does not start the agent."""
    return _call(engine().add_mentor_feedback, task_id, feedback)


@mcp.tool()
def claud_dev_run_task(task_id: str, max_model_turns: int = 3) -> str:
    """Run a bounded local Claud agent cycle using only workspace/source/compile/syntax tools. Core files are never modified by this tool."""
    return _call(engine().run_task, task_id, max_model_turns)


@mcp.tool()
def claud_dev_list_tasks(status: str = "all", limit: int = 20) -> str:
    """List recent development tasks and their statuses."""
    return _call(engine().list_tasks, status, limit)


@mcp.tool()
def claud_dev_task_report(task_id: str) -> str:
    """Retrieve a development task's objective, status, results and action log."""
    return _call(engine().task_report, task_id)


@mcp.tool()
def claud_dev_list_workspace_files(relative_dir: str = "") -> str:
    """List files inside D:\\ClaudAI\\workspace\\self_development only."""
    return _call(engine().list_workspace_files, relative_dir)


@mcp.tool()
def claud_dev_read_workspace_file(relative_path: str) -> str:
    """Read a text/code artifact from the self-development workspace."""
    return _call(engine().read_workspace_file, relative_path)


@mcp.tool()
def claud_dev_read_project_source(relative_path: str, start_line: int = 1, max_lines: int = 160) -> str:
    """Read one bounded line window from an allowlisted Core or development test source file without modifying it."""
    return _call(engine().read_project_source, relative_path, start_line, max_lines)


@mcp.tool()
def claud_dev_write_workspace_file(relative_path: str, content: str) -> str:
    """Create or update one text/code artifact inside the isolated self-development workspace."""
    return _call(engine().write_workspace_file, relative_path, content)


@mcp.tool()
def claud_dev_run_python_compile(relative_paths: list[str]) -> str:
    """Run py_compile on workspace Python files only. This validates syntax and does not execute the code."""
    return _call(engine().run_python_compile, relative_paths)


@mcp.tool()
def claud_dev_validate_powershell_syntax(relative_path: str) -> str:
    """Parse a workspace PowerShell script for syntax errors without executing it."""
    return _call(engine().validate_powershell_syntax, relative_path)


@mcp.tool()
def claud_dev_run_safe_powershell(task_id: str, relative_path: str) -> str:
    """Run only the strict non-destructive PowerShell subset in the isolated workspace, non-admin. No approval is needed for this narrow safe subset."""
    return _call(engine().run_safe_powershell, task_id, relative_path)


@mcp.tool()
def claud_dev_propose_powershell_execution(task_id: str, relative_path: str, purpose: str) -> str:
    """Stage a one-time PowerShell execution approval. The script is not executed; the user must review it locally first."""
    return _call(engine().propose_powershell_execution, task_id, relative_path, purpose)


@mcp.tool()
def claud_dev_execute_approved_powershell(approval_id: int) -> str:
    """Execute a previously locally approved PowerShell script once, non-admin, under the self-development workspace and safety allowlist."""
    return _call(engine().execute_approved_powershell, approval_id)


@mcp.tool()
def claud_dev_propose_core_promotion(
    task_id: str,
    source_relative_path: str,
    destination_relative_path: str,
    purpose: str,
) -> str:
    """Create a review request for promoting one workspace file into an allowlisted Core/test source path. Does not apply the change."""
    return _call(
        engine().propose_core_promotion, task_id, source_relative_path,
        destination_relative_path, purpose,
    )


@mcp.tool()
def claud_dev_apply_approved_promotion(approval_id: int) -> str:
    """Apply a Core promotion only after the user has approved the exact diff locally with Review-ClaudDevelopmentApproval.ps1."""
    return _call(engine().apply_approved_promotion, approval_id)


@mcp.tool()
def claud_dev_list_approvals(status: str = "pending", limit: int = 20) -> str:
    """List pending, approved, applied, executed, rejected, or all risk approvals."""
    return _call(engine().list_approvals, status, limit)


@mcp.tool()
def claud_dev_pause(reason: str = "Paused by user") -> str:
    """Pause all new self-development steps and terminate any tracked PowerShell child process."""
    return _call(engine().pause, reason)


@mcp.tool()
def claud_dev_resume() -> str:
    """Resume the development controller after an explicit user request. No task is started automatically."""
    return _call(engine().resume)


@mcp.tool()
def claud_dev_emergency_stop(reason: str = "Emergency stop requested by user") -> str:
    """Immediately stop the self-development controller, block new actions, and terminate a tracked workspace PowerShell child process."""
    return _call(engine().emergency_stop, reason)


@mcp.tool()
def claud_dev_complete_task(task_id: str, mentor_review: str) -> str:
    """Mark a task completed only after ChatGPT mentor has reviewed its report and validation results."""
    return _call(engine().complete_task, task_id, mentor_review)


if __name__ == "__main__":
    mcp.run(transport="stdio")
