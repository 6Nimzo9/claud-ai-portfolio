from .models import TaskSpec, TaskStatus
from .planner import DevelopmentPlanner
from .execution_state import ExecutionState
from .policies import DevelopmentPolicy


class DevelopmentOrchestrator:
    def __init__(self, policy=None, planner=None, repository=None):
        self.policy = policy or DevelopmentPolicy()
        self.planner = planner or DevelopmentPlanner()
        self.repository = repository

        self.tasks = {}
        self.plans = {}

        if self.repository is not None:
            loaded_tasks, loaded_plans = self.repository.load_all()
            self.tasks = {task.task_id: task for task in loaded_tasks}
            self.plans = loaded_plans

    def create_task(self, title, objective):
        title = title.strip() if isinstance(title, str) else ""
        objective = objective.strip() if isinstance(objective, str) else ""

        if not title:
            raise ValueError("Task title is required")
        if not objective:
            raise ValueError("Task objective is required")

        task = TaskSpec(title=title, objective=objective)
        plan = self.planner.create_plan(task)
        self.policy.validate_plan(plan)

        events = []

        old_status = task.status
        ExecutionState.transition(task, TaskStatus.PLANNED)
        events.append({
            "event_type": "status_changed",
            "from_status": old_status.value,
            "to_status": task.status.value,
            "details": {"reason": "Plan created"}
        })

        if self.policy.require_approval:
            old_status = task.status
            ExecutionState.transition(task, TaskStatus.AWAITING_APPROVAL)
            events.append({
                "event_type": "approval_requested",
                "from_status": old_status.value,
                "to_status": task.status.value,
                "details": {"reason": "Awaiting user approval"}
            })

        if self.repository is not None:
            self.repository.save_new_task(task, plan, events)

        self.tasks[task.task_id] = task
        self.plans[task.task_id] = plan
        return task, plan

    def _transition(self, task, new_status, event_type, details=None):
        old_status = task.status
        ExecutionState.transition(task, new_status)

        if self.repository is not None:
            event = {
                "event_type": event_type,
                "from_status": old_status.value,
                "to_status": task.status.value,
                "details": details or {}
            }
            try:
                self.repository.update_task(task, event)
            except Exception:
                task.status = old_status
                raise

        return task

    def approve_task(self, task_id):
        task = self.get_task(task_id)

        if not self.policy.require_approval:
            raise ValueError("Approval is not required by policy")

        return self._transition(
            task,
            TaskStatus.APPROVED,
            "approval_granted",
            {"approved": True}
        )

    def reject_task(self, task_id):
        task = self.get_task(task_id)

        return self._transition(
            task,
            TaskStatus.CANCELLED,
            "approval_rejected",
            {"approved": False}
        )

    def get_task(self, task_id):
        if task_id not in self.tasks:
            raise KeyError(f"Task not found: {task_id}")
        return self.tasks[task_id]

    def get_plan(self, task_id):
        if task_id not in self.plans:
            raise KeyError(f"Plan not found: {task_id}")
        return self.plans[task_id]

    def list_tasks(self):
        return [
            task.to_dict()
            for task in sorted(
                self.tasks.values(),
                key=lambda item: item.created_at,
                reverse=True
            )
        ]

    def report(self, task_id):
        task = self.get_task(task_id)
        plan = self.get_plan(task_id)

        report = {
            "task": task.to_dict(),
            "plan": plan.to_dict(),
            "execution_enabled": self.policy.execution_enabled
        }

        if self.repository is not None:
            report["events"] = self.repository.list_events(task_id)

        return report
