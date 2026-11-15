"""
Claud AI - Source Intelligence
Version: 0.6.1
"""

import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONFIG_FILE = ROOT / "config" / "settings.json"
DB_FILE = ROOT / "database" / "claud_memory.db"


def load_config():
    with open(CONFIG_FILE, "r", encoding="utf-8-sig") as file:
        return json.load(file)


def database_counts():
    result = {}

    with sqlite3.connect(DB_FILE) as db:
        for table in ("knowledge", "memories", "evaluations"):
            try:
                result[table] = db.execute(
                    f"SELECT COUNT(*) FROM {table}"
                ).fetchone()[0]
            except sqlite3.Error:
                result[table] = None

    return result


def get_sources_report():
    counts = database_counts()

    return {
        "knowledge_database": counts["knowledge"],
        "stored_memories": counts["memories"],
        "evaluations": counts["evaluations"],
        "configuration": str(CONFIG_FILE),
        "database": str(DB_FILE),
    }


def get_runtime_config():
    settings = load_config()

    return {
        "version": settings.get("version"),
        "model": settings.get("local_model"),
        "ollama_url": settings.get("ollama_url"),
        "reasoning_enabled": settings.get("reasoning_enabled"),
        "critique_enabled": settings.get("critique_enabled"),
        "grounding_enabled": settings.get("grounding_enabled"),
    }


def get_grounding_report():
    from knowledge.grounding import inspect_grounding_status
    return inspect_grounding_status()


def get_evaluation_report():
    """
    Read evaluation statistics directly from SQLite.
    These counts describe recorded process outcomes, not factual accuracy.
    """
    report = {
        "total": 0,
        "statuses": {},
        "average_duration": None,
        "accuracy_available": False,
        "quality_score_available": False,
    }

    with sqlite3.connect(DB_FILE) as db:
        report["total"] = db.execute(
            "SELECT COUNT(*) FROM evaluations"
        ).fetchone()[0]

        rows = db.execute(
            """
            SELECT critique_status, COUNT(*)
            FROM evaluations
            GROUP BY critique_status
            ORDER BY critique_status
            """
        ).fetchall()

        report["statuses"] = {
            status or "unknown": count for status, count in rows
        }

        duration = db.execute(
            """
            SELECT AVG(duration_seconds)
            FROM evaluations
            WHERE duration_seconds IS NOT NULL
            """
        ).fetchone()[0]

        report["average_duration"] = duration

    return report


def format_evaluation_report():
    report = get_evaluation_report()

    lines = [
        "CLAUD AI — EVALUATION REPORT",
        f"Total recorded evaluations: {report['total']}",
        "",
        "Recorded process statuses:",
    ]

    if report["statuses"]:
        for status, count in report["statuses"].items():
            lines.append(f"- {status}: {count}")
    else:
        lines.append("- No evaluation records.")

    lines.append("")

    average = report["average_duration"]
    if average is not None:
        lines.append(
            f"Average recorded processing duration: {average:.2f} seconds"
        )
    else:
        lines.append("Average processing duration: unavailable")

    lines.extend([
        "",
        "ACCURACY: Not measured.",
        "QUALITY SCORE: Not measured.",
        "",
        "Important interpretation:",
        "- 'completed' means the self-critique process completed.",
        "- It does not prove that the answer is factually correct.",
        "- No independent fact-validation metric is currently recorded.",
        "- These statistics describe database records, not overall intelligence.",
    ])

    return "\n".join(lines)


