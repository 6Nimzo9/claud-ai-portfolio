import re


class ComplexityAssessor:
    VERSION = "0.2.1"

    DEEP_PATTERNS = {
        "multi_step": [
            "tahap demi tahap",
            "langkah demi langkah",
            "secara menyeluruh",
            "end-to-end",
            "dari awal sampai akhir",
            "analisis secara mendalam",
            "evaluasi secara menyeluruh",
        ],
        "strategic_analysis": [
            "analisis mendalam",
            "evaluasi menyeluruh",
            "strategi jangka panjang",
            "arsitektur",
            "optimasi",
            "optimalisasi",
            "analisis komprehensif",
            "evaluasi strategis",
        ],
        "comparison": [
            "bandingkan",
            "perbandingan",
            "alternatif",
            "trade-off",
            "kelebihan dan kekurangan",
            "analisis komparatif",
        ],
        "planning": [
            "roadmap",
            "rencana implementasi",
            "rencana pengembangan",
            "perencanaan",
            "strategi implementasi",
            "rencana jangka panjang",
            "tahapan pengembangan",
            "rencana migrasi",
        ],
        "problem_solving": [
            "pecahkan masalah",
            "root cause",
            "akar masalah",
            "debugging kompleks",
            "investigasi",
            "analisis akar masalah",
            "identifikasi penyebab utama",
        ],
    }

    CONTEXT_PATTERNS = {
        "context_dependency": [
            "sebelumnya",
            "yang tadi",
            "yang kemarin",
            "proyek kita",
            "arsitektur kita",
            "berdasarkan percakapan",
            "lanjutkan",
            "teruskan",
            "pembahasan sebelumnya",
        ],
        "knowledge_dependency": [
            "berdasarkan data",
            "berdasarkan dokumen",
            "berdasarkan sumber",
            "bukti",
            "referensi",
            "riwayat",
            "berdasarkan hasil sebelumnya",
        ],
    }

    def _matches(self, text, patterns):
        matched = []

        for phrase in patterns:
            pattern = (
                r"(?<!\w)"
                + re.escape(phrase)
                + r"(?!\w)"
            )

            if re.search(pattern, text):
                matched.append(phrase)

        return matched

    def assess(self, text, intent=None, context_available=False):
        if not isinstance(text, str) or not text.strip():
            return {
                "complexity": "unknown",
                "score": 0,
                "reasoning": ["Empty or invalid input"],
                "signals": {},
            }

        normalized = re.sub(
            r"\s+", " ", text.lower()
        ).strip()

        score = 0
        reasoning = []
        signals = {}

        # 1. Intent contribution
        intent_weights = {
            "analysis": 15,
            "planning": 15,
            "creation": 5,
            "troubleshooting": 5,
            "continuation": 10,
        }

        intent_score = intent_weights.get(intent, 0)
        score += intent_score

        if intent_score:
            reasoning.append(
                f"Intent contribution: {intent} (+{intent_score})"
            )

        signals["intent"] = {
            "type": intent or "unknown",
            "score": intent_score,
        }

        # 2. Deep cognitive indicators
        deep_matches = {}

        for category, patterns in self.DEEP_PATTERNS.items():
            matched = self._matches(normalized, patterns)

            if matched:
                deep_matches[category] = matched
                category_score = 20
                score += category_score

                reasoning.append(
                    f"Deep cognitive indicator: {category} "
                    f"(+{category_score})"
                )

        signals["deep_cognitive"] = deep_matches

        # 3. Context dependency
        context_matches = {}

        for category, patterns in self.CONTEXT_PATTERNS.items():
            matched = self._matches(normalized, patterns)

            if matched:
                context_matches[category] = matched
                score += 15

                reasoning.append(
                    f"Context dependency detected: {category} (+15)"
                )

        signals["contextual"] = context_matches

        # 4. Task decomposition
        conjunctions = len(
            re.findall(
                r"\b(dan|serta|kemudian|lalu)\b",
                normalized,
            )
        )

        numbered_steps = len(
            re.findall(r"(?<!\w)\d+[.)]", normalized)
        )

        subtask_score = min(
            20,
            conjunctions * 5 + numbered_steps * 5,
        )

        if subtask_score:
            score += subtask_score
            reasoning.append(
                f"Task decomposition indicators (+{subtask_score})"
            )

        signals["task_indicators"] = {
            "conjunctions": conjunctions,
            "numbered_steps": numbered_steps,
        }

        # Context availability alone does not increase complexity.
        signals["context_available"] = bool(context_available)

        # 5. Normalize score
        score = max(0, min(100, score))

        # 6. Select cognitive level
        if score >= 35:
            complexity = "deep_cognitive"
        elif score >= 15:
            complexity = "contextual"
        else:
            complexity = "direct"

        if not reasoning:
            reasoning.append(
                "No significant complexity indicators detected"
            )

        return {
            "complexity": complexity,
            "score": score,
            "reasoning": reasoning,
            "signals": signals,
        }
