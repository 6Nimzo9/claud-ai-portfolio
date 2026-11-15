from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(os.environ.get("CLAUD_ROOT", Path(__file__).resolve().parents[3])).resolve()
DB_PATH = ROOT / "database" / "communication.db"
WEB_DIR = Path(__file__).resolve().parent / "web"
HOST = "127.0.0.1"
PORT = int(os.environ.get("CLAUD_LIVE_ROOM_PORT", "8766"))
MAX_LIMIT = 100


def db_readonly() -> sqlite3.Connection:
    if not DB_PATH.is_file():
        raise FileNotFoundError(f"Communication database not found: {DB_PATH}")
    uri = DB_PATH.resolve().as_uri() + "?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    con.execute("PRAGMA busy_timeout=5000")
    return con


def rows_to_dict(rows):
    return [dict(row) for row in rows]


class Handler(BaseHTTPRequestHandler):
    server_version = "ClaudLiveRoom/1.0"

    def log_message(self, fmt, *args):
        # Keep routine polling out of the console; errors still go to stderr.
        if args and str(args[1] if len(args) > 1 else "").startswith("5"):
            super().log_message(fmt, *args)

    def send_bytes(self, body: bytes, content_type: str, status: int = 200):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; connect-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, data, status: int = 200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_bytes(body, "application/json; charset=utf-8", status)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/health":
            try:
                with closing(db_readonly()) as con:
                    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                    required = {"communication_sessions", "communication_messages"}
                    missing = sorted(required - tables)
                    events_ready = "communication_events" in tables
                self.send_json({"status": "ready" if not missing else "degraded", "database": str(DB_PATH), "events_table": events_ready, "missing_tables": missing})
            except Exception as exc:
                self.send_json({"status": "error", "error": f"{type(exc).__name__}: {exc}"}, 503)
            return

        if parsed.path == "/api/sessions":
            try:
                query = parse_qs(parsed.query)
                limit = min(MAX_LIMIT, max(1, int(query.get("limit", [30])[0])))
                with closing(db_readonly()) as con:
                    sessions = con.execute(
                        """SELECT s.session_id, s.topic, s.status, s.max_turns, s.turn_count,
                                  s.created_at, s.updated_at,
                                  (SELECT m.sender FROM communication_messages m WHERE m.session_id=s.session_id ORDER BY m.message_id DESC LIMIT 1) AS last_sender,
                                  (SELECT substr(m.content,1,180) FROM communication_messages m WHERE m.session_id=s.session_id ORDER BY m.message_id DESC LIMIT 1) AS last_content,
                                  (SELECT m.created_at FROM communication_messages m WHERE m.session_id=s.session_id ORDER BY m.message_id DESC LIMIT 1) AS last_message_at
                           FROM communication_sessions s
                           ORDER BY s.updated_at DESC LIMIT ?""",
                        (limit,),
                    ).fetchall()
                self.send_json({"sessions": rows_to_dict(sessions)})
            except Exception as exc:
                self.send_json({"error": f"{type(exc).__name__}: {exc}"}, 500)
            return

        if parsed.path == "/api/session":
            query = parse_qs(parsed.query)
            session_id = query.get("id", [""])[0].strip()
            if not session_id or len(session_id) > 80:
                self.send_json({"error": "Valid session id is required."}, 400)
                return
            try:
                with closing(db_readonly()) as con:
                    session = con.execute(
                        "SELECT session_id, topic, status, max_turns, turn_count, created_at, updated_at FROM communication_sessions WHERE session_id=?",
                        (session_id,),
                    ).fetchone()
                    if session is None:
                        self.send_json({"error": "Session not found."}, 404)
                        return
                    messages = con.execute(
                        "SELECT message_id, sender, content, created_at FROM communication_messages WHERE session_id=? ORDER BY message_id ASC LIMIT 1000",
                        (session_id,),
                    ).fetchall()
                    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                    if "communication_events" in tables:
                        events = con.execute(
                            "SELECT event_id, event_type, sender, detail, created_at FROM communication_events WHERE session_id=? ORDER BY event_id ASC LIMIT 1000",
                            (session_id,),
                        ).fetchall()
                    else:
                        events = []
                self.send_json({"session": dict(session), "messages": rows_to_dict(messages), "events": rows_to_dict(events)})
            except Exception as exc:
                self.send_json({"error": f"{type(exc).__name__}: {exc}"}, 500)
            return

        if parsed.path == "/" or parsed.path in ("/index.html", "/app.js", "/style.css"):
            name = "index.html" if parsed.path == "/" else parsed.path.lstrip("/")
            allowed = {"index.html": "text/html; charset=utf-8", "app.js": "text/javascript; charset=utf-8", "style.css": "text/css; charset=utf-8"}
            if name not in allowed:
                self.send_json({"error": "Not found"}, 404)
                return
            file_path = WEB_DIR / name
            if not file_path.is_file():
                self.send_json({"error": "UI asset missing"}, 500)
                return
            self.send_bytes(file_path.read_bytes(), allowed[name])
            return

        self.send_json({"error": "Not found"}, 404)


def main():
    if not WEB_DIR.is_dir():
        raise SystemExit(f"Live Room web assets not found: {WEB_DIR}")
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    httpd.daemon_threads = True
    print(f"Claud Live Conversation Room: http://{HOST}:{PORT}", flush=True)
    print(f"Database (read-only): {DB_PATH}", flush=True)
    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
