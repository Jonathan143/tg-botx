"""任务管理与执行错误。"""


class TaskNotFound(LookupError):
    pass


class TaskStateError(RuntimeError):
    pass


class TaskNameConflictError(TaskStateError):
    pass


class AccountNotFoundError(TaskStateError):
    pass


class ManualRunConflict(RuntimeError):
    pass


class WorkflowVersionNotFound(TaskStateError):
    pass
