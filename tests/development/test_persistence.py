import tempfile
import unittest
from pathlib import Path

from core.development import (
    DevelopmentOrchestrator,
    DevelopmentRepository
)
from core.development.models import TaskStatus


class TestDevelopmentPersistence(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "development_test.db"
        self.repository = DevelopmentRepository(self.db_path)
        self.engine = DevelopmentOrchestrator(
            repository=self.repository
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_task_survives_engine_restart(self):
        task, plan = self.engine.create_task(
            "Persistence test",
            "Verify task and plan survive engine restart."
        )

        restarted_engine = DevelopmentOrchestrator(
            repository=DevelopmentRepository(self.db_path)
        )

        restored_task = restarted_engine.get_task(task.task_id)
        restored_plan = restarted_engine.get_plan(task.task_id)

        self.assertEqual(
            restored_task.status,
            TaskStatus.AWAITING_APPROVAL
        )
        self.assertEqual(restored_plan.plan_id, plan.plan_id)
        self.assertEqual(len(restored_plan.steps), 7)

    def test_approval_is_persisted(self):
        task, _ = self.engine.create_task(
            "Approval test",
            "Verify approval persistence."
        )

        self.engine.approve_task(task.task_id)

        restarted_engine = DevelopmentOrchestrator(
            repository=DevelopmentRepository(self.db_path)
        )

        restored = restarted_engine.get_task(task.task_id)
        self.assertEqual(restored.status, TaskStatus.APPROVED)

    def test_event_history_is_persisted(self):
        task, _ = self.engine.create_task(
            "Audit test",
            "Verify development event history."
        )

        self.engine.approve_task(task.task_id)

        events = self.repository.list_events(task.task_id)
        event_types = [event["event_type"] for event in events]

        self.assertIn("status_changed", event_types)
        self.assertIn("approval_requested", event_types)
        self.assertIn("approval_granted", event_types)

    def test_rejected_task_is_persisted(self):
        task, _ = self.engine.create_task(
            "Rejection test",
            "Verify rejection persistence."
        )

        self.engine.reject_task(task.task_id)

        restarted_engine = DevelopmentOrchestrator(
            repository=DevelopmentRepository(self.db_path)
        )

        restored = restarted_engine.get_task(task.task_id)
        self.assertEqual(restored.status, TaskStatus.CANCELLED)


if __name__ == "__main__":
    unittest.main()
