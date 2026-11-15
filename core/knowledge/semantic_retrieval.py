"""
Claud AI - Semantic Retrieval Engine
Version: 1.0.0

Standalone semantic retrieval using Ollama BGE-M3.
Does not modify the existing knowledge table.
"""

import hashlib
import json
import logging
import math
import sqlite3
import urllib.request
import urllib.error

from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CONFIG_FILE = ROOT / "config" / "settings.json"
DB_FILE = ROOT / "database" / "claud_memory.db"
LOG_FILE = ROOT / "logs" / "semantic_retrieval.log"

DEFAULT_MODEL = "bge-m3"
DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"
KEEP_ALIVE = "2m"
REQUEST_TIMEOUT = 180


LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    filename=str(LOG_FILE),
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    encoding="utf-8",
)

logger = logging.getLogger("semantic_retrieval")


def now():
    return datetime.now(timezone.utc).isoformat()


def load_config():
    with open(CONFIG_FILE, "r", encoding="utf-8-sig") as file:
        return json.load(file)


def get_model():
    config = load_config()
    return config.get("embedding_model", DEFAULT_MODEL)


def get_ollama_url():
    config = load_config()
    return config.get("ollama_url", DEFAULT_OLLAMA_URL).rstrip("/")


def connect():
    DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_FILE, timeout=30)
    db.row_factory = sqlite3.Row
    return db


