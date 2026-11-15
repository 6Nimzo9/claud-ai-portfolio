from .models import TaskStatus


TRANSITIONS = {
    TaskStatus.CREATED: {
        TaskStatus.PLANNED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED
    },
    TaskStatus.PLANNED: {
        TaskStatus.AWAITING_APPROVAL,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED
    },
    TaskStatus.AWAITING_APPROVAL: {
        TaskStatus.APPROVED,
        TaskStatus.CANCELLED
    },
    TaskStatus.APPROVED: {
        TaskStatus.IN_PROGRESS,
        TaskStatus.CANCELLED
    },
    TaskStatus.IN_PROGRESS: {
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED
    },
    TaskStatus.COMPLETED: set(),
    TaskStatus.FAILED: set(),
    TaskStatus.CANCELLED: set()
}


class InvalidTransitionError(Exception):
    pass


class ExecutionState:
    @staticmethod
    def transition(task, new_status):
        if not isinstance(new_status, TaskStatus):
            new_status = TaskStatus(new_status)

        allowed = TRANSITIONS[task.status]

        if new_status not in allowed:
            raise InvalidTransitionError(
                f"Invalid transition: {task.status.value} "
                f"-> {new_status.value}"
            )

        task.status = new_status
        return task
