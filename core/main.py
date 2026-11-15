import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
CONFIG_FILE = ROOT / "config" / "settings.json"
DB_FILE = ROOT / "database" / "claud_memory.db"

sys.path.insert(0, str(ROOT / "core"))

from reasoning.engine import build_system_prompt
from reasoning.self_critique import build_critique_messages
from knowledge.grounding import (
    build_grounded_prompt,
    validate_knowledge_references,
)
from knowledge.manager import (
    initialize as initialize_knowledge,
    add as add_knowledge,
    list_all as list_knowledge,
    search as search_knowledge,
    show as show_knowledge,
    update_status as update_knowledge_status,
    delete as delete_knowledge,
    format_context as format_knowledge_context,
    record_feedback,
    feedback_history
)

from knowledge.source_intelligence import (
    classify_query,
    database_only_answer,
    get_sources_report,
    get_runtime_config,
    get_grounding_report,
    format_evaluation_report,
)



def now():
    return datetime.now(timezone.utc).isoformat()


def load_settings():
    with open(CONFIG_FILE, "r", encoding="utf-8-sig") as file:
        return json.load(file)


def save_settings(settings):
    CONFIG_FILE.write_text(
        json.dumps(settings, indent=2, ensure_ascii=False),
        encoding="utf-8"
    )


def connect_db():
    DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(DB_FILE)


def initialize_db():
    with connect_db() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT,
                created_at TEXT
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id INTEGER,
                role TEXT,
                content TEXT,
                created_at TEXT
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT,
                created_at TEXT
            )
        """)


    with connect_db() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS evaluations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id INTEGER,
                user_question TEXT,
                draft_answer TEXT,
                final_answer TEXT,
                critique_status TEXT,
                duration_seconds REAL,
                created_at TEXT
            )
        """)


def create_conversation():
    with connect_db() as db:
        cursor = db.execute(
            "INSERT INTO conversations (title, created_at) VALUES (?, ?)",
            ("Claud AI Conversation", now())
        )
        return cursor.lastrowid


def latest_conversation():
    with connect_db() as db:
        row = db.execute(
            "SELECT id FROM conversations ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return row[0] if row else None


def save_message(conversation_id, role, content):
    with connect_db() as db:
        db.execute(
            """INSERT INTO messages
               (conversation_id, role, content, created_at)
               VALUES (?, ?, ?, ?)""",
            (conversation_id, role, content, now())
        )


def get_recent_messages(conversation_id, limit=12):
    with connect_db() as db:
        rows = db.execute(
            """SELECT role, content FROM messages
               WHERE conversation_id = ?
               ORDER BY id DESC LIMIT ?""",
            (conversation_id, limit)
        ).fetchall()

    return [
        {"role": role, "content": content}
        for role, content in reversed(rows)
    ]


def retrieve_memories(query, limit=5):
    words = {
        word.lower().strip(".,!?;:()[]{}\"'")
        for word in query.split()
        if len(word.strip(".,!?;:()[]{}\"'")) >= 4
    }

    if not words:
        return []

    matches = []

    with connect_db() as db:
        rows = db.execute(
            "SELECT id, content FROM memories ORDER BY id DESC"
        ).fetchall()

    for memory_id, content in rows:
        content_lower = content.lower()
        score = sum(1 for word in words if word in content_lower)

        if score:
            matches.append((score, memory_id, content))

    matches.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [item[2] for item in matches[:limit]]


def save_memory(content):
    with connect_db() as db:
        existing = db.execute(
            "SELECT id FROM memories WHERE LOWER(content) = LOWER(?)",
            (content.strip(),)
        ).fetchone()

        if existing:
            return False

        db.execute(
            "INSERT INTO memories (content, created_at) VALUES (?, ?)",
            (content.strip(), now())
        )

    return True


def list_memories():
    with connect_db() as db:
        return db.execute(
            "SELECT id, content FROM memories ORDER BY id DESC"
        ).fetchall()


def delete_memory(memory_id):
    with connect_db() as db:
        cursor = db.execute(
            "DELETE FROM memories WHERE id = ?", (memory_id,)
        )
        return cursor.rowcount > 0


def show_history():
    with connect_db() as db:
        rows = db.execute(
            """SELECT c.id, c.title, c.created_at, COUNT(m.id)
               FROM conversations c
               LEFT JOIN messages m ON m.conversation_id = c.id
               GROUP BY c.id
               ORDER BY c.id DESC LIMIT 15"""
        ).fetchall()

    print("\nPREVIOUS CONVERSATIONS")

    for row in rows:
        print(
            f"ID: {row[0]} | {row[1]} | "
            f"Messages: {row[3]} | {row[2]}"
        )


def ollama_chat(settings, messages):
    ollama_url = settings.get(
        "ollama_url", "http://127.0.0.1:11434"
    )
    model = settings.get("local_model", settings.get("model", "llama3.2"))

    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "options": {
            "num_ctx": 2048,
            "temperature": 0.7
        }
    }

    response = requests.post(
        f"{ollama_url.rstrip('/')}/api/chat",
        json=payload,
        timeout=300
    )
    response.raise_for_status()

    return response.json()["message"]["content"].strip()



