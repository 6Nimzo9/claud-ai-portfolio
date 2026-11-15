import re


class IntentAnalyzer:
    VERSION = "0.4.0"

    RULES = {
        "troubleshooting": [
            "error", "gagal", "bug", "debug", "perbaiki",
            "masalah", "tidak berfungsi", "kenapa tidak",
            "troubleshoot", "periksa kesalahan", "cari penyebab"
        ],
        "continuation": [
            "lanjutkan", "teruskan", "kita sebelumnya",
            "proyek kita", "pembahasan sebelumnya",
            "percakapan sebelumnya", "yang tadi",
            "yang kemarin", "lanjut dari"
        ],
        "planning": [
            "rencanakan", "buat rencana", "buatkan rencana",
            "susun rencana", "susun roadmap", "buat roadmap",
            "rencana implementasi", "rencana pengembangan",
            "tahapan pengembangan", "strategi implementasi",
            "prioritaskan", "jadwalkan"
        ],
        "creation": [
            "buatkan", "buat", "bangun", "kembangkan",
            "rancang", "tulis kode", "implementasikan",
            "desain", "hasilkan", "kembangkan sistem"
        ],
        "action": [
            "jalankan", "hapus", "kirim", "ubah",
            "simpan", "aktifkan", "nonaktifkan",
            "deploy", "eksekusi", "install", "restart",
            "pindahkan", "unggah", "unduh"
        ],
        "analysis": [
            "analisis", "analisa", "evaluasi", "bandingkan",
            "kelemahan", "kelebihan", "risiko", "dampak",
            "mengapa", "seberapa baik", "menurut analisa",
            "investigasi", "audit", "tinjau", "review"
        ],
        "question": [
            "apa itu", "bagaimana", "siapa", "kapan",
            "dimana", "di mana", "berapa", "apakah",
            "bisakah", "mengapa", "kenapa"
        ],
    }

    PRIORITY = [
        "troubleshooting",
        "continuation",
        "planning",
        "creation",
        "action",
        "analysis",
        "question",
    ]

    CONVERSATION_PATTERNS = [
        r"^(hai|halo|hello|hi)(\s+claud)?[!?. ]*$",
        r"^(terima kasih|makasih|thanks|thank you)(\s*[,!]?\s*claud)?[!?. ]*$",
        r"^(oke|ok|baik|siap)(\s+claud)?[!?. ]*$",
    ]

    HYPOTHETICAL_PATTERNS = [
        r"\b(?:jika|seandainya|andaikan|misalnya|umpamanya|anggap saja|what if|suppose)\b"
    ]

    CAUSAL_PATTERNS = [
        r"\b(?:apa dampaknya|apa akibatnya|apa risikonya|apa yang terjadi|mengapa hal itu terjadi)\b",
        r"\b(?:dampak|akibat|konsekuensi)\b.{0,50}\b(?:jika|apabila|bila|ketika)\b",
        r"\b(?:jika|apabila|bila|ketika)\b.{0,100}\b(?:dampak|akibat|konsekuensi|terjadi)\b"
    ]

    INFORMATIONAL_PATTERNS = [
        r"\b(?:apa itu|definisi|pengertian|jelaskan|terangkan)\b"
    ]

    ANALYTICAL_PATTERNS = [
        r"\b(?:analisis|analisa|evaluasi|bandingkan|investigasi|audit|tinjau|review)\b",
        r"\b(?:mengapa|kenapa|bagaimana jika|apa dampaknya|apa akibatnya|apa risikonya)\b"
    ]

    NEGATED_ACTION_PATTERNS = [
        r"\b(?:jangan|tidak|tak|dilarang|hindari)\s+"
        r"(?:pernah\s+)?(?:langsung\s+)?"
        r"(?:jalankan|hapus|kirim|ubah|simpan|aktifkan|nonaktifkan|"
        r"deploy|eksekusi|install|restart|pindahkan|unggah|unduh)\b"
    ]

    def _matches(self, text, patterns):
        return [
            pattern for pattern in patterns
            if re.search(pattern, text, re.IGNORECASE)
        ]

    def _keyword_scores(self, text):
        scores = {}

        for intent, keywords in self.RULES.items():
            matched = []

            for keyword in keywords:
                pattern = (
                    r"(?<!\w)" +
                    re.escape(keyword) +
                    r"(?!\w)"
                )

                if re.search(pattern, text):
                    matched.append(keyword)

            if matched:
                scores[intent] = {
                    "score": len(matched),
                    "matched_keywords": matched,
                }

        return scores

    def analyze(self, text):
        if not isinstance(text, str) or not text.strip():
            return {
                "intent": "unknown",
                "confidence": 0.0,
                "reason": "Empty or invalid input",
                "scores": {},
                "version": self.VERSION,
            }

        normalized = re.sub(r"\s+", " ", text.lower()).strip()

        for pattern in self.CONVERSATION_PATTERNS:
            if re.match(pattern, normalized):
                return {
                    "intent": "conversation",
                    "confidence": 0.95,
                    "reason": "Matched conversation pattern",
                    "scores": {},
                    "version": self.VERSION,
                }

        scores = self._keyword_scores(normalized)

        hypothetical = bool(
            self._matches(normalized, self.HYPOTHETICAL_PATTERNS)
        )
        causal = bool(
            self._matches(normalized, self.CAUSAL_PATTERNS)
        )
        informational = bool(
            self._matches(normalized, self.INFORMATIONAL_PATTERNS)
        )
        analytical = bool(
            self._matches(normalized, self.ANALYTICAL_PATTERNS)
        )
        negated_action = bool(
            self._matches(normalized, self.NEGATED_ACTION_PATTERNS)
        )

        # Hypothetical cause-and-effect questions receive analytical priority.
        if hypothetical and (causal or informational or analytical):
            return {
                "intent": "analysis",
                "confidence": 0.85,
                "reason": "Hypothetical causal or analytical question",
                "scores": scores,
                "version": self.VERSION,
            }

        # Negated commands are not classified as action requests.
        if negated_action:
            if analytical or causal:
                selected = "analysis"
            else:
                selected = "question" if "?" in normalized else "unknown"

            return {
                "intent": selected,
                "confidence": 0.80 if selected != "unknown" else 0.60,
                "reason": "Negated action detected; action intent suppressed",
                "scores": scores,
                "version": self.VERSION,
            }

        # Explicit analytical patterns take precedence over generic keywords.
        if analytical or causal:
            return {
                "intent": "analysis",
                "confidence": 0.85,
                "reason": "Matched analytical or causal question pattern",
                "scores": scores,
                "version": self.VERSION,
            }

        # Explicit informational questions.
        if informational:
            return {
                "intent": "question",
                "confidence": 0.85,
                "reason": "Matched informational question pattern",
                "scores": scores,
                "version": self.VERSION,
            }

        if not scores:
            return {
                "intent": "unknown",
                "confidence": 0.0,
                "reason": "No classification rule matched",
                "scores": {},
                "version": self.VERSION,
            }

        highest_score = max(
            item["score"] for item in scores.values()
        )

        candidates = [
            intent
            for intent, item in scores.items()
            if item["score"] == highest_score
        ]

        best_intent = next(
            intent for intent in self.PRIORITY
            if intent in candidates
        )

        is_ambiguous = len(candidates) > 1

        if is_ambiguous:
            confidence = 0.55
        elif highest_score == 1:
            confidence = 0.65
        else:
            confidence = min(
                0.90,
                0.65 + (highest_score - 1) * 0.10
            )

        return {
            "intent": best_intent,
            "confidence": round(confidence, 2),
            "reason": (
                "Ambiguous classification resolved by priority"
                if is_ambiguous
                else "Matched keyword rules"
            ),
            "scores": scores,
            "version": self.VERSION,
        }
