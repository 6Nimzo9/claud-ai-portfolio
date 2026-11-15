from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class CognitiveAssessment:
    intent: str
    complexity: str
    risk_level: str
    confidence: float
    reasoning: List[str] = field(default_factory=list)


@dataclass
class CognitiveContext:
    conversation_id: Optional[int] = None
    recent_messages: List[Dict[str, Any]] = field(default_factory=list)
    memories: List[Dict[str, Any]] = field(default_factory=list)
    knowledge_evidence: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class CognitivePlan:
    strategy: str
    modules: List[str] = field(default_factory=list)
    steps: List[str] = field(default_factory=list)
    requires_approval: bool = False
    execution_allowed: bool = False


@dataclass
class CognitiveResult:
    assessment: CognitiveAssessment
    plan: CognitivePlan
    context: Optional[CognitiveContext] = None
    status: str = "planned"
    metadata: Dict[str, Any] = field(default_factory=dict)
