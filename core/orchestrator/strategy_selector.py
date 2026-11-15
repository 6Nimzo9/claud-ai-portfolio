from typing import Any, Dict


class StrategySelector:
    VERSION = "0.2.0"

    VALID_COMPLEXITIES = {
        "direct",
        "contextual",
        "deep_cognitive",
    }

    VALID_HAZARDS = {
        "low",
        "medium",
        "high",
        "critical",
    }

    def select(
        self,
        intent_result: Dict[str, Any],
        complexity_result: Dict[str, Any],
        risk_result: Dict[str, Any],
        context_available: bool = False,
    ) -> Dict[str, Any]:

        decision_reason = []

        # 1. Validate input contracts
        if not all(
            isinstance(item, dict)
            for item in (
                intent_result,
                complexity_result,
                risk_result,
            )
        ):
            return self._fallback("Invalid assessor input")

        intent = intent_result.get("intent", "unknown")
        confidence = intent_result.get("confidence", 0.0)

        complexity = complexity_result.get(
            "complexity", "unknown"
        )

        hazard = risk_result.get(
            "hazard_severity", "unknown"
        )

        actionability = risk_result.get(
            "actionability", "unknown"
        )

        requires_approval = risk_result.get(
            "requires_approval", False
        )

        execution_allowed = risk_result.get(
            "execution_allowed", False
        )

        if (
            complexity not in self.VALID_COMPLEXITIES
            or hazard not in self.VALID_HAZARDS
            or not isinstance(confidence, (int, float))
            or not 0 <= confidence <= 1
            or not isinstance(requires_approval, bool)
            or not isinstance(execution_allowed, bool)
        ):
            return self._fallback("Invalid assessor values")

        # 2. Select strategy by priority
        if hazard in {"critical", "high"}:
            strategy = "safety_review"
            reasoning_depth = (
                "deep" if complexity == "deep_cognitive"
                else "contextual"
            )
            decision_reason.append(
                f"{hazard}_hazard_detected"
            )

        elif complexity == "deep_cognitive":
            strategy = "deep_cognitive"
            reasoning_depth = "deep"
            decision_reason.append(
                "deep_complexity_detected"
            )

        elif complexity == "contextual" or context_available:
            strategy = "contextual_reasoning"
            reasoning_depth = "contextual"
            decision_reason.append(
                "contextual_requirement_detected"
            )

        else:
            strategy = "direct_response"
            reasoning_depth = "direct"
            decision_reason.append(
                "low_complexity_request"
            )

        # 3. Select recommended modules
        module_map = {
            "direct_response": [
                "response_synthesizer"
            ],
            "contextual_reasoning": [
                "context_manager",
                "knowledge_engine",
                "reasoning_engine",
                "response_synthesizer",
            ],
            "deep_cognitive": [
                "context_manager",
                "knowledge_engine",
                "reasoning_engine",
                "critique_engine",
                "evaluation_engine",
                "response_synthesizer",
            ],
            "safety_review": [
                "context_manager",
                "knowledge_engine",
                "reasoning_engine",
                "critique_engine",
                "safety_review",
                "response_synthesizer",
            ],
        }

        steps_map = {
            "direct_response": [
                "interpret_request",
                "generate_response",
            ],
            "contextual_reasoning": [
                "interpret_request",
                "retrieve_context",
                "retrieve_knowledge",
                "generate_response",
            ],
            "deep_cognitive": [
                "interpret_request",
                "retrieve_context",
                "retrieve_knowledge",
                "analyze_problem",
                "evaluate_reasoning",
                "critique_result",
                "generate_response",
            ],
            "safety_review": [
                "interpret_request",
                "assess_hazards",
                "evaluate_actionability",
                "review_safety_constraints",
                "generate_safe_response",
            ],
        }

        # 4. Preserve safety constraints
        approval_required = (
            requires_approval
            or (
                actionability == "action_request"
                and hazard in {"high", "critical"}
            )
        )

        decision_reason.append(
            f"intent:{intent}"
        )

        if not execution_allowed:
            decision_reason.append(
                "execution_disabled_by_policy"
            )

        return {
            "strategy": strategy,
            "reasoning_depth": reasoning_depth,
            "modules": module_map[strategy],
            "steps": steps_map[strategy],
            "requires_approval": approval_required,
            "execution_allowed": False,
            "decision_reason": decision_reason,
            "metadata": {
                "version": self.VERSION,
                "intent": intent,
                "intent_confidence": confidence,
                "complexity": complexity,
                "hazard_severity": hazard,
                "actionability": actionability,
                "context_available": context_available,
            },
        }

    def _fallback(self, reason: str) -> Dict[str, Any]:
        return {
            "strategy": "safe_fallback",
            "reasoning_depth": "contextual",
            "modules": [
                "safety_review",
                "response_synthesizer",
            ],
            "steps": [
                "validate_input",
                "request_clarification",
            ],
            "requires_approval": False,
            "execution_allowed": False,
            "decision_reason": [reason],
            "metadata": {
                "version": self.VERSION,
                "fallback": True,
            },
        }
