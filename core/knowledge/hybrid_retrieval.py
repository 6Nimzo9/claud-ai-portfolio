"""
Claud AI - Hybrid Retrieval Engine
Version: 1.0.0

Combines lexical and semantic rankings using
Reciprocal Rank Fusion (RRF).

Does not modify the existing retrieval or grounding modules.
"""

import logging
from pathlib import Path

from .manager import retrieve, list_all
from .semantic_retrieval import semantic_search


ROOT = Path(__file__).resolve().parents[2]
LOG_FILE = ROOT / "logs" / "hybrid_retrieval.log"

LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    filename=str(LOG_FILE),
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    encoding="utf-8",
)

logger = logging.getLogger("hybrid_retrieval")

DEFAULT_RRF_K = 60


def reciprocal_rank_fusion(
    lexical_results,
    semantic_results,
    limit=8,
    rrf_k=DEFAULT_RRF_K,
):
    """
    Fuse lexical and semantic rankings.

    RRF score = sum(1 / (k + rank))
    Rank starts at 1.
    """

    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise ValueError("Limit harus berupa integer positif.")

    if not isinstance(rrf_k, int) or rrf_k < 1:
        raise ValueError("rrf_k harus berupa integer positif.")

    fused = {}

    def add_result(item_id, rank, source, record):
        if item_id not in fused:
            fused[item_id] = {
                "id": item_id,
                "rrf_score": 0.0,
                "lexical_rank": None,
                "semantic_rank": None,
                "lexical_score": None,
                "semantic_score": None,
                "category": record.get("category"),
                "content": record.get("content"),
                "status": record.get("status"),
                "source": record.get("source"),
                "created_at": record.get("created_at"),
            }

        entry = fused[item_id]
        entry["rrf_score"] += 1.0 / (rrf_k + rank)

        # Fill metadata from the available source.
        for field in (
            "category", "content", "status", "source", "created_at"
        ):
            if entry[field] is None and record.get(field) is not None:
                entry[field] = record[field]

        entry[f"{source}_rank"] = rank

        if source == "lexical":
            entry["lexical_score"] = record.get("score")

        if source == "semantic":
            entry["semantic_score"] = record.get("score")

    for rank, item in enumerate(lexical_results, 1):
        add_result(
            item["id"],
            rank,
            "lexical",
            item,
        )

    for rank, item in enumerate(semantic_results, 1):
        add_result(
            item["id"],
            rank,
            "semantic",
            item,
        )

    results = list(fused.values())

    results.sort(
        key=lambda item: (-item["rrf_score"], item["id"])
    )

    for item in results:
        item["rrf_score"] = round(item["rrf_score"], 8)

    return results[:limit]


def hybrid_search(
    query,
    limit=8,
    lexical_limit=None,
    semantic_limit=None,
    rrf_k=DEFAULT_RRF_K,
):
    """
    Execute both retrieval engines and fuse their rankings.

    Semantic integrity errors are propagated.
    The function never silently labels lexical-only results as hybrid.
    """

    if not isinstance(query, str) or not query.strip():
        raise ValueError("Query tidak boleh kosong.")

    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise ValueError("Limit harus berupa integer positif.")

    lexical_limit = lexical_limit or limit
    semantic_limit = semantic_limit or limit

    # Read metadata from the existing knowledge database.
    metadata = {
        row[0]: {
            "id": row[0],
            "category": row[1],
            "content": row[2],
            "status": row[3],
            "source": row[4],
            "created_at": row[5],
        }
        for row in list_all()
    }

    # Existing lexical retrieval.
    lexical_raw = retrieve(
        query,
        limit=lexical_limit,
    )

    lexical_results = []

    for score, item_id, category, content, status in lexical_raw:
        record = metadata.get(item_id, {})

        lexical_results.append({
            "id": item_id,
            "category": category,
            "content": content,
            "status": status,
            "source": record.get("source"),
            "created_at": record.get("created_at"),
            "score": score,
        })

    # Semantic retrieval. Integrity errors must not be hidden.
    semantic_raw = semantic_search(
        query,
        limit=semantic_limit,
    )

    fused = reciprocal_rank_fusion(
        lexical_results,
        semantic_raw,
        limit=limit,
        rrf_k=rrf_k,
    )

    result = {
        "query": query,
        "method": "reciprocal_rank_fusion",
        "rrf_k": rrf_k,
        "lexical_count": len(lexical_results),
        "semantic_count": len(semantic_raw),
        "result_count": len(fused),
        "results": fused,
    }

    logger.info(
        "Hybrid search completed: lexical=%s semantic=%s fused=%s",
        len(lexical_results),
        len(semantic_raw),
        len(fused),
    )

    return result
