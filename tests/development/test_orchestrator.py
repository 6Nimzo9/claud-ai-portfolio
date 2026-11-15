import unittest

from core.development import DevelopmentOrchestrator
from core.development.models import TaskStatus
from core.development.execution_state import (
    ExecutionState, InvalidTransitionError
)


class TestDevelopmentOrchestrator(unittest.TestCase):

    def setUp(self):
        self.engine = DevelopmentOrchestrator()

    def test_create_task(self):
        task, plan = self.engine.create_task(
            "Build monitoring app",
            "Create an application that monitors local system metrics."
        )

        self.assertEqual(
            task.status, TaskStatus.AWAITING_APPROVAL
        )
        self.assertEqual(len(plan.steps), 7)
        self.assertEqual(task.task_id, plan.task_id)

    def test_approval(self):
        task, _ = self.engine.create_task(
            "Test application", "Build a test application."
        )

        approved = self.engine.approve_task(task.task_id)

        self.assertEqual(approved.status, TaskStatus.APPROVED)

    def test_rejection(self):
        task, _ = self.engine.create_task(
            "Test application", "Build a test application."
        )

        rejected = self.engine.reject_task(task.task_id)

        self.assertEqual(rejected.status, TaskStatus.CANCELLED)

    def test_empty_objective_rejected(self):
        with self.assertRaises(ValueError):
            self.engine.create_task("Test", " ")

    def test_unknown_task_rejected(self):
        with self.assertRaises(KeyError):
            self.engine.get_task("unknown")

    def test_execution_not_implemented(self):
        task, _ = self.engine.create_task(
            "Test", "Build something."
        )
        self.engine.approve_task(task.task_id)

        self.assertFalse(
            self.engine.policy.execution_enabled
        )

    def test_invalid_transition_rejected(self):
        task, _ = self.engine.create_task(
            "Test", "Build something."
        )

        with self.assertRaises(InvalidTransitionError):
            ExecutionState.transition(
                task, TaskStatus.COMPLETED
            )


if __name__ == "__main__":
    unittest.main()
