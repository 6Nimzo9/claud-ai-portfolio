"""
Claud AI - Memory Adapter
Version: 0.1.0

Read-only memory retrieval adapter.
Preserves the keyword-scoring behavior of Core main.py.
Does not import main.py or modify the database.
"""

import sqlite3
from pathlib import Path
from typing import Dict, List, Optional


class MemoryAdapter:
    VERSION = "0.1.0"

    def __init__(self, database_path: Optional[str] = None):
        if database_path is None:
            root = Path(__file__).resolve().parents[2]
            database_path = root / "database" / "claud_memory.db"

        self.database_path = Path(database_path).resolve()

    def _connect_readonly(self):
        if not self.database_path.is_file():
            raise FileNotFoundError(
                f"Memory database not found: {self.database_path}"
            )

        uri = self.database_path.as_uri() + "?mode=ro"
        return sqlite3.connect(uri, uri=True)

    @staticmethod
    def _extract_query_words(query: str) -> set:
        punctuation = ".,!?;:()[]{}\"'"

        return {
            word.lower().strip(punctuation)
            for word in query.split()
            if len(word.strip(punctuation)) >= 4
        }

    def search(
        self,
        query: str,
        limit: int = 5,
    ) -> List[Dict]:

        if not isinstance(query, str) or not query.strip():
            raise ValueError("Memory query must be a non-empty string.")

        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or limit < 1
        ):
            raise ValueError("limit must be a positive integer.")

        words = self._extract_query_words(query)

        if not words:
            return []

        with self._connect_readonly() as connection:
            connection.row_factory = sqlite3.Row

            rows = connection.execute("""
                SELECT id, content, source, created_at
                FROM memories
                ORDER BY id DESC
            """).fetchall()

        matches = []

        for row in rows:
            content = row["content"] or ""
            content_lower = content.lower()

            score = sum(
                1 for word in words
                if word in content_lower
            )

            if score:
                matches.append({
                    "id": row["id"],
                    "content": content,
                    "source": row["source"],
                    "created_at": row["created_at"],
                    "score": score,
                })

        matches.sort(
            key=lambda item: (item["score"], item["id"]),
            reverse=True,
        )

        return matches[:limit]


VERSION = MemoryAdapter.VERSION
