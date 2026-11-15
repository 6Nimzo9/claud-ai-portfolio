"""
Claud AI - Knowledge Grounding Engine
Version: 0.6.0

Provides deterministic evidence packaging and provenance rules.
It does not independently verify the truth of stored information.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "core"))

from knowledge.manager import retrieve, list_all


GROUNDING_RULES = """KNOWLEDGE GROUNDING PROTOCOL

You must distinguish the origin, category, and certainty of information.

INFORMATION CLASSES:

1. FACT
   A stored fact is a record classified as fact in the database.
   It represents recorded information, not automatically objective truth.
   A status of "verified" means the user marked it verified.
   It does NOT mean independent verification was performed.

2. ASSUMPTION
   An assumption is an unestablished proposition.
   Never present it as an established fact or proof.

3. INFERENCE
   An inference is a recorded conclusion or interpretation.
   It is not independent evidence that the conclusion is true.

4. OPINION
   An opinion is subjective.
   Do not present it as objective or independently established fact.

5. OPEN QUESTION
   An open question is unresolved.
   It is NOT evidence that a capability, feature, process, or solution
   already exists or has been implemented.

6. MODEL KNOWLEDGE
   Information from pretrained model knowledge must not be attributed
   to the local knowledge database.

7. UNKNOWN
   When evidence is insufficient, explicitly state the limitation.
   Do not invent missing details.

STRICT EVIDENCE RULES:

- Never invent Knowledge IDs, sources, statuses, or quotations.
- Only refer to Knowledge IDs actually included in the retrieved evidence.
- Preserve the exact meaning and category of every stored record.
- Never use assumption, inference, opinion, or open_question records
  as proof of factual claims.
- A fact record may support a statement about what is recorded,
  but its status and source must remain clear.
- Never interpret user-marked "verified" as independently verified.
- An open_question must never be used to claim that its subject
  has already been implemented or resolved.
- If records conflict, disclose the conflict.
- If no relevant records are retrieved, say so when discussing
  what is stored in the knowledge database.
- You may provide general analysis without database evidence,
  but clearly distinguish analysis from stored knowledge.
- Do not claim that a database record supports a statement unless
  its actual content supports that statement.
- Answer the user's actual question.
- Never expose private internal chain-of-thought."""


def build_evidence_context(query, limit=8):
    """
    Retrieve lexical matches and separate them by evidential role.
    Stored records are not independently verified by this function.
    """
    records = retrieve(query, limit=limit)

    if not records:
        return (
            "KNOWLEDGE DATABASE EVIDENCE:\n"
            "No matching knowledge records were retrieved for this query.\n"
            "Do not claim that the database contains supporting records."
        )

    metadata = {
        row[0]: row
        for row in list_all()
    }

    sections = {
        "fact": [],
        "assumption": [],
        "inference": [],
        "opinion": [],
        "open_question": [],
        "other": [],
    }

    role_labels = {
        "fact": (
            "STORED FACT RECORDS - recorded information, "
            "not independently verified."
        ),
        "assumption": (
            "ASSUMPTIONS - unestablished propositions; "
            "not factual evidence."
        ),
        "inference": (
            "STORED INFERENCES - interpretations, "
            "not independent proof."
        ),
        "opinion": (
            "OPINIONS - subjective records, not objective proof."
        ),
        "open_question": (
            "OPEN QUESTIONS - unresolved; never evidence "
            "that a capability or feature already exists."
        ),
        "other": (
            "OTHER RECORD CATEGORIES - preserve their original meaning "
            "and do not assume they are factual evidence."
        ),
    }

    for score, item_id, category, content, status in records:
        normalized_category = str(category).strip().lower()
        section = normalized_category if normalized_category in sections else "other"

        full_record = metadata.get(item_id)
        source = full_record[4] if full_record and len(full_record) > 4 else None
        created_at = full_record[5] if full_record and len(full_record) > 5 else None

        record_text = (
            f"[Knowledge ID: {item_id} | Category: {category} | "
            f"Status: {status} | Relevance: {score}]\n"
            f"Source: {source or 'Not specified'}\n"
            f"Recorded at: {created_at or 'Not specified'}\n"
            f"Content: {content}"
        )
        sections[section].append(record_text)

    lines = [
        "KNOWLEDGE DATABASE EVIDENCE:",
        "Retrieved records from the local SQLite database follow.",
        "These records establish what is stored, not automatic truth."
    ]

    for category in (
        "fact", "assumption", "inference",
        "opinion", "open_question", "other"
    ):
        items = sections[category]
        if not items:
            continue

        lines.append(role_labels[category])
        lines.extend(items)

    lines.append(
        "EVIDENCE INTEGRITY REQUIREMENT: Use each record only according "
        "to its category, status, source, and exact content. "
        "Do not turn unresolved questions, assumptions, opinions, "
        "or inferences into established facts. "
        "Cite only Knowledge IDs present above."
    )

    return "\n\n".join(lines)


def validate_knowledge_references(answer, evidence_context):
    """Validate explicit Knowledge ID citations against active evidence."""
    import re

    citation_pattern = re.compile(
        r"\bKnowledge\s+ID\s*[:#]?\s*(\d+)\b",
        re.IGNORECASE
    )
    current_pattern = re.compile(
        r"\[Knowledge ID:\s*(\d+)\s*\|",
        re.IGNORECASE
    )
    legacy_pattern = re.compile(
        r"\[[^|\]]+\s*\|\s*[^|\]]+\s*\|\s*ID\s*(\d+)\s*\]",
        re.IGNORECASE
    )

    cited_ids = {
        int(value) for value in citation_pattern.findall(answer or "")
    }

    evidence = evidence_context or ""
    allowed_ids = {
        int(value) for value in current_pattern.findall(evidence)
    }
    allowed_ids.update(
        int(value) for value in legacy_pattern.findall(evidence)
    )

    invalid_ids = sorted(cited_ids - allowed_ids)

    return {
        "valid": not invalid_ids,
        "cited_ids": sorted(cited_ids),
        "allowed_ids": sorted(allowed_ids),
        "invalid_ids": invalid_ids,
    }

def build_grounded_prompt(query, existing_system_prompt):
    evidence = build_evidence_context(query)

    prompt = (
        existing_system_prompt.strip()
        + "\n\n"
        + GROUNDING_RULES
        + "\n\n"
        + evidence
    )

    return prompt, evidence


def inspect_grounding_status():
    return {
        "engine": "Knowledge Grounding Engine",
        "version": "0.6.0",
        "retrieval": "keyword-based",
        "provenance": "enabled",
        "independent_fact_verification": False
    }
