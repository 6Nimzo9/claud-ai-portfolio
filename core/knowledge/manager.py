import sqlite3
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[2]
DB_FILE = ROOT / "database" / "claud_memory.db"

CATEGORIES = {
    "fact",
    "assumption",
    "inference",
    "opinion",
    "open_question"
}

def now():
    return datetime.now(timezone.utc).isoformat()

def connect():
    DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(DB_FILE)

def initialize():
    with connect() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS knowledge (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT NOT NULL,
                content TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'unverified',
                source TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS user_feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                evaluation_id INTEGER,
                rating TEXT NOT NULL,
                comment TEXT,
                created_at TEXT NOT NULL
            )
        """)

def add(category, content, source="user"):
    category = category.lower().strip()
    content = content.strip()

    if category not in CATEGORIES:
        raise ValueError(
            "Category must be: " + ", ".join(sorted(CATEGORIES))
        )
    if not content:
        raise ValueError("Knowledge content cannot be empty.")

    status = "unverified"
    if category == "opinion":
        status = "subjective"
    elif category == "open_question":
        status = "unresolved"

    with connect() as db:
        existing = db.execute(
            """SELECT id FROM knowledge
               WHERE category = ? AND LOWER(TRIM(content)) = LOWER(TRIM(?))""",
            (category, content)
        ).fetchone()

        if existing:
            return existing[0], False

        cursor = db.execute(
            """INSERT INTO knowledge
               (category, content, status, source, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (category, content, status, source, now(), now())
        )
        return cursor.lastrowid, True

def list_all(category=None):
    with connect() as db:
        if category:
            return db.execute(
                """SELECT id, category, content, status, source, created_at
                   FROM knowledge WHERE category = ? ORDER BY id DESC""",
                (category,)
            ).fetchall()

        return db.execute(
            """SELECT id, category, content, status, source, created_at
               FROM knowledge ORDER BY id DESC"""
        ).fetchall()

def search(term, limit=10):
    pattern = "%" + term.strip() + "%"
    with connect() as db:
        return db.execute(
            """SELECT id, category, content, status, source
               FROM knowledge
               WHERE content LIKE ? OR category LIKE ?
               ORDER BY id DESC LIMIT ?""",
            (pattern, pattern, limit)
        ).fetchall()

def show(item_id):
    with connect() as db:
        return db.execute(
            """SELECT id, category, content, status, source, created_at, updated_at
               FROM knowledge WHERE id = ?""",
            (item_id,)
        ).fetchone()

def update_status(item_id, status):
    allowed = {"unverified", "verified", "disputed", "subjective", "unresolved"}
    if status not in allowed:
        raise ValueError("Invalid status: " + ", ".join(sorted(allowed)))

    with connect() as db:
        cursor = db.execute(
            """UPDATE knowledge SET status = ?, updated_at = ?
               WHERE id = ?""",
            (status, now(), item_id)
        )
        return cursor.rowcount > 0

def delete(item_id):
    with connect() as db:
        cursor = db.execute(
            "DELETE FROM knowledge WHERE id = ?", (item_id,)
        )
        return cursor.rowcount > 0

def _retrieval_tokens(value):
    """
    Normalize text into whole-word tokens.
    This is lexical matching, not semantic understanding.
    """
    import re

    stopwords = {
        "yang", "dan", "atau", "dari", "dengan", "untuk",
        "pada", "dalam", "oleh", "sebagai", "adalah",
        "itu", "ini", "apa", "apakah", "bagaimana",
        "mengapa", "kapan", "dimana", "tentang", "agar",
        "dapat", "bisa", "akan", "sudah", "belum",
        "nantinya", "secara", "lebih", "sangat", "masih",
        "saat", "ketika", "saya", "kita", "mereka",
        "claud", "claudai", "claud_ai", "ai",
        "fakta", "benar", "tersimpan", "digunakan",
        "melakukan", "membuat", "menjadi", "meningkatkan",
        "kemampuan", "efektif", "canggih",
    }

    aliases = {
        "bahasa": "language",
        "bahasanya": "language",
    }

    tokens = re.findall(
        r"[a-z0-9]+(?:[._-][a-z0-9]+)*",
        value.lower()
    )

    normalized = set()

    for token in tokens:
        token = aliases.get(token, token)

        if token not in stopwords and len(token) >= 3:
            normalized.add(token)

    return normalized


def retrieve(query, limit=5, min_score=2):
    """
    Improved lexical retrieval.

    - Matches whole tokens, not substrings.
    - Removes common Indonesian words and generic identity terms.
    - Requires a minimum number of meaningful token matches.
    - Preserves the existing result tuple structure.
    - Does not claim semantic understanding.
    """
    query_tokens = _retrieval_tokens(query)

    if not query_tokens:
        return []

    rows = list_all()
    scored = []

    for row in rows:
        item_id, category, content, status, source, created_at = row

        content_tokens = _retrieval_tokens(content)
        matched_tokens = query_tokens.intersection(content_tokens)
        score = len(matched_tokens)

        if score >= min_score:
            scored.append(
                (score, item_id, category, content, status)
            )

    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)

    return scored[:limit]


def record_feedback(evaluation_id, rating, comment=""):
    rating = rating.lower().strip()
    if rating not in {"good", "bad"}:
        raise ValueError("Rating must be good or bad.")

    with connect() as db:
        cursor = db.execute(
            """SELECT id FROM evaluations WHERE id = ?""",
            (evaluation_id,)
        )
        if not cursor.fetchone():
            raise ValueError("Evaluation ID not found.")

        db.execute(
            """INSERT INTO user_feedback
               (evaluation_id, rating, comment, created_at)
               VALUES (?, ?, ?, ?)""",
            (evaluation_id, rating, comment.strip(), now())
        )

def feedback_history(limit=10):
    with connect() as db:
        return db.execute(
            """SELECT id, evaluation_id, rating, comment, created_at
               FROM user_feedback ORDER BY id DESC LIMIT ?""",
            (limit,)
        ).fetchall()

def format_context(query):
    items = retrieve(query)
    if not items:
        return ""

    lines = [
        "RELEVANT KNOWLEDGE BASE ENTRIES:",
        "These are stored records, not automatically verified truths."
    ]

    for item_id, category, content, status in [
        (x[1], x[2], x[3], x[4]) for x in items
    ]:
        lines.append(
            f"- [{category.upper()} | {status} | ID {item_id}] {content}"
        )

    lines.append(
        "Do not present unverified records as confirmed facts. "
        "Distinguish them from independently established information."
    )
    return "\n".join(lines)
