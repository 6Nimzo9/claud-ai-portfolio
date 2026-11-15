from typing import Optional

from .models import (
    CognitiveAssessment,
    CognitiveContext,
    CognitivePlan,
    CognitiveResult,
)
from .intent_analyzer import IntentAnalyzer
from .complexity_assessor import ComplexityAssessor
from .risk_assessor import RiskAssessor
from .strategy_selector import StrategySelector


class CognitiveOrchestrator:
    VERSION = "0.1.0"
    MODE = "shadow"

    def __init__(self):
        self.intent_analyzer = IntentAnalyzer()
        self.complexity_assessor = ComplexityAssessor()
        self.risk_assessor = RiskAssessor()
        self.strategy_selector = StrategySelector()

    def analyze(
        self,
        text: str,
        context: Optional[CognitiveContext] = None,
    ) -> CognitiveResult:

        if not isinstance(text, str) or not text.strip():
            raise ValueError("Input must be a non-empty string")

        if context is not None and not isinstance(
            context, CognitiveContext
        ):
            raise TypeError("context must be CognitiveContext or None")

        if context is None:
            context = CognitiveContext()

        context_available = bool(
            context.recent_messages
            or context.memories
            or context.knowledge_evidence
        )

        # Stage 1: Analyze user intent
        intent_result = self.intent_analyzer.analyze(text)

        intent = intent_result.get("intent", "unknown")
        confidence = intent_result.get("confidence", 0.0)

        # Stage 2: Assess cognitive complexity
        complexity_result = self.complexity_assessor.assess(
            text,
            intent=intent,
            context_available=context_available,
        )

        # Stage 3: Assess safety and operational risk
        risk_result = self.risk_assessor.assess(
            text,
            intent=intent,
        )

        # Stage 4: Select cognitive strategy
        strategy_result = self.strategy_selector.select(
            intent_result=intent_result,
            complexity_result=complexity_result,
            risk_result=risk_result,
            context_available=context_available,
        )

        # Stage 5: Consolidate cognitive assessment
        assessment = CognitiveAssessment(
            intent=intent,
            complexity=complexity_result.get(
                "complexity", "unknown"
            ),
            risk_level=risk_result.get(
                "hazard_severity", "unknown"
            ),
            confidence=confidence,
            reasoning=(
                [intent_result.get("reason", "")]
                + complexity_result.get("reasoning", [])
                + risk_result.get("reasoning", [])
            ),
        )

        # Stage 6: Build a non-executable cognitive plan
        plan = CognitivePlan(
            strategy=strategy_result["strategy"],
            modules=strategy_result["modules"],
            steps=strategy_result["steps"],
            requires_approval=strategy_result["requires_approval"],
            execution_allowed=False,
        )

        return CognitiveResult(
            assessment=assessment,
            plan=plan,
            context=context,
            status="shadow_analysis",
            metadata={
                "orchestrator_version": self.VERSION,
                "mode": self.MODE,
                "intent_analysis": intent_result,
                "complexity_analysis": complexity_result,
                "risk_analysis": risk_result,
                "strategy_selection": strategy_result,
                "execution_enabled": False,
            },
        )
