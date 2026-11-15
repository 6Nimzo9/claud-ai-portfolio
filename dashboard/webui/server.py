# Claud AI Web Interface - local adapter for the existing production core.
# Uses only Python standard library plus the requests dependency already used by core.
import importlib.util
import json
import mimetypes
import threading
import traceback
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

ROOT = Path(__file__).resolve().parents[2]
CORE_FILE = ROOT / "core" / "main.py"
WEB_DIR = ROOT / "dashboard" / "webui" / "web"
HOST, PORT = "127.0.0.1", 8765
LOCK = threading.RLock()

if not CORE_FILE.exists():
    raise RuntimeError(f"Claud AI core not found: {CORE_FILE}")

spec = importlib.util.spec_from_file_location("claud_core_main", CORE_FILE)
core = importlib.util.module_from_spec(spec)
spec.loader.exec_module(core)
core.initialize_db()
core.initialize_knowledge()

# DEVELOPMENT_ENGINE_INTEGRATION_V1
# Keep Development Engine persistence separate from Claud AI memory.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.development import (
    DevelopmentOrchestrator,
    DevelopmentRepository,
    TaskStatus,
)
from core.goal_fulfillment import GoalFulfillmentEngine, GoalRepository, ToolRegistry

DEVELOPMENT_REPOSITORY = DevelopmentRepository()
DEVELOPMENT_ENGINE = DevelopmentOrchestrator(
    repository=DEVELOPMENT_REPOSITORY
)

# UGFE API uses the existing memory database and only a workspace-scoped,
# read-only built-in tool. No research provider is configured in this runtime.
UGFE_ENGINE = GoalFulfillmentEngine(
    repository=GoalRepository(ROOT / "database" / "claud_memory.db"),
    tool_registry=ToolRegistry.with_workspace_read(ROOT),
    workspace_root=ROOT,
)

def json_bytes(data):
    return json.dumps(data, ensure_ascii=False).encode("utf-8")

def goal_api_response(goal_id):
    report = UGFE_ENGINE.report(goal_id)
    challenge = UGFE_ENGINE.take_approval_challenge(goal_id)
    if challenge is not None:
        report["approval_challenge"] = challenge
    return report

def get_conversations():
    with core.connect_db() as db:
        rows = db.execute("""
            SELECT c.id, c.title, c.created_at, COUNT(m.id)
            FROM conversations c LEFT JOIN messages m ON m.conversation_id=c.id
            GROUP BY c.id ORDER BY c.id DESC LIMIT 100
        """).fetchall()
    return [{"id":r[0], "title":r[1] or "Conversation", "created_at":r[2], "messages":r[3]} for r in rows]

def get_messages(cid):
    with core.connect_db() as db:
        rows = db.execute("""
            SELECT id, role, content, created_at FROM messages
            WHERE conversation_id=? ORDER BY id ASC
        """, (cid,)).fetchall()
    return [{"id":r[0], "role":r[1], "content":r[2], "created_at":r[3]} for r in rows]

def new_conversation():
    return core.create_conversation()

def send_message(cid, text):
    text = text.strip()
    if not text:
        raise ValueError("Pesan kosong.")
    with LOCK:
        with core.connect_db() as db:
            exists = db.execute("SELECT id FROM conversations WHERE id=?", (cid,)).fetchone()
        if not exists:
            raise ValueError("Conversation tidak ditemukan.")
        # Match the CLI behavior: history is read before current message is saved.
        route = core.classify_query(text)
        if route == "evaluation":
            answer = core.format_evaluation_report()
            status, duration, draft = "deterministic_evaluation", 0.0, answer
        elif route == "database":
            answer = core.database_only_answer(text)
            if answer is None:
                answer, status, duration, draft = core.generate_answer(core.load_settings(), cid, text)
            else:
                status, duration, draft = "deterministic_database", 0.0, answer
        elif route == "configuration":
            answer = core.deterministic_configuration_answer()
            status, duration, draft = "deterministic_configuration", 0.0, answer
        else:
            answer, status, duration, draft = core.generate_answer(core.load_settings(), cid, text)
        core.save_message(cid, "user", text)
        core.save_message(cid, "assistant", answer)
        core.log_evaluation(cid, text, draft, answer, status, duration)
        return {"answer":answer, "status":status, "duration_seconds":duration, "route":route}

