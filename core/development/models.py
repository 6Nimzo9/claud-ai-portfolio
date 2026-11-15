from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from uuid import uuid4


def utc_now():
    return datetime.now(timezone.utc).isoformat()


class TaskStatus(str, Enum):
    CREATED = "created"
    PLANNED = "planned"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class TaskSpec:
    title: str
    objective: str
    task_id: str = field(default_factory=lambda: str(uuid4()))
    status: TaskStatus = TaskStatus.CREATED
    created_at: str = field(default_factory=utc_now)

    def to_dict(self):
        return {
            "task_id": self.task_id,
            "title": self.title,
            "objective": self.objective,
            "status": self.status.value,
            "created_at": self.created_at
        }


@dataclass
class ExecutionStep:
    sequence: int
    name: str
    description: str
    step_id: str = field(default_factory=lambda: str(uuid4()))
    status: str = "pending"

    def to_dict(self):
        return {
            "step_id": self.step_id,
            "sequence": self.sequence,
            "name": self.name,
            "description": self.description,
            "status": self.status
        }


@dataclass
class DevelopmentPlan:
    task_id: str
    objective: str
    steps: list
    plan_id: str = field(default_factory=lambda: str(uuid4()))
    created_at: str = field(default_factory=utc_now)
    version: str = "0.1.0"

    def to_dict(self):
        return {
            "plan_id": self.plan_id,
            "task_id": self.task_id,
            "objective": self.objective,
            "version": self.version,
            "created_at": self.created_at,
            "steps": [step.to_dict() for step in self.steps]
        }
