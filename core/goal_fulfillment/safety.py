"""Risk checks and secret-safe summaries for UGFE events and action logs."""

import json
import re
from dataclasses import dataclass
from typing import Any

from .models import RiskTier, ToolDefinition


_SECRET_VALUE = re.compile(
    r"(?i)(\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|password|passwd|secret|authorization)\b\s*[:=]\s*)([^\s,;]+)"
)
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]+=*")
_SENSITIVE_KEYS = re.compile(r"(?i)(api[_-]?key|access[_-]?token|refresh[_-]?token|token|password|passwd|secret|authorization)")


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): "[REDACTED]" if _SENSITIVE_KEYS.search(str(key)) else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    if isinstance(value, str):
        parsed = None
        if len(value) <= 100_000:
            try:
                parsed = json.loads(value)
            except (json.JSONDecodeError, TypeError):
                pass
        if isinstance(parsed, (dict, list)):
            return redact(parsed)
        value = _SECRET_VALUE.sub(r"\1[REDACTED]", value)
        return _BEARER.sub("Bearer [REDACTED]", value)
    if value is None or isinstance(value, (int, float, bool)):
        return value
    return str(value)


def safe_summary(value: Any, limit: int = 1200) -> str:
    safe = redact(value)
    if isinstance(safe, str):
        rendered = safe
    else:
        rendered = json.dumps(safe, ensure_ascii=False, sort_keys=True, default=str)
    return rendered[:limit]


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    requires_approval: bool
    reason: str


class RiskPolicy:
    """Conservative default policy; risk is declared by each registered tool."""

    APPROVAL_RISKS = {
        RiskTier.EXTERNAL, RiskTier.DESTRUCTIVE, RiskTier.SYSTEM,
        RiskTier.WORKSPACE_WRITE,
    }

    def assess(self, definition: ToolDefinition) -> PolicyDecision:
        if not isinstance(definition.risk_tier, RiskTier):
            return PolicyDecision(False, False, "Tool declared an unknown risk tier")
        needs_approval = definition.requires_approval or definition.risk_tier in self.APPROVAL_RISKS
        if definition.risk_tier == RiskTier.READ_ONLY and not definition.permission:
            return PolicyDecision(False, False, "Read-only tool has no declared permission scope")
        reason = (
            f"{definition.risk_tier.value} action requires human approval"
            if needs_approval else "Registered read-only action is within policy"
        )
        return PolicyDecision(True, needs_approval, reason)


def propose_skill(capability: str, step_description: str = "") -> dict[str, Any]:
    """Return a non-executable contract; generated code is never activated."""
    return {
        "name": capability,
        "version": "0.1.0",
        "status": "proposed",
        "description": step_description or f"Implement the missing capability: {capability}",
        "inputs": {"declared_by": "goal step input schema"},
        "outputs": {"declared_by": "acceptance criteria"},
        "permissions": ["must be reviewed and least-privilege scoped"],
        "tests": ["required before validation or activation"],
        "provenance": "UGFE capability-gap analysis",
        "activation_requires_human_approval": True,
        "executable_code_included": False,
    }