class Handler(BaseHTTPRequestHandler):
    server_version = "ClaudAIWeb/1.0"
    def log_message(self, fmt, *args):
        # Keep the local UI quiet; errors are returned to the browser.
        pass
    def send_json(self, code, data):
        body = json_bytes(data)
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)
    def body_json(self):
        n = int(self.headers.get("Content-Length", "0"))
        if n > 200000:
            raise ValueError("Request terlalu besar.")
        return json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
    def do_GET(self):
        u = urlparse(self.path)
        try:
            if u.path == "/api/health":
                settings = core.load_settings()
                try:
                    import requests
                    r = requests.get(settings.get("ollama_url","http://127.0.0.1:11434").rstrip("/") + "/api/tags", timeout=3)
                    ollama = r.ok
                except Exception:
                    ollama = False
                self.send_json(200, {"ok":True, "core":"connected", "ollama":ollama,
                    "model":settings.get("local_model", settings.get("model","llama3.2")),
                    "version":settings.get("version","unknown"),
                    "reasoning":settings.get("reasoning_enabled",True),
                    "critique":settings.get("critique_enabled",True),
                    "grounding":settings.get("grounding_enabled",True)})
            elif u.path == "/api/conversations":
                self.send_json(200, {"items":get_conversations()})
            elif u.path.startswith("/api/conversations/") and u.path.endswith("/messages"):
                cid = int(u.path.split("/")[3])
                self.send_json(200, {"items":get_messages(cid)})
            elif u.path.startswith("/api/conversations/") and u.path.endswith("/regenerate"):
                self.send_error(405)
            elif u.path.startswith("/api/conversations/"):
                self.send_error(405)
            elif u.path == "/api/memories":
                self.send_json(200, {"items":[{"id":r[0],"content":r[1]} for r in core.list_memories()]})
            elif u.path == "/api/knowledge":
                rows = core.list_knowledge()
                self.send_json(200, {"items":[{"id":r[0],"content":r[1],"category":r[2],"status":r[3]} for r in rows]})
            elif u.path == "/api/sources":
                self.send_json(200, core.get_sources_report())

            elif u.path == "/api/development/tasks":
                with LOCK:
                    items = DEVELOPMENT_ENGINE.list_tasks()
                    execution_enabled = (
                        DEVELOPMENT_ENGINE.policy.execution_enabled
                    )
                self.send_json(200, {
                    "items": items,
                    "execution_enabled": execution_enabled
                })

            elif u.path.startswith("/api/development/tasks/"):
                parts = [part for part in u.path.split("/") if part]
                if len(parts) != 4:
                    self.send_error(404)
                    return

                task_id = parts[3]
                with LOCK:
                    report = DEVELOPMENT_ENGINE.report(task_id)

                self.send_json(200, report)

            elif u.path == "/api/goals":
                self.send_json(200, {"items": [goal.to_dict() for goal in UGFE_ENGINE.repository.list_goals()]})

            elif u.path.startswith("/api/goals/"):
                parts = [part for part in u.path.split("/") if part]
                if len(parts) != 3:
                    self.send_error(404)
                    return
                self.send_json(200, UGFE_ENGINE.report(parts[2]))

            else:
                path = WEB_DIR / ("index.html" if u.path == "/" else u.path.lstrip("/"))
                if not path.resolve().is_relative_to(WEB_DIR.resolve()) or not path.is_file():
                    self.send_error(404); return
                data = path.read_bytes()
                ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
                self.send_response(200)
                self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith(("text/","application/javascript")) else ""))
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        except Exception as e:
            self.send_json(500, {"error":str(e)})
    def do_PATCH(self):
        try:
            data = self.body_json()
            parts = [part for part in urlparse(self.path).path.split("/") if part]
            if len(parts) != 3 or parts[0] != "api" or parts[1] != "conversations":
                self.send_error(404); return
            cid = int(parts[2])
            title = str(data.get("title", "")).strip()
            if not title or len(title) > 80:
                raise ValueError("Judul harus berisi 1â€“80 karakter.")
            with core.connect_db() as db:
                cur = db.execute("UPDATE conversations SET title=? WHERE id=?", (title, cid))
                if cur.rowcount == 0:
                    raise ValueError("Conversation tidak ditemukan.")
                db.commit()
            self.send_json(200, {"ok": True, "id": cid, "title": title})
        except Exception as e:
            self.send_json(400, {"error": str(e)})

    def do_DELETE(self):
        try:
            parts = [part for part in urlparse(self.path).path.split("/") if part]
            if len(parts) != 3 or parts[0] != "api" or parts[1] != "conversations":
                self.send_error(404); return
            cid = int(parts[2])
            with LOCK, core.connect_db() as db:
                exists = db.execute("SELECT id FROM conversations WHERE id=?", (cid,)).fetchone()
                if not exists:
                    raise ValueError("Conversation tidak ditemukan.")
                # Explicitly delete child messages first; memory and knowledge tables are untouched.
                db.execute("DELETE FROM messages WHERE conversation_id=?", (cid,))
                db.execute("DELETE FROM conversations WHERE id=?", (cid,))
                db.commit()
            self.send_json(200, {"ok": True, "deleted_id": cid})
        except Exception as e:
            self.send_json(400, {"error": str(e)})

    def do_POST(self):
        try:
            data = self.body_json()
            if self.path == "/api/conversations":
                cid = new_conversation()
                self.send_json(200, {"id":cid})
            elif urlparse(self.path).path.startswith("/api/conversations/") and urlparse(self.path).path.endswith("/regenerate"):
                parts = [part for part in urlparse(self.path).path.split("/") if part]
                if len(parts) != 4:
                    self.send_error(404); return
                cid = int(parts[2])
                with LOCK:
                    with core.connect_db() as db:
                        last = db.execute("SELECT id, role, content FROM messages WHERE conversation_id=? ORDER BY id DESC LIMIT 1", (cid,)).fetchone()
                        if not last or last[1] != "assistant":
                            raise ValueError("Tidak ada jawaban terakhir yang dapat dibuat ulang.")
                        previous_user = db.execute("SELECT id, content FROM messages WHERE conversation_id=? AND role='user' ORDER BY id DESC LIMIT 1", (cid,)).fetchone()
                        if not previous_user:
                            raise ValueError("Pesan pengguna tidak ditemukan.")
                        prompt = previous_user[1]
                        previous_answer = last[2]
                        db.execute("DELETE FROM messages WHERE id IN (?, ?)", (last[0], previous_user[0]))
                        db.commit()
                    try:
                        # Match normal chat flow: generate against prior history, then persist this turn.
                        answer, status, duration, draft = core.generate_answer(core.load_settings(), cid, prompt)
                        core.save_message(cid, "user", prompt)
                        core.save_message(cid, "assistant", answer)
                        core.log_evaluation(cid, prompt, draft, answer, status, duration)
                    except Exception:
                        core.save_message(cid, "user", prompt)
                        core.save_message(cid, "assistant", previous_answer)
                        raise
                self.send_json(200, {"answer": answer, "status": status, "duration_seconds": duration})
            elif self.path == "/api/chat":
                result = send_message(int(data["conversation_id"]), str(data["message"]))
                self.send_json(200, result)
            elif self.path == "/api/memories":
                content = str(data.get("content","")).strip()
                if not content: raise ValueError("Isi memory kosong.")
                ok = core.save_memory(content)
                self.send_json(200, {"saved":ok})
            elif self.path == "/api/settings/toggle":
                key = data.get("key")
                allowed = {"reasoning_enabled","critique_enabled","grounding_enabled"}
                if key not in allowed: raise ValueError("Setting tidak diizinkan.")
                settings = core.load_settings()
                settings[key] = bool(data.get("value"))
                core.save_settings(settings)
                self.send_json(200, {"ok":True, "key":key, "value":settings[key]})

            elif urlparse(self.path).path == "/api/development/tasks":
                title = data.get("title", "")
                objective = data.get("objective", "")

                with LOCK:
                    task, plan = DEVELOPMENT_ENGINE.create_task(
                        title, objective
                    )
                    report = DEVELOPMENT_ENGINE.report(task.task_id)

                self.send_json(201, report)

            elif urlparse(self.path).path.startswith(
                "/api/development/tasks/"
            ):
                parts = [
                    part for part in urlparse(self.path).path.split("/")
                    if part
                ]

                if len(parts) != 5:
                    self.send_error(404)
                    return

                task_id = parts[3]
                action = parts[4]

                with LOCK:
                    if action == "approve":
                        DEVELOPMENT_ENGINE.approve_task(task_id)

                    elif action == "reject":
                        task = DEVELOPMENT_ENGINE.get_task(task_id)

                        if task.status != TaskStatus.AWAITING_APPROVAL:
                            raise ValueError(
                                "Only tasks awaiting approval can be rejected."
                            )

                        DEVELOPMENT_ENGINE.reject_task(task_id)

                    else:
                        self.send_error(404)
                        return

                    report = DEVELOPMENT_ENGINE.report(task_id)

                self.send_json(200, report)

            elif urlparse(self.path).path == "/api/goals":
                goal = UGFE_ENGINE.create_goal(
                    user_goal=data.get("user_goal", ""),
                    clarified_intent=data.get("clarified_intent"),
                    acceptance_criteria=data.get("acceptance_criteria", []),
                    required_capabilities=data.get("required_capabilities", []),
                    research_topic=data.get("research_topic"),
                    steps=data.get("steps", []),
                )
                with LOCK:
                    goal = UGFE_ENGINE.run(goal.goal_id)
                    report = goal_api_response(goal.goal_id)
                self.send_json(201, report)

            elif urlparse(self.path).path.startswith("/api/goals/"):
                parts = [part for part in urlparse(self.path).path.split("/") if part]
                if len(parts) != 4 or parts[3] != "approve":
                    self.send_error(404)
                    return
                with LOCK:
                    UGFE_ENGINE.approve(parts[2], str(data.get("approval_token", "")))
                    report = goal_api_response(parts[2])
                self.send_json(200, report)

            else:
                self.send_error(404)
        except Exception as e:
            self.send_json(400, {"error":str(e), "trace":traceback.format_exc(limit=2)})

def main():
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    httpd.daemon_threads = True
    print(f"Claud AI Web UI listening on http://{HOST}:{PORT}")
    httpd.serve_forever()

if __name__ == "__main__":
    main()
