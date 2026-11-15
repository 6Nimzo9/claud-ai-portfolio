"""Typed data contracts for the Universal Goal Fulfillment Engine MVP."""

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class GoalState(str, Enum):
    RECEIVED = "received"
    ANALYZING = "analyzing"
    PLANNING = "planning"
    RESEARCHING = "researching"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    NEEDS_APPROVAL = "needs_approval"


class RiskTier(str, Enum):
    READ_ONLY = "read_only"
    WORKSPACE_WRITE = "workspace_write"
    EXTERNAL = "external"
    DESTRUCTIVE = "destructive"
    SYSTEM = "system"


@dataclass
class AcceptanceCriterion:
    description: str
    check: str
    expected: str | None = None

    def __post_init__(self):
        allowed = {"tool_succeeded", "output_contains", "output_equals", "evidence_present"}
        if self.check not in allowed:
            raise ValueError(f"Unsupported acceptance check: {self.check}")
        if not self.description.strip():
            raise ValueError("Acceptance criterion description is required")
        if self.check in {"output_contains", "output_equals"} and (
            not isinstance(self.expected, str) or not self.expected.strip()
        ):
            raise ValueError(f"{self.check} requires an expected value")
        if len(self.description) > 500 or (self.expected is not None and len(self.expected) > 1000):
            raise ValueError("Acceptance criterion fields exceed their size limits")


