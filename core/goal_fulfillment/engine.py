"""Bounded, policy-gated goal execution with deterministic acceptance checks."""

import hashlib
import hmac
import itertools
import json
import secrets
import time
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Iterable, Protocol

from .models import (
    AcceptanceCriterion, CapabilityGap, Evidence, Goal, GoalState,
    RiskTier, ToolDefinition, ToolStep, VerificationResult,
)
from .repository import GoalRepository
from .safety import RiskPolicy, propose_skill, redact, safe_summary
from .tools import ToolRegistry


class ResearchProvider(Protocol):
    """Configured research adapter. Search results are untrusted evidence data."""
    def search(self, topic: str) -> Iterable[Evidence | dict[str, Any]]: ...


_TRANSITIONS = {
    GoalState.RECEIVED: {GoalState.ANALYZING, GoalState.BLOCKED},
    GoalState.ANALYZING: {GoalState.PLANNING, GoalState.RESEARCHING, GoalState.BLOCKED, GoalState.NEEDS_APPROVAL},
    GoalState.RESEARCHING: {GoalState.PLANNING, GoalState.BLOCKED},
    GoalState.PLANNING: {GoalState.EXECUTING, GoalState.BLOCKED, GoalState.NEEDS_APPROVAL},
    GoalState.EXECUTING: {GoalState.VERIFYING, GoalState.BLOCKED, GoalState.NEEDS_APPROVAL},
    GoalState.VERIFYING: {
        GoalState.COMPLETED, GoalState.EXECUTING, GoalState.NEEDS_APPROVAL, GoalState.BLOCKED,
    },
    GoalState.NEEDS_APPROVAL: {GoalState.EXECUTING, GoalState.BLOCKED},
    GoalState.COMPLETED: set(), GoalState.BLOCKED: set(),
}

_RECOVERY_STATES = {
    GoalState.RECEIVED, GoalState.ANALYZING, GoalState.PLANNING,
    GoalState.EXECUTING, GoalState.NEEDS_APPROVAL,
}