def classify_query(query):
    normalized = query.lower()

    evaluation_terms = (
        "evaluasi claud ai",
        "evaluasi sistem claud",
        "evaluasi kinerja claud",
        "evaluasi performa claud",
        "hasil evaluasi claud",
        "seberapa akurat claud",
        "akurasi claud ai",
        "kualitas jawaban claud",
        "kinerja sistem claud",
        "laporan evaluasi claud",
    )

    if any(term in normalized for term in evaluation_terms):
        return "evaluation"

    claud_evaluation_indicators = (
        "akurasi", "akurat", "kualitas jawaban",
        "kinerja", "performa", "skor", "persentase",
    )
    if (
        "claud" in normalized
        and any(term in normalized for term in claud_evaluation_indicators)
    ):
        return "evaluation"

    database_terms = (
        "berdasarkan knowledge database",
        "berdasarkan database",
        "knowledge database",
        "knowledge id",
        "apa fakta",
        "fakta yang tersimpan",
        "apa asumsi",
        "asumsi yang tersimpan",
        "apa inferensi",
        "inferensi yang tersimpan",
        "apa opini",
        "opini yang tersimpan",
        "pertanyaan terbuka",
        "open question",
        "semantic memory retrieval",
        "semantic retrieval",
        "record knowledge",
        "semua knowledge",
        "seluruh knowledge",
        "semua record",
        "seluruh record",
    )
    if any(term in normalized for term in database_terms):
        return "database"

    configuration_terms = (
        "konfigurasi claud",
        "model apa yang digunakan",
        "model apa yang sedang",
        "ollama url",
        "versi claud ai",
        "status grounding",
    )
    if any(term in normalized for term in configuration_terms):
        return "configuration"

    analytical_terms = (
        "analisis",
        "bandingkan",
        "hubungan antara",
        "evaluasi",
        "mengapa",
        "bagaimana",
        "jelaskan perbedaan",
    )
    if any(term in normalized for term in analytical_terms):
        return "analytical"

    return "general"

