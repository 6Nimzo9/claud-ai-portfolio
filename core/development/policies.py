from dataclasses import dataclass


@dataclass(frozen=True)
class DevelopmentPolicy:
    require_approval: bool = True
    max_steps: int = 20
    execution_enabled: bool = False
    allow_core_modification: bool = False

    def validate_plan(self, plan):
        if len(plan.steps) > self.max_steps:
            raise ValueError(
                f"Plan exceeds maximum steps: {self.max_steps}"
            )

        if not plan.steps:
            raise ValueError("Development plan cannot be empty")

        if self.execution_enabled:
            raise ValueError(
                "Execution is not implemented in Development Engine v0.1"
            )

        return True
