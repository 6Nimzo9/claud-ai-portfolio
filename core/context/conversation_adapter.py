"""
Claud AI - Conversation Adapter
Version: 0.1.0

Read-only adapter for retrieving conversation history.
Does not import main.py or modify the database.
"""

import sqlite3
from pathlib import Path
from typing import Dict, List, Optional


class ConversationAdapter:
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

    def get_recent_messages(
        self,
        conversation_id: int,
        limit: int = 12,
    ) -> List[Dict]:

        if (
            not isinstance(conversation_id, int)
            or isinstance(conversation_id, bool)
            or conversation_id < 1
        ):
            raise ValueError(
                "conversation_id must be a positive integer."
            )

        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or limit < 1
        ):
            raise ValueError("limit must be a positive integer.")

        query = """
            SELECT role, content, created_at
            FROM messages
            WHERE conversation_id = ?
            ORDER BY id DESC
            LIMIT ?
        """

        with self._connect_readonly() as connection:
            connection.row_factory = sqlite3.Row

            rows = connection.execute(
                query,
                (conversation_id, limit),
            ).fetchall()

        messages = [
            {
                "role": row["role"],
                "content": row["content"],
                "created_at": row["created_at"],
            }
            for row in reversed(rows)
        ]

        return messages


VERSION = ConversationAdapter.VERSION