def database_only_answer(query):
    """
    Contextual retrieval from SQLite.
    Preserves original category, status, source and content.
    Never delegates a database-intent query to the LLM.
    """
    import re

    normalized = query.lower()

    category_map = {
        "fact": ("fakta", "fact"),
        "assumption": ("asumsi", "assumption"),
        "inference": ("inferensi", "inference"),
        "opinion": ("opini", "pendapat", "opinion"),
        "open_question": (
            "pertanyaan terbuka",
            "open question",
            "pertanyaan yang belum terjawab",
        ),
    }

    selected_category = None
    for category, terms in category_map.items():
        if any(term in normalized for term in terms):
            selected_category = category
            break

    all_records_triggers = (
        "semua knowledge",
        "seluruh knowledge",
        "semua record",
        "seluruh record",
        "semua data knowledge",
        "tampilkan semua knowledge",
        "apa saja knowledge",
        "apa saja yang tersimpan",
        "seluruh isi knowledge database",
        "tersimpan di knowledge database",
        "tersimpan dalam knowledge database",
        "tersimpan di database",
        "tersimpan dalam database",
        "benar-benar tersimpan",
    )
    all_records = any(
        term in normalized for term in all_records_triggers
    )

    stopwords = {
        "apa", "apakah", "yang", "dan", "atau", "dari", "dengan",
        "untuk", "pada", "dalam", "oleh", "sebagai", "adalah",
        "itu", "ini", "bisa", "dapat", "sudah", "belum", "akan",
        "saat", "ketika", "secara", "lebih", "masih", "saya",
        "kita", "mereka", "claud", "claudai", "ai",
        "berdasarkan", "database", "knowledge", "sebutkan",
        "jelaskan", "status", "jangan", "menggunakan",
        "jawaban", "assistant", "sebelumnya", "bukti",
        "tolong", "tentang", "terkait", "mengenai",
        "model", "bahasa",
    }

    tokens = set(
        re.findall(r"[a-z0-9]+(?:[._-][a-z0-9]+)*", normalized)
    )
    tokens.difference_update(stopwords)

    # Concept aliases bridge natural language and stored wording.
    topic_terms = set(tokens)

    if (
        "model" in normalized
        or "bahasa" in normalized
        or "llm" in tokens
        or "ollama" in tokens
        or "llama" in tokens
    ):
        topic_terms.update({
            "model", "language", "ollama", "llama3.2"
        })

    if (
        "semantic" in normalized
        or "retrieval" in normalized
        or "pengambilan semantik" in normalized
    ):
        topic_terms.update({
            "semantic", "memory", "retrieval"
        })

    with sqlite3.connect(DB_FILE) as db:
        rows = db.execute(
            """
            SELECT id, category, content, status, source, created_at
            FROM knowledge
            ORDER BY id ASC
            """
        ).fetchall()

    if not rows:
        return "Knowledge database belum memiliki record."

    if selected_category:
        candidates = [
            row for row in rows if row[1] == selected_category
        ]
        category_words = {
            term
            for terms in category_map.values()
            for term in terms
        }
        category_words.update({
            "tersimpan", "disimpan",
            "asumsinya", "inferensinya", "faktanya",
            "opininya", "pendapatnya",
            "pertanyaannya",
        })

        query_instruction_words = {
            "apa", "jelaskan", "sebutkan", "tampilkan",
            "kategori", "status", "isi", "record",
            "knowledge", "database", "claud", "claudai",
            "ai", "yang", "dalam", "untuk", "berdasarkan",
            "terkait", "mengenai", "tentang",
        }

        specific_topic_terms = (
            topic_terms - category_words - query_instruction_words
        )

        if specific_topic_terms:
            candidates = [
                row for row in candidates
                if specific_topic_terms.intersection(
                    set(re.findall(
                        r"[a-z0-9]+(?:[._-][a-z0-9]+)*",
                        row[2].lower()
                    ))
                )
            ]
        selected = candidates

    elif all_records:
        selected = rows

    else:
        scored = []
        for row in rows:
            content_tokens = set(re.findall(
                r"[a-z0-9]+(?:[._-][a-z0-9]+)*",
                row[2].lower()
            ))
            score = len(topic_terms.intersection(content_tokens))
            if score >= 2:
                scored.append((score, row))

        scored.sort(key=lambda item: (-item[0], item[1][0]))
        selected = [row for score, row in scored]

    if not selected:
        return (
            "Tidak ditemukan record yang relevan dalam knowledge "
            "database. Sistem tidak akan mengarang jawaban atau "
            "menganggap informasi yang tidak tersimpan sebagai fakta."
        )

    if selected_category:
        title = (
            "RECORD BERDASARKAN KATEGORI: "
            + selected_category.upper()
        )
    elif all_records:
        title = "SELURUH RECORD KNOWLEDGE DATABASE"
    else:
        title = "RECORD RELEVAN DALAM KNOWLEDGE DATABASE"

    lines = [
        title,
        "Data diambil langsung dari SQLite.",
        "Isi, kategori, dan status ditampilkan sesuai record asli.",
    ]

    for item_id, category, content, status, source, created_at in selected:
        lines.extend([
            "",
            f"Knowledge ID: {item_id}",
            f"Category: {category}",
            f"Status: {status}",
            f"Source: {source or 'Not specified'}",
            f"Created at: {created_at}",
            f"Content: {content}",
        ])

        if status == "verified":
            lines.append(
                "Verification note: Status verified adalah status "
                "yang tercatat di database, bukan verifikasi independen."
            )

        if category == "open_question":
            lines.append(
                "Interpretation: Ini adalah pertanyaan terbuka yang "
                "belum terselesaikan, bukan bukti bahwa fitur tersebut "
                "sudah tersedia."
            )
        elif category == "assumption":
            lines.append(
                "Interpretation: Ini adalah asumsi, bukan fakta "
                "yang telah dibuktikan."
            )
        elif category == "inference":
            lines.append(
                "Interpretation: Ini adalah inferensi, bukan bukti "
                "independen."
            )
        elif category == "opinion":
            lines.append(
                "Interpretation: Ini adalah opini atau penilaian "
                "subjektif."
            )

    lines.extend([
        "",
        f"Jumlah record yang cocok: {len(selected)}",
    ])

    return "\n".join(lines)