@dataclass
class ToolStep:
    tool_name: str
    inputs: dict[str, Any] = field(default_factory=dict, repr=False)
    description: str = ""
    step_id: str = field(default_factory=lambda: str(uuid4()))
    status: str = "pending"
    attempts: int = 0
    last_error: str | None = None
    output_summary: str | None = None

    def __post_init__(self):
        if not isinstance(self.tool_name, str) or not self.tool_name.strip() or len(self.tool_name) > 256:
            raise ValueError("Tool step requires a valid registered tool name")
        if not isinstance(self.inputs, dict):
            raise ValueError("Tool step inputs must be an object")
        if len(self.description) > 500:
            raise ValueError("Tool step description is too long")

    def to_dict(self) -> dict[str, Any]:
        """Serialize without invocation inputs, which may contain secrets."""
        return {
            "tool_name": self.tool_name,
            "description": self.description,
            "step_id": self.step_id,
            "status": self.status,
            "attempts": self.attempts,
            "last_error": self.last_error,
            "output_summary": self.output_summary,
            "inputs_persisted": False,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ToolStep":
        return cls(
            tool_name=data["tool_name"], description=data.get("description", ""),
            step_id=data["step_id"], status=data.get("status", "pending"),
            attempts=int(data.get("attempts", 0)), last_error=data.get("last_error"),
            output_summary=data.get("output_summary"), inputs={},
        )


@dataclass
class Evidence:
    title: str
    source_id: str
    provider: str
    excerpt: str
    retrieved_at: str = field(default_factory=utc_now)
    publisher: str | None = None
    confidence: float = 0.5
    independently_corroborated: bool = False
    classification: str = "fact"
    content_hash: str | None = None
    topic: str = ""
    evidence_id: str = field(default_factory=lambda: str(uuid4()))
    trust: str = "untrusted_external_data"

    def __post_init__(self):
        if not self.title.strip() or not self.source_id.strip() or not self.provider.strip():
            raise ValueError("Evidence title, source identifier, and provider are required")
        if len(self.title) > 1000 or len(self.source_id) > 2048 or len(self.provider) > 256:
            raise ValueError("Evidence source metadata exceeds its size limits")
        if len(self.excerpt) > 10_000 or len(self.topic) > 1000:
            raise ValueError("Evidence content exceeds its size limits")
        if not 0.0 <= float(self.confidence) <= 1.0:
            raise ValueError("Evidence confidence must be between 0 and 1")
        if self.classification not in {"fact", "assumption", "inference", "open_question"}:
            raise ValueError("Invalid evidence classification")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Evidence":
        values = dict(data)
        values.setdefault("excerpt", "")
        return cls(**values)


@dataclass
class CapabilityGap:
    capability: str
    availability: str
    evidence: str
    safe_next_action: str
    approval_required: bool = False
    tested: bool = False


@dataclass
class VerificationResult:
    criterion: str
    passed: bool
    method: str
    details: str
    checked_at: str = field(default_factory=utc_now)


@dataclass
class Goal:
    user_goal: str
    clarified_intent: str
    acceptance_criteria: list[AcceptanceCriterion]
    required_capabilities: list[str] = field(default_factory=list)
    steps: list[ToolStep] = field(default_factory=list)
    research_topic: str | None = None
    goal_id: str = field(default_factory=lambda: str(uuid4()))
    state: GoalState = GoalState.RECEIVED
    current_step_id: str | None = None
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    evidence: list[Evidence] = field(default_factory=list)
    prior_knowledge: list[dict[str, Any]] = field(default_factory=list)
    capability_gaps: list[CapabilityGap] = field(default_factory=list)
    verification_results: list[VerificationResult] = field(default_factory=list)
    risk_approval_requirements: list[str] = field(default_factory=list)
    retries_used: int = 0
    iteration_count: int = 0
    max_retries: int = 3
    iteration_budget: int = 12
    time_budget_seconds: float = 120.0
    final_result: str | None = None
    blocker_reason: str | None = None
    proposed_skill: dict[str, Any] | None = None
    cognitive_assessment: dict[str, Any] | None = None
    outputs: list[str] = field(default_factory=list, repr=False)

    def to_dict(self, include_evidence_text: bool = True) -> dict[str, Any]:
        evidence = []
        for item in self.evidence:
            entry = item.to_dict()
            if not include_evidence_text:
                entry.pop("excerpt", None)
            evidence.append(entry)
        return {
            "goal_id": self.goal_id,
            "user_goal": self.user_goal,
            "clarified_intent": self.clarified_intent,
            "acceptance_criteria": [asdict(item) for item in self.acceptance_criteria],
            "required_capabilities": list(self.required_capabilities),
            "state": self.state.value,
            "current_step_id": self.current_step_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "steps": [item.to_dict() for item in self.steps],
            "research_topic": self.research_topic,
            "evidence": evidence,
            "prior_knowledge": list(self.prior_knowledge),
            "capability_gaps": [asdict(item) for item in self.capability_gaps],
            "verification_results": [asdict(item) for item in self.verification_results],
            "risk_approval_requirements": list(self.risk_approval_requirements),
            "retries_used": self.retries_used,
            "iteration_count": self.iteration_count,
            "max_retries": self.max_retries,
            "iteration_budget": self.iteration_budget,
            "time_budget_seconds": self.time_budget_seconds,
            "final_result": self.final_result,
            "blocker_reason": self.blocker_reason,
            "proposed_skill": self.proposed_skill,
            "cognitive_assessment": self.cognitive_assessment,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Goal":
        return cls(
            goal_id=data["goal_id"], user_goal=data["user_goal"],
            clarified_intent=data.get("clarified_intent", data["user_goal"]),
            acceptance_criteria=[AcceptanceCriterion(**x) for x in data.get("acceptance_criteria", [])],
            required_capabilities=data.get("required_capabilities", []),
            steps=[ToolStep.from_dict(x) for x in data.get("steps", [])],
            research_topic=data.get("research_topic"), state=GoalState(data["state"]),
            current_step_id=data.get("current_step_id"),
            created_at=data["created_at"], updated_at=data["updated_at"],
            evidence=[Evidence.from_dict(x) for x in data.get("evidence", [])],
            prior_knowledge=data.get("prior_knowledge", []),
            capability_gaps=[CapabilityGap(**x) for x in data.get("capability_gaps", [])],
            verification_results=[VerificationResult(**x) for x in data.get("verification_results", [])],
            risk_approval_requirements=data.get("risk_approval_requirements", []),
            retries_used=data.get("retries_used", 0), iteration_count=data.get("iteration_count", 0),
            max_retries=data.get("max_retries", 3), iteration_budget=data.get("iteration_budget", 12),
            time_budget_seconds=data.get("time_budget_seconds", 120.0),
            final_result=data.get("final_result"), blocker_reason=data.get("blocker_reason"),
            proposed_skill=data.get("proposed_skill"), cognitive_assessment=data.get("cognitive_assessment"),
        )


@dataclass
class ToolDefinition:
    name: str
    description: str
    input_schema: dict[str, Any]
    output_description: str
    risk_tier: RiskTier = RiskTier.READ_ONLY
    permission: str = ""
    requires_approval: bool = False
    retry_safe: bool = False

    def __post_init__(self):
        if not isinstance(self.risk_tier, RiskTier):
            self.risk_tier = RiskTier(self.risk_tier)
        if type(self.retry_safe) is not bool:
            raise ValueError("retry_safe must be a bool")
        if self.retry_safe and self.risk_tier is not RiskTier.READ_ONLY:
            raise ValueError("retry_safe=True is only allowed for read-only tools")

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["risk_tier"] = self.risk_tier.value
        return result
