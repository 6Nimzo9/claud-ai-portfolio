"""
Claud AI - Context Knowledge Adapter
Version: 0.1.0

Read-only adapter for lexical, semantic, and hybrid knowledge retrieval.
Does not modify the existing Knowledge Engine or database.
"""

import hashlib
import json
import math
import re
import sqlite3
import urllib.request
import urllib.error

from pathlib import Path
from typing import Any, Dict, List, Optional

from .models import ContextSourceResult


ROOT = Path(__file__).resolve().parents[2]
CONFIG_FILE = ROOT / "config" / "settings.json"
DB_FILE = ROOT / "database" / "claud_memory.db"

DEFAULT_MODEL = "bge-m3"
DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"
REQUEST_TIMEOUT = 180
DEFAULT_RRF_K = 60
VERSION = "0.1.0"


STOPWORDS = {
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

TOKEN_ALIASES = {
    "bahasa": "language",
    "bahasanya": "language",
}


class KnowledgeAdapter:
    """Read-only knowledge retrieval adapter."""

    def __init__(
        self,
        db_path: Optional[Path] = None,
        config_path: Optional[Path] = None,
    ):
        self.db_path = Path(db_path or DB_FILE)
        self.config_path = Path(config_path or CONFIG_FILE)

    def _load_config(self):
        if not self.config_path.is_file():
            raise FileNotFoundError(
                f"Configuration file not found: {self.config_path}"
            )

        with self.config_path.open(
            "r", encoding="utf-8-sig"
        ) as file:
            config = json.load(file)

        model = config.get("embedding_model") or DEFAULT_MODEL
        ollama_url = (
            config.get("ollama_url") or DEFAULT_OLLAMA_URL
        ).rstrip("/")

        if not isinstance(model, str) or not model.strip():
            raise ValueError("Invalid embedding model configuration.")

        if not isinstance(ollama_url, str) or not ollama_url.startswith(
            ("http://", "https://")
        ):
            raise ValueError("Invalid Ollama URL configuration.")

        return model.strip(), ollama_url

    def _connect_readonly(self):
        if not self.db_path.is_file():
            raise FileNotFoundError(
                f"Knowledge database not found: {self.db_path}"
            )

        uri = self.db_path.resolve().as_uri() + "?mode=ro"

        db = sqlite3.connect(
            uri,
            uri=True,
            timeout=30,
        )
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only = ON")
        return db

    @staticmethod
    def _validate_limit(limit):
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or limit < 1
        ):
            raise ValueError("Limit must be a positive integer.")

    @staticmethod
    def _retrieval_tokens(value):
        if not isinstance(value, str):
            return set()

        tokens = re.findall(
            r"[a-z0-9]+(?:[._-][a-z0-9]+)*",
            value.lower(),
        )

        normalized = set()

        for token in tokens:
            token = TOKEN_ALIASES.get(token, token)

            if token in STOPWORDS or len(token) < 3:
                continue

            normalized.add(token)

        return normalized

    def _read_records(self):
        with self._connect_readonly() as db:
            rows = db.execute("""
                SELECT id, category, content, status,
                       source, created_at
                FROM knowledge
                ORDER BY id DESC
            """).fetchall()

        return [dict(row) for row in rows]

    def _lexical_search(
        self,
        query,
        limit=5,
        min_score=2,
        records=None,
    ):
        self._validate_limit(limit)
        self._validate_limit(min_score)

        query_tokens = self._retrieval_tokens(query)

        if not query_tokens:
            return []

        if records is None:
            records = self._read_records()

        scored = []

        for record in records:
            content_tokens = self._retrieval_tokens(
                record["content"]
            )

            matched_tokens = query_tokens.intersection(
                content_tokens
            )

            score = len(matched_tokens)

            if score >= min_score:
                scored.append({
                    **record,
                    "score": score,
                })

        scored.sort(
            key=lambda item: (item["score"], item["id"]),
            reverse=True,
        )

        return scored[:limit]

    @staticmethod
    def _content_hash(content):
        return hashlib.sha256(
            content.encode("utf-8")
        ).hexdigest()

    def _validate_index(self, records, model):
        with self._connect_readonly() as db:
            embeddings = db.execute("""
                SELECT knowledge_id, model, dimension,
                       embedding_json, content_hash
                FROM knowledge_embeddings
                ORDER BY knowledge_id
            """).fetchall()

        knowledge_by_id = {
            item["id"]: item for item in records
        }

        embedding_by_id = {
            row["knowledge_id"]: dict(row)
            for row in embeddings
        }

        missing = []
        stale = []
        valid = []

        for item_id, record in knowledge_by_id.items():
            embedding = embedding_by_id.get(item_id)

            if embedding is None:
                missing.append(item_id)
                continue

            reasons = []

            expected_hash = self._content_hash(
                record["content"]
            )

            if embedding["content_hash"] != expected_hash:
                reasons.append("content_hash_mismatch")

            if embedding["model"] != model:
                reasons.append("model_mismatch")

            try:
                vector = json.loads(
                    embedding["embedding_json"]
                )

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

        synchronized = (
            not missing
            and not stale
            and not orphaned
            and len(records) == len(embeddings)
        )

        return {
            "model_configured": model,
            "knowledge_records": len(records),
            "embedding_records": len(embeddings),
            "valid_ids": valid,
            "missing_ids": missing,
            "stale_records": stale,
            "orphaned_ids": orphaned,
            "synchronized": synchronized,
        }, embedding_by_id

    @staticmethod
    def _cosine_similarity(vector_a, vector_b):
        if len(vector_a) != len(vector_b):
            raise ValueError("Embedding dimensions do not match.")

        dot = sum(
            a * b for a, b in zip(vector_a, vector_b)
        )

        norm_a = math.sqrt(sum(a * a for a in vector_a))
        norm_b = math.sqrt(sum(b * b for b in vector_b))

        if norm_a == 0 or norm_b == 0:
            return 0.0

        return dot / (norm_a * norm_b)

    def _generate_embedding(self, text, model, ollama_url):
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Embedding input cannot be empty.")

        payload = {
            "model": model,
            "prompt": text,
            "keep_alive": "2m",
        }

        request = urllib.request.Request(
            f"{ollama_url}/api/embeddings",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(
                request,
                timeout=REQUEST_TIMEOUT,
            ) as response:
                result = json.loads(
                    response.read().decode("utf-8")
                )

        except (urllib.error.URLError, TimeoutError) as error:
            raise RuntimeError(
                f"Ollama embedding request failed: {error}"
            ) from error

        vector = result.get("embedding")

        if not isinstance(vector, list) or not vector:
            raise RuntimeError(
                "Ollama did not return a valid embedding."
            )

        try:
            vector = [float(value) for value in vector]
        except (TypeError, ValueError) as error:
            raise RuntimeError(
                "Embedding contains non-numeric values."
            ) from error

        if not all(math.isfinite(value) for value in vector):
            raise RuntimeError(
                "Embedding contains non-finite values."
            )

        return vector

    def _semantic_search(
        self,
        query,
        limit,
        records,
        model,
        ollama_url,
        integrity,
        embedding_by_id,
    ):
        if not integrity["synchronized"]:
            raise RuntimeError(
                "Semantic index is not synchronized. "
                f"Missing={integrity['missing_ids']}; "
                f"Stale={integrity['stale_records']}; "
                f"Orphaned={integrity['orphaned_ids']}"
            )

        query_vector = self._generate_embedding(
            query, model, ollama_url
        )

        record_by_id = {
            item["id"]: item for item in records
        }

        results = []

        for item_id in sorted(record_by_id):
            embedding = embedding_by_id[item_id]

            if embedding["model"] != model:
                raise RuntimeError(
                    f"Model mismatch for knowledge ID {item_id}."
                )

            if embedding["dimension"] != len(query_vector):
                raise RuntimeError(
                    f"Dimension mismatch for knowledge ID {item_id}."
                )

            stored_vector = json.loads(
                embedding["embedding_json"]
            )

            score = self._cosine_similarity(
                query_vector,
                stored_vector,
            )

            results.append({
                **record_by_id[item_id],
                "model": model,
                "score": round(score, 6),
            })

        results.sort(
            key=lambda item: item["score"],
            reverse=True,
        )

        return results[:limit]

    @staticmethod
    def _reciprocal_rank_fusion(
        lexical_results,
        semantic_results,
        limit,
        rrf_k=DEFAULT_RRF_K,
    ):
        if (
            not isinstance(rrf_k, int)
            or isinstance(rrf_k, bool)
            or rrf_k < 1
        ):
            raise ValueError("rrf_k must be a positive integer.")

        fused = {}

        def add_result(item, rank, source):
            item_id = item["id"]

            if item_id not in fused:
                fused[item_id] = {
                    "id": item_id,
                    "rrf_score": 0.0,
                    "lexical_rank": None,
                    "semantic_rank": None,
                    "lexical_score": None,
                    "semantic_score": None,
                    "category": item.get("category"),
                    "content": item.get("content"),
                    "status": item.get("status"),
                    "source": item.get("source"),
                    "created_at": item.get("created_at"),
                }

            entry = fused[item_id]
            entry["rrf_score"] += 1.0 / (rrf_k + rank)

            for field in (
                "category", "content", "status",
                "source", "created_at",
            ):
                if entry[field] is None and item.get(field) is not None:
                    entry[field] = item[field]

            entry[f"{source}_rank"] = rank
            entry[f"{source}_score"] = item.get("score")

        for rank, item in enumerate(lexical_results, 1):
            add_result(item, rank, "lexical")

        for rank, item in enumerate(semantic_results, 1):
            add_result(item, rank, "semantic")

        results = list(fused.values())

        results.sort(
            key=lambda item: (-item["rrf_score"], item["id"])
        )

        for item in results:
            item["rrf_score"] = round(
                item["rrf_score"], 8
            )

        return results[:limit]

    def retrieve(
        self,
        query,
        mode="none",
        limit=8,
        min_score=2,
        rrf_k=DEFAULT_RRF_K,
    ):
        if not isinstance(query, str) or not query.strip():
            raise ValueError("Knowledge query cannot be empty.")

        self._validate_limit(limit)

        if mode not in {"none", "lexical", "hybrid"}:
            raise ValueError(
                "Knowledge mode must be none, lexical, or hybrid."
            )

        if mode == "none":
            return ContextSourceResult(
                source="knowledge",
                status="not_requested",
                method="none",
                metadata={
                    "version": VERSION,
                    "execution_enabled": False,
                },
            )

        records = self._read_records()

        if mode == "lexical":
            items = self._lexical_search(
                query=query,
                limit=limit,
                min_score=min_score,
                records=records,
            )

            return ContextSourceResult(
                source="knowledge",
                status="success" if items else "no_results",
                items=items,
                method="lexical",
                metadata={
                    "version": VERSION,
                    "record_count": len(records),
                    "min_score": min_score,
                    "execution_enabled": False,
                },
            )

        model, ollama_url = self._load_config()

        integrity, embedding_by_id = self._validate_index(
            records, model
        )

        lexical_results = self._lexical_search(
            query=query,
            limit=limit,
            min_score=min_score,
            records=records,
        )

        semantic_results = self._semantic_search(
            query=query,
            limit=limit,
            records=records,
            model=model,
            ollama_url=ollama_url,
            integrity=integrity,
            embedding_by_id=embedding_by_id,
        )

        items = self._reciprocal_rank_fusion(
            lexical_results=lexical_results,
            semantic_results=semantic_results,
            limit=limit,
            rrf_k=rrf_k,
        )

        return ContextSourceResult(
            source="knowledge",
            status="success" if items else "no_results",
            items=items,
            method="hybrid",
            metadata={
                "version": VERSION,
                "model": model,
                "index_integrity": integrity,
                "lexical_result_count": len(lexical_results),
                "semantic_result_count": len(semantic_results),
                "rrf_k": rrf_k,
                "execution_enabled": False,
            },
        )