def generate_answer(settings, conversation_id, user_text):
    import time

    started = time.perf_counter()

    # Capture history before saving the current message.
    history = get_recent_messages(conversation_id)

    # Historical assistant responses are dialogue context, never evidence.
    history = [
        {
            **message,
            "content": (
                "[HISTORICAL ASSISTANT RESPONSE - NOT VERIFIED EVIDENCE]\n"
                + message.get("content", "")
            ),
        }
        if message.get("role") == "assistant"
        else message
        for message in history
    ]
    memories = retrieve_memories(user_text)

    base_prompt = settings.get(
        "system_prompt",
        "You are Claud AI, a helpful cognitive intelligence assistant."
    )

    system_prompt = build_system_prompt(
        base_prompt,
        memories,
        settings.get("reasoning_enabled", True)
    )

    evidence_context = ""

    if settings.get("grounding_enabled", True):
        system_prompt, evidence_context = build_grounded_prompt(
            user_text,
            system_prompt
        )
    else:
        evidence_context = format_knowledge_context(user_text)
        if evidence_context:
            system_prompt += "\n\n" + evidence_context

    system_prompt += """

ACTIVE EVIDENCE AUTHORITY:
- Current retrieved database evidence is authoritative for database claims.
- Historical assistant responses are dialogue context, not evidence.
- Never reuse historical Knowledge IDs unless present in current evidence.
- Previous assistant responses are not proof of correctness.
- Personal memories are context, not proof of implementation.
"""

    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_text})

    draft = ollama_chat(settings, messages)

    draft_validation = validate_knowledge_references(
        draft,
        evidence_context
    )

    if not draft_validation["valid"]:
        print(
            "[EVIDENCE GUARD] Draft rejected. "
            f"Unsupported Knowledge IDs: {draft_validation['invalid_ids']}"
        )
        safe_answer = (
            "Saya tidak dapat menggunakan jawaban tersebut karena "
            "memuat Knowledge ID yang tidak tersedia dalam evidence "
            "aktif untuk pertanyaan ini."
        )
        return (
            safe_answer,
            "rejected_unsupported_reference",
            time.perf_counter() - started,
            draft
        )

    if not settings.get("critique_enabled", True):
        return draft, "disabled", time.perf_counter() - started, draft

    print("\n[SELF-CRITIQUE] Evaluating response quality...")

    try:
        critique_messages = build_critique_messages(user_text, draft, evidence_context)
        improved = ollama_chat(settings, critique_messages)

        if not improved:
            return draft, "fallback_empty", time.perf_counter() - started, draft

        improved_validation = validate_knowledge_references(
            improved,
            evidence_context
        )

        if not improved_validation["valid"]:
            print(
                "[EVIDENCE GUARD] Critique result rejected. "
                f"Unsupported Knowledge IDs: "
                f"{improved_validation['invalid_ids']}"
            )
            return (
                draft,
                "rejected_unsupported_reference_after_critique",
                time.perf_counter() - started,
                draft
            )

        # Basic safeguard against an unexpectedly destructive shortening.
        # A shorter answer is accepted only when it remains reasonably substantial.
        if len(draft) >= 300 and len(improved) < len(draft) * 0.50:
            print("[SELF-CRITIQUE] Rejected: answer lost too much substance.")
            return draft, "rejected_shortening", time.perf_counter() - started, draft

        return improved, "completed", time.perf_counter() - started, draft

    except requests.exceptions.RequestException as error:
        print(
            "[SELF-CRITIQUE] Evaluation unavailable; "
            "using original answer. " + str(error)
        )
        return draft, "fallback_error", time.perf_counter() - started, draft


