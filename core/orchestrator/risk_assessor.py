import re


class RiskAssessor:
    VERSION = "0.4.1"

    CRITICAL_PATTERNS = {
        "production_data": [
            # Active voice
            r"\b(?:hapus|menghapus|delete|drop|truncate)\b.{0,60}\b(?:database|basis data|data)\b.{0,40}\b(?:produksi|production|prod)\b",

            # Passive voice and reversed word order
            r"\b(?:database|basis data|data)\b.{0,40}\b(?:produksi|production|prod)\b.{0,40}\b(?:dihapus|terhapus|dihilangkan|di-delete|deleted|dropped|truncated)\b",

            # Nominalization
            r"\b(?:penghapusan|pemusnahan|penghilangan)\b.{0,40}\b(?:database|basis data|data)\b.{0,40}\b(?:produksi|production|prod)\b",

            # Production data deletion in hypothetical or future contexts
            r"\b(?:database|basis data|data)\b.{0,40}\b(?:produksi|production|prod)\b.{0,40}\b(?:akan dihapus|akan dihilangkan|dapat dihapus|bisa dihapus)\b",

            # Destruction of storage and backups
            r"\b(?:format|wipe|destroy)\b.{0,40}\b(?:drive|disk|server)\b",
            r"\b(?:hapus|menghapus|delete)\b.{0,40}\b(?:semua|seluruh)\b.{0,30}\b(?:backup|cadangan)\b"
        ],
        "security": [
            r"\b(?:bypass authentication|bypass security|unauthorized access)\b",
            r"\b(?:curi|mencuri|steal)\b.{0,30}\b(?:password|kredensial|credentials)\b",
            r"\b(?:ransomware|menghilangkan jejak akses)\b"
        ],
        "system_integrity": [
            r"\b(?:disable all security|nonaktifkan seluruh proteksi)\b"
        ]
    }

    HIGH_PATTERNS = {
        "data_modification": [
            r"\b(?:hapus|menghapus|penghapusan|delete|drop|truncate)\b.{0,40}\b(?:database|data|table|backup)\b",
            r"\b(?:overwrite|reset)\b.{0,30}\b(?:database|data)\b"
        ],
        "system_modification": [
            r"\b(?:ubah|mengubah|modifikasi)\b.{0,30}\b(?:konfigurasi sistem|system configuration|core|main\.py|security policy)\b",
            r"\b(?:deploy ke production|restart production server|nonaktifkan firewall)\b"
        ],
        "external_action": [
            r"\b(?:kirim|mengirim|send)\b.{0,30}\b(?:email|pesan|message)\b",
            r"\b(?:publish|posting|transfer uang|lakukan pembayaran|submit application|submit lamaran|hapus akun)\b"
        ],
        "sensitive_access": [
            r"\b(?:akses|ambil|export|buka)\b.{0,30}\b(?:password|credential|data pribadi|personal data|token|api key)\b"
        ]
    }

    MEDIUM_PATTERNS = {
        "operational": [
            r"\b(?:install|uninstall|update package|restart service)\b",
            r"\b(?:ubah|mengubah)\b.{0,30}\b(?:environment variable|permission|registry|file konfigurasi)\b"
        ],
        "financial": [
            r"\b(?:investasi|keputusan finansial|pinjaman|transaksi|pembelian|penjualan)\b"
        ],
        "data_access": [
            r"\b(?:baca|membaca|export|ambil|akses)\b.{0,30}\b(?:database|file sensitif|data)\b"
        ]
    }

    INFORMATIONAL_PATTERNS = [
        r"\b(?:jelaskan|terangkan|mengapa|kenapa|bagaimana|analisis|evaluasi|risiko|bahaya|dampak|pelajari)\b",
        r"\b(?:apa yang terjadi|apa akibatnya|apa dampaknya|apa risikonya)\b"
    ]

    ACTION_PATTERNS = [
        r"\b(?:lakukan|jalankan|eksekusi|hapus|menghapus|delete|kirim|mengirim|ubah|mengubah|deploy|aktifkan|nonaktifkan|transfer|submit|buat|backup|cadangkan|install|uninstall|restart|publish|posting)\b"
    ]

    NEGATION_PATTERNS = [
        r"\b(?:jangan|tidak|tak|bukan|dilarang|hindari|batalkan|urungkan)\b"
    ]

    HYPOTHETICAL_PATTERNS = [
        r"\b(?:jika|seandainya|andaikan|misalnya|umpamanya|anggap saja|what if|suppose)\b"
    ]

    def _matches(self, text, patterns):
        return [
            pattern for pattern in patterns
            if re.search(pattern, text, re.IGNORECASE)
        ]

    def _detect_hazard(self, text):
        rule_groups = {
            "critical": self.CRITICAL_PATTERNS,
            "high": self.HIGH_PATTERNS,
            "medium": self.MEDIUM_PATTERNS
        }

        signals = {
            "critical": {},
            "high": {},
            "medium": {}
        }
        reasoning = []

        for level, categories in rule_groups.items():
            for category, patterns in categories.items():
                matched = self._matches(text, patterns)
                if matched:
                    signals[level][category] = matched
                    reasoning.append(
                        f"{level.upper()} hazard detected: {category}"
                    )

        if signals["critical"]:
            return "critical", 95, signals, reasoning
        if signals["high"]:
            return "high", 75, signals, reasoning
        if signals["medium"]:
            return "medium", 45, signals, reasoning

        return "low", 10, signals, reasoning

    def _classify_actionability(self, text, intent):
        informational = bool(
            self._matches(text, self.INFORMATIONAL_PATTERNS)
        )
        hypothetical = bool(
            self._matches(text, self.HYPOTHETICAL_PATTERNS)
        )
        negated = bool(
            self._matches(text, self.NEGATION_PATTERNS)
        )
        action_matches = self._matches(text, self.ACTION_PATTERNS)

        if informational:
            actionability = "informational"
        elif hypothetical:
            actionability = "analytical"
        elif negated:
            actionability = "analytical"
        elif intent == "action" or action_matches:
            actionability = "action_request"
        elif intent in {"analysis", "question", "conversation"}:
            actionability = "analytical"
        else:
            actionability = "undetermined"

        return actionability, {
            "informational": informational,
            "hypothetical": hypothetical,
            "negated": negated,
            "action_detected": bool(action_matches)
        }

    def assess(self, text, intent=None):
        if not isinstance(text, str) or not text.strip():
            return {
                "risk_level": "unknown",
                "hazard_severity": "unknown",
                "actionability": "unknown",
                "score": 0,
                "requires_approval": True,
                "execution_allowed": False,
                "reasoning": ["Empty or invalid input"],
                "signals": {},
                "recommended_controls": [
                    "Do not execute invalid or empty requests"
                ],
                "version": self.VERSION
            }

        normalized = re.sub(r"\s+", " ", text.lower()).strip()

        hazard, score, signals, reasoning = self._detect_hazard(
            normalized
        )

        actionability, context_signals = self._classify_actionability(
            normalized, intent
        )

        requires_approval = (
            actionability == "action_request"
            and hazard in {"high", "critical"}
        )

        if hazard == "critical":
            controls = [
                "Hold any requested action",
                "Verify authorization and target",
                "Require security review and appropriate safeguards"
            ]
        elif hazard == "high":
            controls = [
                "Require explicit human approval before action",
                "Verify authorization and target",
                "Prepare backup or rollback where applicable"
            ]
        elif hazard == "medium":
            controls = [
                "Validate context and permissions",
                "Review operational consequences"
            ]
        else:
            controls = [
                "Apply normal response validation"
            ]

        reasoning.append(
            f"Actionability classified as: {actionability}"
        )

        if context_signals["hypothetical"]:
            reasoning.append("Hypothetical context detected")

        if context_signals["negated"]:
            reasoning.append("Negation or prohibition detected")

        if context_signals["informational"]:
            reasoning.append("Informational context detected")

        return {
            "risk_level": hazard,
            "hazard_severity": hazard,
            "actionability": actionability,
            "score": score,
            "requires_approval": requires_approval,
            "execution_allowed": False,
            "reasoning": reasoning,
            "signals": {
                **signals,
                "context": context_signals
            },
            "recommended_controls": controls,
            "version": self.VERSION
        }