class GoalFulfillmentEngine:
    def __init__(self, repository: GoalRepository, tool_registry: ToolRegistry | None = None,
                 research_provider: ResearchProvider | None = None, policy: RiskPolicy | None = None,
                 workspace_root: str = ".", max_retries: int = 3,
                 iteration_budget: int = 12, time_budget_seconds: float = 120.0):
        self.repository = repository
        self.tool_registry = tool_registry or ToolRegistry.with_workspace_read(workspace_root)
        self.research_provider = research_provider
        self.policy = policy or RiskPolicy()
        self.max_retries = max(0, min(int(max_retries), 3))
        self.iteration_budget = max(1, int(iteration_budget))
        self.time_budget_seconds = max(1.0, float(time_budget_seconds))
        # Invocation arguments stay in process memory and never enter the DB or logs.
        self._runtime_inputs: dict[tuple[str, str], dict[str, Any]] = {}
        self._approval_grants: dict[tuple[str, str], str] = {}
        self._approval_challenges: dict[tuple[str, str], tuple[str, str]] = {}
        self._approval_challenge_delivery: dict[str, dict[str, str]] = {}
        self._deadlines: dict[str, float] = {}

    def create_goal(self, user_goal: str, acceptance_criteria: list[AcceptanceCriterion | dict[str, Any]],
                    clarified_intent: str | None = None, required_capabilities: list[str] | None = None,
                    steps: list[ToolStep | dict[str, Any]] | None = None,
                    research_topic: str | None = None, max_retries: int | None = None,
                    iteration_budget: int | None = None,
                    time_budget_seconds: float | None = None) -> Goal:
        if not isinstance(user_goal, str) or not user_goal.strip():
            raise ValueError("A non-empty user goal is required")
        if len(user_goal) > 10_000:
            raise ValueError("User goal exceeds 10000 characters")
        criteria = [x if isinstance(x, AcceptanceCriterion) else AcceptanceCriterion(**x)
                    for x in (acceptance_criteria or [])]
        normalized_steps = []
        for item in steps or []:
            step = item if isinstance(item, ToolStep) else ToolStep(**item)
            normalized_steps.append(step)
        if len(criteria) > 20 or len(normalized_steps) > 20:
            raise ValueError("A goal may contain at most 20 acceptance criteria and 20 steps")
        goal = Goal(
            user_goal=user_goal.strip(), clarified_intent=(clarified_intent or user_goal).strip(),
            acceptance_criteria=criteria,
            required_capabilities=list(dict.fromkeys(required_capabilities or [])),
            steps=normalized_steps, research_topic=(research_topic.strip() if research_topic else None),
            max_retries=self.max_retries if max_retries is None else max(0, min(int(max_retries), 3)),
            iteration_budget=self.iteration_budget if iteration_budget is None else max(1, min(int(iteration_budget), 50)),
            time_budget_seconds=self.time_budget_seconds if time_budget_seconds is None else max(1.0, min(float(time_budget_seconds), 3600.0)),
        )
        self.repository.save_goal(goal)
        self.repository.add_event(goal.goal_id, "goal_received", None, goal.state.value,
                                  {"acceptance_criteria_count": len(criteria)})
        for step in normalized_steps:
            self._runtime_inputs[(goal.goal_id, step.step_id)] = dict(step.inputs)
        return goal

    def run(self, goal_id: str) -> Goal:
        goal = self.repository.get_goal(goal_id)
        if goal.state != GoalState.RECEIVED:
            return goal
        deadline = time.monotonic() + goal.time_budget_seconds
        self._deadlines[goal.goal_id] = deadline
        self._transition(goal, GoalState.ANALYZING, "analysis_started")
        self._attach_cognitive_assessment(goal)
        if not goal.acceptance_criteria:
            return self._block(goal, "acceptance_criteria_required",
                               "Add at least one measurable acceptance criterion before execution.")

        required = list(goal.required_capabilities)
        required.extend(step.tool_name for step in goal.steps if step.tool_name not in required)
        if goal.research_topic and "research" not in required:
            required.append("research")
        goal.required_capabilities = required
        self._analyze_capabilities(goal)

        if goal.research_topic:
            goal.prior_knowledge = self.repository.inspect_existing_knowledge(goal.research_topic)
            if goal.prior_knowledge:
                self._event(goal, "existing_knowledge_inspected", {
                    "record_count": len(goal.prior_knowledge),
                    "note": "Existing store records are context references; only source-backed UGFE evidence can satisfy evidence checks.",
                })
            cached = self.repository.search_knowledge(goal.research_topic)
            if cached:
                goal.evidence.extend(cached)
                self._event(goal, "verified_knowledge_recalled", {"count": len(cached), "query": goal.research_topic})
            else:
                if self.research_provider is None:
                    self._transition(goal, GoalState.RESEARCHING, "research_requested",
                                     {"provider_available": False})
                    return self._block(goal, "research_unavailable",
                                       "No research provider is configured. UGFE did not search or create sources.")
                self._transition(goal, GoalState.RESEARCHING, "research_started",
                                 {"provider": type(self.research_provider).__name__})
                try:
                    acquired = self._acquire_evidence(goal.research_topic)
                except Exception as exc:
                    safe_error = safe_summary(f"{type(exc).__name__}: {exc}", 500)
                    self._event(goal, "research_failed", {"error": safe_error})
                    return self._block(goal, "research_failed", safe_error)
                if not acquired:
                    return self._block(goal, "research_no_evidence",
                                       "The configured provider returned no source-backed evidence.")
                goal.evidence.extend(acquired)
                self._event(goal, "research_evidence_acquired", {
                    "provider": type(self.research_provider).__name__, "count": len(acquired),
                    "sources": [item.source_id for item in acquired],
                })

        if time.monotonic() >= deadline:
            return self._block(goal, "time_budget_exhausted", "Goal time budget was exhausted during analysis.")
        self._transition(goal, GoalState.PLANNING, "plan_created", {
            "steps": [{"tool": step.tool_name, "description": step.description} for step in goal.steps],
            "evidence_count": len(goal.evidence),
        })

        missing_steps = [step for step in goal.steps if self.tool_registry.get(step.tool_name) is None]
        if missing_steps:
            for step in missing_steps:
                goal.proposed_skill = propose_skill(step.tool_name, step.description)
            return self._block(goal, "execution_capability_missing",
                               "A required tool is not registered. A non-executable skill contract was proposed.")
        missing_requirements = [gap for gap in goal.capability_gaps
                                if gap.availability == "missing" and gap.capability != "research"]
        if missing_requirements:
            gap = missing_requirements[0]
            goal.proposed_skill = propose_skill(gap.capability)
            return self._block(goal, "required_capability_missing",
                               f"Required capability is unavailable: {gap.capability}.")
        if not goal.steps and not goal.research_topic:
            return self._block(goal, "no_execution_plan", "No registered tool step or research task was supplied.")

        if not self._criteria_can_be_checked(goal):
            return self._block(goal, "criteria_not_checkable",
                               "Acceptance criteria do not match the supplied tool steps or evidence task.")

        pending_approval = []
        for step in goal.steps:
            definition, _ = self.tool_registry.get(step.tool_name)  # type: ignore[misc]
            decision = self.policy.assess(definition)
            if not decision.allowed:
                return self._block(goal, "policy_denied", decision.reason)
            if decision.requires_approval:
                pending_approval.append((step, definition, decision.reason))
        if pending_approval:
            step, definition, reason = pending_approval[0]
            self._request_approval(goal, step, definition, reason)
            return goal

        self._transition(goal, GoalState.EXECUTING, "execution_started")
        self._execute_and_verify(goal, deadline)
        return goal

    def list_recovery_candidates(self) -> list[dict[str, Any]]:
        """List incomplete goals with a safe, bounded recovery assessment."""
        candidates = []
        for goal in self.repository.list_recovery_goals():
            recoverable, reason = self._recovery_readiness(goal)
            requires_fresh_approval = any(
                (entry := self.tool_registry.get(step.tool_name)) is not None
                and self.policy.assess(entry[0]).requires_approval
                for step in goal.steps
            )
            candidates.append({
                "goal_id": goal.goal_id,
                "user_goal": safe_summary(goal.user_goal, 500),
                "state": goal.state.value,
                "created_at": goal.created_at,
                "updated_at": goal.updated_at,
                "step_count": len(goal.steps),
                "attempted_step_count": sum(step.attempts > 0 for step in goal.steps),
                "recoverable": recoverable,
                "reason": reason,
                # Goal JSON deliberately omits every step's invocation arguments.
                "requires_new_inputs": bool(goal.steps),
                "requires_fresh_approval": requires_fresh_approval,
            })
        return candidates

    def recover_goal(self, goal_id: str, step_inputs: dict[str, dict[str, Any]]) -> Goal:
        """Explicitly restart a pre-execution goal with caller-resupplied inputs."""
        goal = self.repository.get_goal(goal_id)
        if goal.state in {GoalState.COMPLETED, GoalState.BLOCKED}:
            raise ValueError("Terminal goals cannot be recovered")
        if goal.state not in _RECOVERY_STATES:
            raise ValueError("Goal state is not eligible for pre-execution recovery")

        if self._has_unknown_step_outcome(goal):
            self.repository.add_event(
                goal.goal_id, "goal_recovery_blocked", goal.state.value, goal.state.value,
                {"reason": "manual_review_required", "step_count": len(goal.steps)},
            )
            raise ValueError("Recovery requires manual review because a tool outcome may be unknown")
        if goal.research_topic:
            raise ValueError("Research goals are not supported by recovery")
        if not goal.steps:
            raise ValueError("Recovery requires at least one registered tool step")
        if not isinstance(step_inputs, dict):
            raise ValueError("Recovery inputs must be a dictionary keyed by step_id")

        step_ids = [step.step_id for step in goal.steps]
        if any(not isinstance(step_id, str) or not step_id for step_id in step_ids):
            raise ValueError("Goal contains an invalid step_id")
        if len(set(step_ids)) != len(step_ids):
            raise ValueError("Goal contains duplicate step_ids and cannot be recovered")
        if any(not isinstance(step_id, str) for step_id in step_inputs):
            raise ValueError("Recovery input keys must be step_id strings")
        if set(step_inputs) != set(step_ids):
            raise ValueError("Recovery inputs must contain exactly one entry for every goal step")

        validated_inputs: dict[tuple[str, str], dict[str, Any]] = {}
        for step in goal.steps:
            entry = self.tool_registry.get(step.tool_name)
            if entry is None:
                raise ValueError(f"Tool is not currently registered: {step.tool_name}")
            definition, _ = entry
            decision = self.policy.assess(definition)
            if not decision.allowed:
                raise ValueError(f"Recovery denied by current risk policy for {step.tool_name}")
            arguments = step_inputs[step.step_id]
            if not isinstance(arguments, dict):
                raise ValueError(f"Recovery inputs for step {step.step_id} must be a dictionary")
            try:
                self.tool_registry.validate_inputs(step.tool_name, arguments)
                validated_inputs[(goal.goal_id, step.step_id)] = deepcopy(arguments)
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"Invalid recovery inputs for step {step.step_id}: {exc}") from exc

        prior_state = goal.state
        self._clear_runtime_state(goal.goal_id)
        self._runtime_inputs.update(validated_inputs)

        # These steps are eligible only when they are still pristine. Preserve
        # their zero attempt counts and pending status; action history is untouched.
        for step in goal.steps:
            step.inputs = {}
            step.last_error = None
            step.output_summary = None
        goal.state = GoalState.RECEIVED
        goal.current_step_id = None
        goal.blocker_reason = None
        goal.final_result = None
        goal.verification_results = []
        goal.risk_approval_requirements = []
        goal.evidence = []
        goal.prior_knowledge = []
        goal.capability_gaps = []
        goal.proposed_skill = None
        goal.cognitive_assessment = None
        goal.outputs = []
        goal.retries_used = 0
        goal.iteration_count = 0
        goal.updated_at = datetime.now(timezone.utc).isoformat()
        self.repository.save_goal(goal)
        self.repository.add_event(goal.goal_id, "goal_recovery_started", prior_state.value,
                                  GoalState.RECEIVED.value, {
                                      "prior_state": prior_state.value,
                                      "new_state": GoalState.RECEIVED.value,
                                      "step_count": len(goal.steps),
                                      "inputs_resupplied": True,
                                  })
        return self.run(goal.goal_id)

    def _clear_runtime_state(self, goal_id: str) -> None:
        for key in [key for key in self._runtime_inputs if key[0] == goal_id]:
            self._runtime_inputs.pop(key, None)
        for key in [key for key in self._approval_grants if key[0] == goal_id]:
            self._approval_grants.pop(key, None)
        for key in [key for key in self._approval_challenges if key[0] == goal_id]:
            self._approval_challenges.pop(key, None)
        self._approval_challenge_delivery.pop(goal_id, None)
        self._deadlines.pop(goal_id, None)

    def _has_unknown_step_outcome(self, goal: Goal) -> bool:
        action_step_ids = {
            action["step_id"] for action in self.repository.list_actions(goal.goal_id)
        }
        return any(
            step.attempts != 0 or step.status != "pending" or step.step_id in action_step_ids
            for step in goal.steps
        )

    def _recovery_readiness(self, goal: Goal) -> tuple[bool, str]:
        if self._has_unknown_step_outcome(goal):
            return False, "manual_review_required"
        if goal.state not in _RECOVERY_STATES:
            return False, "unsupported_state"
        if goal.research_topic:
            return False, "research_recovery_unsupported"
        if not goal.steps:
            return False, "no_tool_steps"
        for step in goal.steps:
            entry = self.tool_registry.get(step.tool_name)
            if entry is None:
                return False, "tool_not_registered"
            if not self.policy.assess(entry[0]).allowed:
                return False, "policy_denied"
        return True, "eligible_pre_execution"

    def approve(self, goal_id: str, approval_token: str) -> Goal:
        goal = self.repository.get_goal(goal_id)
        if goal.state != GoalState.NEEDS_APPROVAL:
            raise ValueError("This goal is not awaiting approval")
        if not isinstance(approval_token, str) or not approval_token:
            raise PermissionError("A valid pending-action approval token is required.")
        for step in goal.steps:
            if self.tool_registry.get(step.tool_name) and (goal.goal_id, step.step_id) not in self._runtime_inputs:
                return self._block(goal, "approval_resume_inputs_unavailable",
                                   "Invocation inputs are not retained across process restarts; re-submit the goal after approval.")
        deadline = self._deadlines.get(goal.goal_id)
        if deadline is None or time.monotonic() >= deadline:
            return self._block(goal, "time_budget_exhausted",
                               "The goal execution time budget expired while awaiting approval.")

        step = next((item for item in goal.steps if item.step_id == goal.current_step_id), None)
        if step is None:
            raise PermissionError("The pending approval is not bound to a goal step.")
        entry = self.tool_registry.get(step.tool_name)
        if entry is None:
            return self._block(goal, "execution_capability_missing",
                               f"Tool is no longer registered: {step.tool_name}")
        definition, _ = entry
        decision = self.policy.assess(definition)
        challenge_key = (goal.goal_id, step.step_id)
        challenge = self._approval_challenges.get(challenge_key)
        token_digest = hashlib.sha256(approval_token.encode("utf-8")).hexdigest()
        if (not decision.allowed or not decision.requires_approval or challenge is None
                or challenge[1] != self._step_fingerprint(goal, step, definition)
                or not hmac.compare_digest(challenge[0], token_digest)):
            raise PermissionError("Approval token is invalid or does not match the pending action.")

        self._approval_challenges.pop(challenge_key, None)
        self._approval_challenge_delivery.pop(goal.goal_id, None)
        self._approval_grants[challenge_key] = challenge[1]

        pending_approval = []
        for candidate in goal.steps:
            candidate_entry = self.tool_registry.get(candidate.tool_name)
            if candidate_entry is None:
                continue
            candidate_definition, _ = candidate_entry
            candidate_decision = self.policy.assess(candidate_definition)
            if not candidate_decision.allowed:
                return self._block(goal, "policy_denied", candidate_decision.reason)
            candidate_key = (goal.goal_id, candidate.step_id)
            if (candidate_decision.requires_approval
                    and self._approval_grants.get(candidate_key)
                    != self._step_fingerprint(goal, candidate, candidate_definition)):
                pending_approval.append((candidate, candidate_definition, candidate_decision.reason))

        if pending_approval:
            self._event(goal, "approval_granted", {
                "approved": True, "tool": definition.name, "step_id": step.step_id,
            })
            next_step, next_definition, next_reason = pending_approval[0]
            self._request_approval(goal, next_step, next_definition, next_reason)
            return goal

        goal.risk_approval_requirements = []
        self._transition(goal, GoalState.EXECUTING, "approval_granted", {
            "approved": True, "tools": [definition.name], "step_id": step.step_id,
        })
        self._execute_and_verify(goal, deadline)
        return goal

    def get_goal(self, goal_id: str) -> Goal:
        return self.repository.get_goal(goal_id)

    def report(self, goal_id: str) -> dict[str, Any]:
        goal = self.repository.get_goal(goal_id)
        return {"goal": goal.to_dict(), "events": self.repository.list_events(goal_id),
                "actions": self.repository.list_actions(goal_id)}

    def take_approval_challenge(self, goal_id: str) -> dict[str, str] | None:
        """Return the pending token once; the token is never persisted or included in reports."""
        goal = self.repository.get_goal(goal_id)
        if goal.state != GoalState.NEEDS_APPROVAL:
            self._approval_challenge_delivery.pop(goal_id, None)
            return None
        return self._approval_challenge_delivery.pop(goal_id, None)

    def _request_approval(self, goal: Goal, step: ToolStep, definition: ToolDefinition,
                          reason: str) -> None:
        token = secrets.token_urlsafe(32)
        challenge_key = (goal.goal_id, step.step_id)
        self._approval_challenges[challenge_key] = (
            hashlib.sha256(token.encode("utf-8")).hexdigest(),
            self._step_fingerprint(goal, step, definition),
        )
        self._approval_challenge_delivery[goal.goal_id] = {
            "step_id": step.step_id,
            "tool_name": definition.name,
            "input_summary": safe_summary(
                self._runtime_inputs.get((goal.goal_id, step.step_id), {}), 500,
            ),
            "approval_token": token,
        }
        goal.current_step_id = step.step_id
        goal.risk_approval_requirements = [f"{definition.name}: {reason}"]
        details = {"tool": definition.name, "step_id": step.step_id, "reason": reason}
        if goal.state == GoalState.NEEDS_APPROVAL:
            self._event(goal, "additional_approval_required", details)
        else:
            self._transition(goal, GoalState.NEEDS_APPROVAL, "approval_required", details)

    def _analyze_capabilities(self, goal: Goal) -> None:
        existing = {gap.capability for gap in goal.capability_gaps}
        for capability in goal.required_capabilities:
            if capability in existing:
                continue
            if capability == "research":
                if self.research_provider is not None:
                    goal.capability_gaps.append(CapabilityGap(
                        capability="research", availability="available",
                        evidence=f"Callable research adapter is configured: {type(self.research_provider).__name__}.",
                        safe_next_action="Use its source-backed results as untrusted evidence data.",
                    ))
                else:
                    goal.capability_gaps.append(CapabilityGap(
                        capability="research", availability="partially_available",
                        evidence="No live research provider is configured; previously acceptance-checked source evidence may still be recalled.",
                        safe_next_action="Search stored source evidence first; connect an approved provider for novel topics.",
                    ))
                continue
            if self.tool_registry.get(capability):
                goal.capability_gaps.append(CapabilityGap(
                    capability=capability, availability="available",
                    evidence="A callable implementation is present in the runtime tool registry.",
                    safe_next_action="Validate inputs and invoke through registered tool policy.",
                ))
            else:
                goal.capability_gaps.append(CapabilityGap(
                    capability=capability, availability="missing",
                    evidence="No registered tool or installed-skill registry entry matches this capability.",
                    safe_next_action="Propose a reviewed tool/skill contract; do not execute generated code.",
                ))

    def _acquire_evidence(self, topic: str) -> list[Evidence]:
        assert self.research_provider is not None
        raw_items = itertools.islice(self.research_provider.search(topic), 20)
        normalized: list[Evidence] = []
        for raw in raw_items:
            if isinstance(raw, Evidence):
                data = raw.to_dict()
                data["provider"] = data.get("provider") or type(self.research_provider).__name__
                data["topic"] = data.get("topic") or topic
                data["trust"] = "untrusted_external_data"
                data["independently_corroborated"] = False
                item = Evidence(**data)
            elif isinstance(raw, dict):
                excerpt = str(raw.get("excerpt") or raw.get("summary") or "").strip()
                item = Evidence(
                    title=str(raw.get("title") or "").strip(),
                    source_id=str(raw.get("source_id") or raw.get("url") or "").strip(),
                    provider=str(raw.get("provider") or type(self.research_provider).__name__),
                    publisher=raw.get("publisher"), retrieved_at=str(raw.get("retrieved_at") or
                        datetime.now(timezone.utc).isoformat()), excerpt=excerpt,
                    confidence=float(raw.get("confidence", 0.5)),
                    independently_corroborated=False,
                    classification=str(raw.get("classification", "fact")), topic=topic,
                )
            else:
                continue
            if not item.excerpt.strip():
                continue
            item.source_id = str(redact(item.source_id))
            item.title = str(redact(item.title))
            item.provider = str(redact(item.provider))
            item.publisher = str(redact(item.publisher)) if item.publisher is not None else None
            item.excerpt = str(redact(item.excerpt))
            item.content_hash = hashlib.sha256(item.excerpt.encode("utf-8")).hexdigest()
            normalized.append(item)
        by_hash: dict[str, set[str]] = {}
        for item in normalized:
            by_hash.setdefault(item.content_hash or "", set()).add(item.source_id)
        for item in normalized:
            item.independently_corroborated = len(by_hash.get(item.content_hash or "", set())) > 1
        return normalized

    def _criteria_can_be_checked(self, goal: Goal) -> bool:
        checks = {item.check for item in goal.acceptance_criteria}
        if "tool_succeeded" in checks and not goal.steps:
            return False
        if any(check in {"output_contains", "output_equals"} for check in checks) and not goal.steps:
            return False
        if "evidence_present" in checks and not goal.research_topic:
            return False
        return True

    def _execute_and_verify(self, goal: Goal, deadline: float) -> None:
        while True:
            if goal.iteration_count >= goal.iteration_budget:
                self._block(goal, "iteration_budget_exhausted", "Goal iteration budget was exhausted.")
                return
            if time.monotonic() >= deadline:
                self._block(goal, "time_budget_exhausted", "Goal time budget was exhausted.")
                return
            goal.iteration_count += 1
            failed = self._execute_steps(goal, deadline)
            if failed:
                return
            self._transition(goal, GoalState.VERIFYING, "verification_started")
            goal.verification_results = self._verify(goal)
            self._event(goal, "verification_finished", {
                "passed": all(result.passed for result in goal.verification_results),
                "checks": [{"criterion": r.criterion, "passed": r.passed, "method": r.method}
                           for r in goal.verification_results],
            })
            if goal.verification_results and all(result.passed for result in goal.verification_results):
                goal.final_result = "All acceptance criteria passed deterministic checks."
                for gap in goal.capability_gaps:
                    if gap.availability == "available":
                        used = gap.capability == "research" and bool(goal.evidence)
                        used = used or any(step.tool_name == gap.capability for step in goal.steps)
                        if used:
                            gap.tested = True
                            gap.evidence += " Goal acceptance checks passed after this capability was used."
                if goal.evidence:
                    self.repository.save_verified_evidence(goal.goal_id, goal.evidence)
                    self._event(goal, "verified_knowledge_persisted", {"count": len(goal.evidence)})
                self._transition(goal, GoalState.COMPLETED, "goal_completed")
                return
            if not goal.steps or goal.retries_used >= goal.max_retries:
                self._block(goal, "verification_failed", "Acceptance checks failed and no bounded retry remains.")
                return
            unsafe_steps = []
            for step in goal.steps:
                entry = self.tool_registry.get(step.tool_name)
                if entry is None:
                    unsafe_steps.append({
                        "step_id": step.step_id, "tool": step.tool_name,
                        "reason": "tool_not_registered",
                    })
                    continue
                definition, _ = entry
                if definition.retry_safe is not True:
                    unsafe_steps.append({
                        "step_id": step.step_id, "tool": definition.name,
                        "reason": "retry_safe_not_declared",
                    })
                if definition.risk_tier is not RiskTier.READ_ONLY:
                    unsafe_steps.append({
                        "step_id": step.step_id, "tool": definition.name,
                        "reason": "risk_tier_not_read_only",
                    })
                if step.attempts > goal.max_retries:
                    unsafe_steps.append({
                        "step_id": step.step_id, "tool": definition.name,
                        "reason": "retry_capacity_exhausted",
                    })
            if unsafe_steps:
                self._event(goal, "retry_safety_guard_blocked", {
                    "retry": goal.retries_used,
                    "steps": unsafe_steps,
                    "action": "blocked_before_replay",
                })
                self._block(
                    goal, "retry_not_safe",
                    "The full plan was not replayed because at least one step failed retry safety preflight.",
                )
                return
            goal.retries_used += 1
            self._transition(goal, GoalState.EXECUTING, "bounded_retry", {
                "retry": goal.retries_used, "max_retries_per_step": goal.max_retries,
                "diagnosis": "Acceptance check failure; rerunning registered tool step(s).",
            })

    def _execute_steps(self, goal: Goal, deadline: float) -> bool:
        goal.outputs = []
        for step in goal.steps:
            entry = self.tool_registry.get(step.tool_name)
            if entry is None:
                self._block(goal, "execution_capability_missing", f"Tool is no longer registered: {step.tool_name}")
                return True
            definition, function = entry
            decision = self.policy.assess(definition)
            if not decision.allowed:
                self._block(goal, "policy_denied", decision.reason)
                return True
            if decision.requires_approval:
                grant_key = (goal.goal_id, step.step_id)
                if self._approval_grants.get(grant_key) != self._step_fingerprint(goal, step, definition):
                    self._request_approval(
                        goal, step, definition,
                        "Risk declaration changed or a new approval is required for this action attempt.",
                    )
                    return True
            args = self._runtime_inputs.get((goal.goal_id, step.step_id))
            if args is None:
                self._block(goal, "invocation_inputs_unavailable", "Tool inputs are not available in this process.")
                return True
            if step.attempts > goal.max_retries:
                self._block(goal, "retry_limit_reached", f"Retry limit reached for {step.tool_name}.")
                return True
            if time.monotonic() >= deadline:
                self._block(goal, "time_budget_exhausted", "Goal time budget was exhausted during execution.")
                return True
            step.attempts += 1
            step.status = "running"
            goal.current_step_id = step.step_id
            self._persist(goal)
            started = datetime.now(timezone.utc).isoformat()
            try:
                self.tool_registry.validate_inputs(step.tool_name, args)
                if decision.requires_approval:
                    self._approval_grants.pop((goal.goal_id, step.step_id), None)
                output = function(**args)
                rendered = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False, default=str)
                step.status = "succeeded"
                step.last_error = None
                step.output_summary = safe_summary(rendered, 500)
                goal.outputs.append(rendered)
                result_summary = safe_summary(rendered, 500)
                outcome = "succeeded"
            except Exception as exc:
                rendered = ""
                step.status = "failed"
                step.last_error = safe_summary(f"{type(exc).__name__}: {exc}", 500)
                step.output_summary = None
                result_summary = step.last_error
                outcome = "failed"
            finished = datetime.now(timezone.utc).isoformat()
            self.repository.add_action(
                goal_id=goal.goal_id, step_id=step.step_id, tool_name=step.tool_name,
                input_summary="Invocation inputs omitted by policy.", risk_tier=definition.risk_tier.value,
                result_summary=result_summary, outcome=outcome,
                started_at=started, finished_at=finished,
            )
            self._event(goal, "tool_action_recorded", {
                "step_id": step.step_id, "tool": step.tool_name,
                "risk_tier": definition.risk_tier.value, "outcome": outcome,
                "attempt": step.attempts, "result": result_summary,
            })
            if outcome == "failed" and step.attempts > goal.max_retries:
                self._block(goal, "execution_failed", f"Tool failed after {step.attempts} bounded attempts.")
                return True
            self._persist(goal)
        return False

    def _verify(self, goal: Goal) -> list[VerificationResult]:
        results = []
        combined = "\n".join(goal.outputs)
        for criterion in goal.acceptance_criteria:
            if criterion.check == "tool_succeeded":
                passed = bool(goal.steps) and all(step.status == "succeeded" for step in goal.steps)
                method, details = "registered_tool_status", "All planned registered tool steps succeeded." if passed else "At least one planned tool step did not succeed."
            elif criterion.check == "output_contains":
                passed = (criterion.expected or "") in combined
                method, details = "deterministic_substring_check", "Expected text was present." if passed else "Expected text was absent from tool output."
            elif criterion.check == "output_equals":
                passed = combined == (criterion.expected or "")
                method, details = "deterministic_exact_match", "Output matched exactly." if passed else "Output did not match the expected value."
            else:  # evidence_present
                passed = any(item.source_id.strip() and item.title.strip() and item.excerpt.strip() for item in goal.evidence)
                method, details = "source_provenance_check", "Source identifier, title, and excerpt are present." if passed else "No complete source-backed evidence item is available."
            results.append(VerificationResult(criterion.description, passed, method, details))
        return results

    def _attach_cognitive_assessment(self, goal: Goal) -> None:
        """Reuse the existing shadow orchestrator for context; its plan cannot execute."""
        try:
            from core.orchestrator.cognitive_orchestrator import CognitiveOrchestrator
            result = CognitiveOrchestrator().analyze(goal.clarified_intent)
            goal.cognitive_assessment = {
                "intent": result.assessment.intent,
                "complexity": result.assessment.complexity,
                "risk_level": result.assessment.risk_level,
                "confidence": result.assessment.confidence,
                "orchestrator_status": result.status,
                "execution_allowed": result.plan.execution_allowed,
            }
        except Exception as exc:
            self._event(goal, "cognitive_orchestrator_unavailable", {"reason": type(exc).__name__})

    def _step_fingerprint(self, goal: Goal, step: ToolStep, definition: ToolDefinition) -> str:
        action = {
            "definition": definition.to_dict(),
            "inputs": self._runtime_inputs.get((goal.goal_id, step.step_id), {}),
        }
        rendered = json.dumps(action, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(rendered.encode("utf-8")).hexdigest()

    def _transition(self, goal: Goal, new_state: GoalState, event_type: str, details: Any = None) -> None:
        if new_state not in _TRANSITIONS[goal.state]:
            raise ValueError(f"Invalid goal transition: {goal.state.value} -> {new_state.value}")
        previous = goal.state
        goal.state = new_state
        goal.updated_at = datetime.now(timezone.utc).isoformat()
        self.repository.save_goal(goal)
        self.repository.add_event(goal.goal_id, event_type, previous.value, new_state.value, details or {})

    def _event(self, goal: Goal, event_type: str, details: Any = None) -> None:
        self.repository.add_event(goal.goal_id, event_type, goal.state.value, goal.state.value, details or {})
        self._persist(goal)

    def _persist(self, goal: Goal) -> None:
        goal.updated_at = datetime.now(timezone.utc).isoformat()
        self.repository.save_goal(goal)

    def _block(self, goal: Goal, reason: str, message: str) -> Goal:
        self._approval_challenge_delivery.pop(goal.goal_id, None)
        for key in [key for key in self._approval_challenges if key[0] == goal.goal_id]:
            self._approval_challenges.pop(key, None)
        for key in [key for key in self._approval_grants if key[0] == goal.goal_id]:
            self._approval_grants.pop(key, None)
        goal.blocker_reason = reason
        goal.final_result = None
        if goal.state != GoalState.BLOCKED:
            self._transition(goal, GoalState.BLOCKED, "goal_blocked", {"reason": reason, "message": message})
        else:
            self._persist(goal)
        return goal