def log_evaluation(
    conversation_id,
    user_question,
    draft_answer,
    final_answer,
    critique_status,
    duration_seconds
):
    with connect_db() as db:
        db.execute(
            """
            INSERT INTO evaluations (
                conversation_id,
                user_question,
                draft_answer,
                final_answer,
                critique_status,
                duration_seconds,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                conversation_id,
                user_question,
                draft_answer,
                final_answer,
                critique_status,
                duration_seconds,
                now()
            )
        )


def show_evaluation_history(limit=10):
    with connect_db() as db:
        rows = db.execute(
            """
            SELECT id, critique_status, duration_seconds, created_at
            FROM evaluations
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,)
        ).fetchall()

    print("\nEVALUATION HISTORY")
    if not rows:
        print("No evaluation records yet.")
        return

    for row in rows:
        duration = row[2] if row[2] is not None else 0
        print(
            f"ID: {row[0]} | Status: {row[1]} | "
            f"Duration: {duration:.2f}s | {row[3]}"
        )



def print_sources_report():
    report = get_sources_report()
    print("\nSOURCE REGISTRY")
    print(f"Knowledge records : {report['knowledge_database']}")
    print(f"Stored memories   : {report['stored_memories']}")
    print(f"Evaluations       : {report['evaluations']}")
    print(f"Configuration    : {report['configuration']}")
    print(f"Database         : {report['database']}")


def print_runtime_config():
    config = get_runtime_config()
    print("\nRUNTIME CONFIGURATION")

    labels = {
        "version": "Version",
        "model": "Local model",
        "ollama_url": "Ollama URL",
        "reasoning_enabled": "Reasoning",
        "critique_enabled": "Self-critique",
        "grounding_enabled": "Grounding",
    }

    for key, label in labels.items():
        value = config.get(key)
        if isinstance(value, bool):
            value = "Enabled" if value else "Disabled"
        print(f"{label:18}: {value}")


def print_grounding_report():
    report = get_grounding_report()
    print("\nGROUNDING STATUS")

    if isinstance(report, dict):
        for key, value in report.items():
            print(f"{key}: {value}")
    else:
        print(report)


def show_evaluation_audit(evaluation_id):
    with connect_db() as db:
        row = db.execute(
            """
            SELECT id, user_question, draft_answer, final_answer,
                   critique_status, duration_seconds, created_at
            FROM evaluations
            WHERE id = ?
            """,
            (evaluation_id,)
        ).fetchone()

    if not row:
        print("Evaluation not found.")
        return

    labels = (
        "Evaluation ID",
        "Question",
        "Draft answer",
        "Final answer",
        "Status",
        "Duration (seconds)",
        "Created at",
    )

    print("\nEVALUATION AUDIT")
    for label, value in zip(labels, row):
        print(f"\n{label}:")
        print(value if value is not None else "Not available")


def deterministic_configuration_answer():
    config = get_runtime_config()

    return (
        "INFORMASI KONFIGURASI CLAUD AI\n"
        f"Version: {config.get('version')}\n"
        f"Local model: {config.get('model')}\n"
        f"Ollama URL: {config.get('ollama_url')}\n"
        f"Reasoning enabled: {config.get('reasoning_enabled')}\n"
        f"Self-critique enabled: {config.get('critique_enabled')}\n"
        f"Grounding enabled: {config.get('grounding_enabled')}\n"
        "\nInformasi ini dibaca langsung dari konfigurasi runtime."
    )


def print_help():
    print("""
COMMANDS
/sources          Show registered data sources and counts
/config           Show runtime configuration
/grounding        Show grounding status
/audit ID         Inspect an evaluation record
/help             Show commands
/new              Start a new conversation
/history          List previous conversations
/resume ID        Resume a conversation
/remember TEXT    Save a memory
/memories         List saved memories
/forget ID        Delete a memory
/reasoning        Show reasoning status
/reasoning on     Enable reasoning
/reasoning off    Disable reasoning
/critique         Show self-critique status
/critique on      Enable self-critique
/critique off     Disable self-critique
/exit             Exit Claud AI
""")


