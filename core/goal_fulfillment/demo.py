"""Offline UGFE example. Run with: python -m core.goal_fulfillment.demo"""

import tempfile
from pathlib import Path

from .engine import GoalFulfillmentEngine
from .models import Evidence, RiskTier, ToolDefinition
from .repository import GoalRepository
from .tools import ToolRegistry

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class MockResearchProvider:
    def search(self, topic):
        return [Evidence(
            title="Offline acceptance check example",
            source_id="mock://ugfe-demo/acceptance-check",
            provider="UGFE demo fixture",
            publisher="Local mock provider",
            excerpt="A deterministic acceptance check compares the produced output with an explicit criterion.",
            confidence=0.95,
            topic=topic,
        )]


def main():
    with tempfile.TemporaryDirectory(prefix=".ugfe-demo-", dir=PROJECT_ROOT / "workspace") as temp:
        registry = ToolRegistry()
        registry.register(ToolDefinition(
            name="demo.format_result", description="Return a deterministic local demo result.",
            input_schema={"text": "string"}, output_description="Formatted result text.",
            risk_tier=RiskTier.READ_ONLY, permission="in-memory demonstration only",
        ), lambda text: f"acceptance passed: {text}")
        engine = GoalFulfillmentEngine(
            repository=GoalRepository(Path(temp) / "demo.sqlite3"),
            tool_registry=registry,
            research_provider=MockResearchProvider(),
        )
        goal = engine.create_goal(
            user_goal="Demonstrate a source-backed offline goal run",
            clarified_intent="Retrieve mocked evidence and verify a local result.",
            research_topic="deterministic acceptance checks",
            acceptance_criteria=[
                {"description": "A source-backed item is available", "check": "evidence_present"},
                {"description": "The output includes the pass marker", "check": "output_contains", "expected": "acceptance passed"},
            ],
            steps=[{"tool_name": "demo.format_result", "inputs": {"text": "local only"},
                    "description": "Produce the deterministic demonstration result."}],
        )
        goal = engine.run(goal.goal_id)
        report = engine.report(goal.goal_id)
        print("Goal state transitions:")
        for event in report["events"]:
            if event["from_state"] != event["to_state"]:
                print(f"  {event['from_state']} -> {event['to_state']}")
        print("Evidence:")
        for item in goal.evidence:
            print(f"  {item.title} | {item.source_id} | confidence={item.confidence:.2f} | {item.trust}")
        print(f"Final status: {goal.state.value}")
        print(f"Result: {goal.final_result or goal.blocker_reason}")


if __name__ == "__main__":
    main()