def initialize():
    with connect() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS knowledge_embeddings (
                knowledge_id INTEGER PRIMARY KEY,
                model TEXT NOT NULL,
                dimension INTEGER NOT NULL,
                embedding_json TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                indexed_at TEXT NOT NULL
            )
        """)

    logger.info("Semantic retrieval database initialized.")


def content_hash(content):
    return hashlib.sha256(
        content.encode("utf-8")
    ).hexdigest()


def generate_embedding(text):
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Embedding input tidak boleh kosong.")

    model = get_model()

    payload = {
        "model": model,
        "prompt": text,
        "keep_alive": KEEP_ALIVE,
    }

    request = urllib.request.Request(
        f"{get_ollama_url()}/api/embeddings",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=REQUEST_TIMEOUT
        ) as response:
            result = json.loads(response.read().decode("utf-8"))

    except (urllib.error.URLError, TimeoutError) as error:
        logger.exception("Ollama embedding request failed.")
        raise RuntimeError(
            f"Gagal menghubungi Ollama embedding API: {error}"
        ) from error

    vector = result.get("embedding")

    if not isinstance(vector, list) or not vector:
        raise RuntimeError("Ollama tidak mengembalikan embedding valid.")

    try:
        vector = [float(value) for value in vector]
    except (TypeError, ValueError) as error:
        raise RuntimeError("Embedding berisi nilai non-numerik.") from error

    if not all(math.isfinite(value) for value in vector):
        raise RuntimeError("Embedding mengandung nilai non-finite.")

    return vector


def cosine_similarity(vector_a, vector_b):
    if len(vector_a) != len(vector_b):
        raise ValueError("Dimensi embedding tidak sama.")

    dot = sum(a * b for a, b in zip(vector_a, vector_b))
    norm_a = math.sqrt(sum(a * a for a in vector_a))
    norm_b = math.sqrt(sum(b * b for b in vector_b))

    if norm_a == 0 or norm_b == 0:
        return 0.0

    return dot / (norm_a * norm_b)


def sync_embeddings():
    """
    Index new or changed knowledge records.
    Remove embeddings whose knowledge records no longer exist.
    Existing knowledge records are never modified.
    """

    initialize()

    model = get_model()

    with connect() as db:
        records = db.execute("""
            SELECT id, content
            FROM knowledge
            ORDER BY id
        """).fetchall()

        existing = {
            row["knowledge_id"]: row
            for row in db.execute("""
                SELECT knowledge_id, model, dimension, content_hash
                FROM knowledge_embeddings
            """).fetchall()
        }

    indexed = 0
    unchanged = 0
    valid_ids = {row["id"] for row in records}

    for record in records:
        item_id = record["id"]
        content = record["content"]
        digest = content_hash(content)

        previous = existing.get(item_id)

        if (
            previous
            and previous["model"] == model
            and previous["content_hash"] == digest
        ):
            unchanged += 1
            continue

        vector = generate_embedding(content)

        if previous and previous["dimension"] != len(vector):
            logger.info(
                "Knowledge ID %s embedding dimension changed.",
                item_id,
            )

        with connect() as db:
            db.execute("""
                INSERT INTO knowledge_embeddings (
                    knowledge_id,
                    model,
                    dimension,
                    embedding_json,
                    content_hash,
                    indexed_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(knowledge_id) DO UPDATE SET
                    model = excluded.model,
                    dimension = excluded.dimension,
                    embedding_json = excluded.embedding_json,
                    content_hash = excluded.content_hash,
                    indexed_at = excluded.indexed_at
            """, (
                item_id,
                model,
                len(vector),
                json.dumps(vector, separators=(",", ":")),
                digest,
                now(),
            ))

        indexed += 1
        logger.info("Indexed knowledge ID %s.", item_id)

    stale_ids = set(existing.keys()) - valid_ids

    if stale_ids:
        with connect() as db:
            db.executemany(
                "DELETE FROM knowledge_embeddings WHERE knowledge_id = ?",
                [(item_id,) for item_id in stale_ids],
            )

    result = {
        "model": model,
        "knowledge_records": len(records),
        "new_or_updated": indexed,
        "unchanged": unchanged,
        "stale_removed": len(stale_ids),
    }

    logger.info("Sync completed: %s", result)
    return result


def index_knowledge():
    return sync_embeddings()



def validate_index():
    """
    Compare every knowledge record with its indexed embedding.
    Returns detailed integrity information without modifying knowledge.
    """

    model = get_model()

    with connect() as db:
        records = db.execute("""
            SELECT id, content
            FROM knowledge
            ORDER BY id
        """).fetchall()

        embeddings = db.execute("""
            SELECT knowledge_id, model, dimension,
                   embedding_json, content_hash
            FROM knowledge_embeddings
            ORDER BY knowledge_id
        """).fetchall()

    knowledge_by_id = {
        row["id"]: row for row in records
    }

    embedding_by_id = {
        row["knowledge_id"]: row for row in embeddings
    }

    missing = []
    stale = []
    invalid = []
    valid = []

    for item_id, record in knowledge_by_id.items():
        embedding = embedding_by_id.get(item_id)

        if embedding is None:
            missing.append(item_id)
            continue

        expected_hash = content_hash(record["content"])

        reasons = []

        if embedding["content_hash"] != expected_hash:
            reasons.append("content_hash_mismatch")

        if embedding["model"] != model:
            reasons.append("model_mismatch")

        try:
            vector = json.loads(embedding["embedding_json"])

            if not isinstance(vector, list) or not vector:
                reasons.append("empty_vector")
            elif len(vector) != embedding["dimension"]:
                reasons.append("dimension_metadata_mismatch")
            elif not all(
                isinstance(value, (int, float))
                and math.isfinite(value)
                for value in vector
            ):
                reasons.append("invalid_vector_values")
        except (TypeError, ValueError, json.JSONDecodeError):
            reasons.append("invalid_embedding_json")

        if reasons:
            stale.append({
                "knowledge_id": item_id,
                "reasons": reasons,
            })
        else:
            valid.append(item_id)

    orphaned = sorted(
        set(embedding_by_id) - set(knowledge_by_id)
    )

    return {
        "model_configured": model,
        "knowledge_records": len(records),
        "embedding_records": len(embeddings),
        "valid_ids": valid,
        "missing_ids": missing,
        "stale_records": stale,
        "orphaned_ids": orphaned,
        "synchronized": (
            not missing
            and not stale
            and not orphaned
            and len(records) == len(embeddings)
        ),
    }


def get_index_status():
    initialize()

    integrity = validate_index()

    with connect() as db:
        models = [
            row[0]
            for row in db.execute("""
                SELECT DISTINCT model
                FROM knowledge_embeddings
            """).fetchall()
        ]

        dimensions = [
            row[0]
            for row in db.execute("""
                SELECT DISTINCT dimension
                FROM knowledge_embeddings
            """).fetchall()
        ]

    return {
        **integrity,
        "indexed_models": models,
        "dimensions": dimensions,
        "missing_records": len(integrity["missing_ids"]),
        "stale_records_count": len(integrity["stale_records"]),
        "orphaned_records_count": len(integrity["orphaned_ids"]),
    }



def semantic_search(query, limit=5):
    if not isinstance(query, str) or not query.strip():
        raise ValueError("Query tidak boleh kosong.")

    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise ValueError("Limit harus berupa integer positif.")

    initialize()

    integrity = validate_index()

    if not integrity["synchronized"]:
        logger.error(
            "Search blocked because index is not synchronized: %s",
            integrity,
        )
        raise RuntimeError(
            "Semantic index belum sinkron. "
            "Jalankan sync_embeddings() sebelum pencarian. "
            f"Missing={integrity['missing_ids']}; "
            f"Stale={integrity['stale_records']}; "
            f"Orphaned={integrity['orphaned_ids']}"
        )

    query_vector = generate_embedding(query)
    model = get_model()

    with connect() as db:
        rows = db.execute("""
            SELECT
                k.id,
                k.category,
                k.content,
                k.status,
                k.source,
                k.created_at,
                e.model,
                e.dimension,
                e.embedding_json
            FROM knowledge_embeddings e
            INNER JOIN knowledge k
                ON k.id = e.knowledge_id
            ORDER BY k.id
        """).fetchall()

    results = []

    for row in rows:
        if row["model"] != model:
            raise RuntimeError(
                f"Model mismatch pada Knowledge ID {row['id']}."
            )

        if row["dimension"] != len(query_vector):
            raise RuntimeError(
                f"Dimension mismatch pada Knowledge ID {row['id']}."
            )

        stored_vector = json.loads(row["embedding_json"])

        score = cosine_similarity(
            query_vector,
            stored_vector,
        )

        results.append({
            "id": row["id"],
            "category": row["category"],
            "content": row["content"],
            "status": row["status"],
            "source": row["source"],
            "created_at": row["created_at"],
            "model": row["model"],
            "score": round(score, 6),
        })

    results.sort(
        key=lambda item: item["score"],
        reverse=True,
    )

    logger.info(
        "Semantic search completed. Query length=%s, results=%s",
        len(query),
        len(results[:limit]),
    )

    return results[:limit]


def rebuild_index():
    """
    Re-embed every existing knowledge record.
    Does not delete or modify the knowledge table.
    """

    initialize()

    with connect() as db:
        db.execute("DELETE FROM knowledge_embeddings")

    logger.warning("Semantic index cleared for rebuild.")

    return sync_embeddings()


if __name__ == "__main__":
    print("SEMANTIC RETRIEVAL ENGINE")
    print("=" * 50)

    print("\nInitializing...")
    initialize()

    print("\nSynchronizing embeddings...")
    print(json.dumps(sync_embeddings(), indent=2))

    print("\nIndex status:")
    print(json.dumps(get_index_status(), indent=2))

    print("\nInitialization test completed.")