def main():
    initialize_db()
    initialize_knowledge()
    settings = load_settings()

    conversation_id = latest_conversation()

    if conversation_id is None:
        conversation_id = create_conversation()

    print("\n=======================================================")
    print(" CLAUD AI - PERSONAL COGNITIVE INTELLIGENCE")
    print("=======================================================")
    print(f"Version  : {settings.get('version', '0.4.0')}")
    print(f"Engine   : {settings.get('model', 'llama3.2')}")
    print("Memory   : SQLite persistent memory")
    print(
        "Reasoning: "
        + ("Enabled" if settings.get("reasoning_enabled", True) else "Disabled")
    )
    print(
        "Critique : "
        + ("Enabled" if settings.get("critique_enabled", True) else "Disabled")
    )
    print(f"Storage  : {ROOT}")
    print(f"Session  : {conversation_id}")
    print("\nType /help for commands.")
    print("=======================================================\n")

    while True:
        try:
            user_text = input("YOU > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nClaud AI session ended.")
            break

        if not user_text:
            continue

        if user_text == "/exit":
            print("Claud AI session ended.")
            break

        if user_text == "/help":
            print_help()
            continue

        if user_text == "/new":
            conversation_id = create_conversation()
            print(f"New conversation created: {conversation_id}")
            continue

        if user_text == "/history":
            show_history()
            continue

        if user_text.startswith("/resume "):
            try:
                target_id = int(user_text.split(maxsplit=1)[1])

                with connect_db() as db:
                    row = db.execute(
                        "SELECT id FROM conversations WHERE id = ?",
                        (target_id,)
                    ).fetchone()

                if row:
                    conversation_id = target_id
                    print(f"Resumed conversation {conversation_id}")
                else:
                    print("Conversation not found.")

            except (ValueError, IndexError):
                print("Usage: /resume ID")

            continue

        if user_text.startswith("/remember "):
            content = user_text[len("/remember "):].strip()

            if content:
                print(
                    "Memory saved."
                    if save_memory(content)
                    else "This memory already exists."
                )
            else:
                print("Usage: /remember TEXT")

            continue

        if user_text == "/memories":
            memories = list_memories()
            print("\nSAVED MEMORIES")

            if not memories:
                print("No memories saved.")

            for memory_id, content in memories:
                print(f"{memory_id}: {content}")

            continue

        if user_text.startswith("/forget "):
            try:
                memory_id = int(user_text.split(maxsplit=1)[1])
                print(
                    "Memory deleted."
                    if delete_memory(memory_id)
                    else "Memory not found."
                )
            except (ValueError, IndexError):
                print("Usage: /forget ID")

            continue


        if user_text == "/knowledge":
            rows = list_knowledge()
            print("\nKNOWLEDGE BASE")
            if not rows:
                print("No knowledge entries.")
            for row in rows:
                print(
                    f"{row[0]} | {row[1]} | {row[3]} | {row[2]}"
                )
            continue

        if user_text.startswith("/knowledge search "):
            term = user_text[len("/knowledge search "):].strip()
            rows = search_knowledge(term)
            print("\nKNOWLEDGE SEARCH")
            for row in rows:
                print(f"{row[0]} | {row[1]} | {row[3]} | {row[2]}")
            if not rows:
                print("No matching knowledge.")
            continue

        if user_text.startswith("/knowledge show "):
            try:
                item_id = int(user_text.rsplit(" ", 1)[1])
                row = show_knowledge(item_id)
                print(row if row else "Knowledge entry not found.")
            except ValueError:
                print("Usage: /knowledge show ID")
            continue

        if user_text.startswith("/knowledge delete "):
            try:
                item_id = int(user_text.rsplit(" ", 1)[1])
                print(
                    "Knowledge deleted."
                    if delete_knowledge(item_id)
                    else "Knowledge not found."
                )
            except ValueError:
                print("Usage: /knowledge delete ID")
            continue

        if user_text.startswith("/know "):
            payload = user_text[len("/know "):]
            if "|" not in payload:
                print("Usage: /know CATEGORY | TEXT")
                continue
            category, content = payload.split("|", 1)
            try:
                item_id, created = add_knowledge(category, content)
                print(
                    f"Knowledge saved. ID: {item_id}"
                    if created else f"Duplicate entry. Existing ID: {item_id}"
                )
            except ValueError as error:
                print(str(error))
            continue

        if user_text.startswith("/knowstatus "):
            parts = user_text.split(maxsplit=2)
            if len(parts) != 3:
                print("Usage: /knowstatus ID STATUS")
                continue
            try:
                success = update_knowledge_status(int(parts[1]), parts[2])
                print("Knowledge status updated." if success else "Knowledge not found.")
            except ValueError as error:
                print(str(error))
            continue

        if user_text.startswith("/feedback "):
            payload = user_text[len("/feedback "):]
            if "|" in payload:
                header, comment = payload.split("|", 1)
            else:
                header, comment = payload, ""
            parts = header.split()
            if len(parts) != 2:
                print("Usage: /feedback EVALUATION_ID good|bad | COMMENT")
                continue
            try:
                record_feedback(int(parts[0]), parts[1], comment)
                print("Feedback saved.")
            except ValueError as error:
                print(str(error))
            continue

        if user_text == "/feedbacks":
            rows = feedback_history()
            print("\nUSER FEEDBACK HISTORY")
            if not rows:
                print("No feedback records.")
            for row in rows:
                print(
                    f"ID: {row[0]} | Evaluation: {row[1]} | "
                    f"Rating: {row[2]} | Comment: {row[3]}"
                )
            continue

        if user_text == "/reasoning":
            print(
                "Reasoning protocol:",
                "Enabled" if settings.get("reasoning_enabled", True)
                else "Disabled"
            )
            continue

        if user_text in ("/reasoning on", "/reasoning off"):
            settings["reasoning_enabled"] = user_text.endswith("on")
            save_settings(settings)
            print(
                "Reasoning protocol "
                + ("enabled." if settings["reasoning_enabled"] else "disabled.")
            )
            continue

        if user_text in ("/evaluation", "/evaluations"):
            show_evaluation_history()
            continue

        if user_text == "/critique":
            print(
                "Self-critique:",
                "Enabled" if settings.get("critique_enabled", True)
                else "Disabled"
            )
            continue

        if user_text in ("/critique on", "/critique off"):
            settings["critique_enabled"] = user_text.endswith("on")
            save_settings(settings)
            print(
                "Self-critique "
                + ("enabled." if settings["critique_enabled"] else "disabled.")
            )
            continue

        if user_text == "/sources":
            print_sources_report()
            continue

        if user_text == "/config":
            print_runtime_config()
            continue

        if user_text == "/grounding":
            print_grounding_report()
            continue

        if user_text.startswith("/audit"):
            parts = user_text.split()

            if len(parts) != 2:
                print("Usage: /audit ID")
            else:
                try:
                    show_evaluation_audit(int(parts[1]))
                except ValueError:
                    print("Evaluation ID must be a number.")

            continue

        try:
            route = classify_query(user_text)

            if route == "evaluation":
                answer = format_evaluation_report()
                critique_status = "deterministic_evaluation"
                duration = 0.0
                draft = answer

            elif route == "database":
                answer = database_only_answer(user_text)

                if answer is None:
                    answer, critique_status, duration, draft = generate_answer(
                        settings, conversation_id, user_text
                    )
                else:
                    critique_status = "deterministic_database"
                    duration = 0.0
                    draft = answer

            elif route == "configuration":
                answer = deterministic_configuration_answer()
                critique_status = "deterministic_configuration"
                duration = 0.0
                draft = answer

            elif route in ("analytical", "general"):
                answer, critique_status, duration, draft = generate_answer(
                    settings, conversation_id, user_text
                )

            save_message(conversation_id, "user", user_text)
            save_message(conversation_id, "assistant", answer)

            log_evaluation(
                conversation_id,
                user_text,
                draft,
                answer,
                critique_status,
                duration
            )

            if critique_status == "completed":
                print("[SELF-CRITIQUE] Review completed.")
            elif critique_status == "rejected_shortening":
                print("[SELF-CRITIQUE] Original answer preserved.")
            elif critique_status.startswith("fallback"):
                print("[SELF-CRITIQUE] Original answer preserved.")

            print(f"[EVALUATION] Status: {critique_status}")
            print(f"\nCLAUD AI > {answer}\n")

        except requests.exceptions.RequestException as error:
            print(f"\nOLLAMA CONNECTION ERROR: {error}\n")
        except Exception as error:
            print(f"\nCLAUD AI ERROR: {error}\n")


if __name__ == "__main__":
    main()
