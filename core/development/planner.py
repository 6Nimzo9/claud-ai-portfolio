from .models import DevelopmentPlan, ExecutionStep


class DevelopmentPlanner:
    def create_plan(self, task):
        phases = [
            (
                "understand",
                "Analyze the objective, requirements and constraints."
            ),
            (
                "requirements",
                "Define functional and non-functional requirements."
            ),
            (
                "architecture",
                "Design components, interfaces and data structures."
            ),
            (
                "implementation",
                "Develop the required source code in an isolated workspace."
            ),
            (
                "testing",
                "Create and execute tests against the implementation."
            ),
            (
                "review",
                "Review correctness, security and requirement coverage."
            ),
            (
                "delivery",
                "Prepare verified artifacts and a development report."
            )
        ]

        steps = [
            ExecutionStep(
                sequence=index,
                name=name,
                description=description
            )
            for index, (name, description) in enumerate(phases, 1)
        ]

        return DevelopmentPlan(
            task_id=task.task_id,
            objective=task.objective,
            steps=steps
        )
