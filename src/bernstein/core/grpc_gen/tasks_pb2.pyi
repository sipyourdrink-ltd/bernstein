import datetime

from google.protobuf import timestamp_pb2 as _timestamp_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class TaskStatus(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    TASK_STATUS_UNSPECIFIED: _ClassVar[TaskStatus]
    TASK_STATUS_OPEN: _ClassVar[TaskStatus]
    TASK_STATUS_CLAIMED: _ClassVar[TaskStatus]
    TASK_STATUS_IN_PROGRESS: _ClassVar[TaskStatus]
    TASK_STATUS_DONE: _ClassVar[TaskStatus]
    TASK_STATUS_FAILED: _ClassVar[TaskStatus]
    TASK_STATUS_BLOCKED: _ClassVar[TaskStatus]
    TASK_STATUS_CANCELLED: _ClassVar[TaskStatus]
    TASK_STATUS_ORPHANED: _ClassVar[TaskStatus]
TASK_STATUS_UNSPECIFIED: TaskStatus
TASK_STATUS_OPEN: TaskStatus
TASK_STATUS_CLAIMED: TaskStatus
TASK_STATUS_IN_PROGRESS: TaskStatus
TASK_STATUS_DONE: TaskStatus
TASK_STATUS_FAILED: TaskStatus
TASK_STATUS_BLOCKED: TaskStatus
TASK_STATUS_CANCELLED: TaskStatus
TASK_STATUS_ORPHANED: TaskStatus

class Task(_message.Message):
    __slots__ = ("id", "goal", "role", "status", "assigned_agent", "assigned_node", "priority", "model", "effort", "created_at", "updated_at", "metadata")
    class MetadataEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...
    ID_FIELD_NUMBER: _ClassVar[int]
    GOAL_FIELD_NUMBER: _ClassVar[int]
    ROLE_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    ASSIGNED_AGENT_FIELD_NUMBER: _ClassVar[int]
    ASSIGNED_NODE_FIELD_NUMBER: _ClassVar[int]
    PRIORITY_FIELD_NUMBER: _ClassVar[int]
    MODEL_FIELD_NUMBER: _ClassVar[int]
    EFFORT_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    UPDATED_AT_FIELD_NUMBER: _ClassVar[int]
    METADATA_FIELD_NUMBER: _ClassVar[int]
    id: str
    goal: str
    role: str
    status: TaskStatus
    assigned_agent: str
    assigned_node: str
    priority: int
    model: str
    effort: str
    created_at: _timestamp_pb2.Timestamp
    updated_at: _timestamp_pb2.Timestamp
    metadata: _containers.ScalarMap[str, str]
    def __init__(self, id: _Optional[str] = ..., goal: _Optional[str] = ..., role: _Optional[str] = ..., status: _Optional[_Union[TaskStatus, str]] = ..., assigned_agent: _Optional[str] = ..., assigned_node: _Optional[str] = ..., priority: _Optional[int] = ..., model: _Optional[str] = ..., effort: _Optional[str] = ..., created_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., updated_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., metadata: _Optional[_Mapping[str, str]] = ...) -> None: ...

class CreateTaskRequest(_message.Message):
    __slots__ = ("goal", "role", "priority", "model", "effort", "metadata")
    class MetadataEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...
    GOAL_FIELD_NUMBER: _ClassVar[int]
    ROLE_FIELD_NUMBER: _ClassVar[int]
    PRIORITY_FIELD_NUMBER: _ClassVar[int]
    MODEL_FIELD_NUMBER: _ClassVar[int]
    EFFORT_FIELD_NUMBER: _ClassVar[int]
    METADATA_FIELD_NUMBER: _ClassVar[int]
    goal: str
    role: str
    priority: int
    model: str
    effort: str
    metadata: _containers.ScalarMap[str, str]
    def __init__(self, goal: _Optional[str] = ..., role: _Optional[str] = ..., priority: _Optional[int] = ..., model: _Optional[str] = ..., effort: _Optional[str] = ..., metadata: _Optional[_Mapping[str, str]] = ...) -> None: ...

class ClaimTaskRequest(_message.Message):
    __slots__ = ("task_id", "agent_id", "node_id")
    TASK_ID_FIELD_NUMBER: _ClassVar[int]
    AGENT_ID_FIELD_NUMBER: _ClassVar[int]
    NODE_ID_FIELD_NUMBER: _ClassVar[int]
    task_id: str
    agent_id: str
    node_id: str
    def __init__(self, task_id: _Optional[str] = ..., agent_id: _Optional[str] = ..., node_id: _Optional[str] = ...) -> None: ...

class CompleteTaskRequest(_message.Message):
    __slots__ = ("task_id", "result_summary", "files_changed")
    TASK_ID_FIELD_NUMBER: _ClassVar[int]
    RESULT_SUMMARY_FIELD_NUMBER: _ClassVar[int]
    FILES_CHANGED_FIELD_NUMBER: _ClassVar[int]
    task_id: str
    result_summary: str
    files_changed: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, task_id: _Optional[str] = ..., result_summary: _Optional[str] = ..., files_changed: _Optional[_Iterable[str]] = ...) -> None: ...

class FailTaskRequest(_message.Message):
    __slots__ = ("task_id", "error", "retryable")
    TASK_ID_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    RETRYABLE_FIELD_NUMBER: _ClassVar[int]
    task_id: str
    error: str
    retryable: bool
    def __init__(self, task_id: _Optional[str] = ..., error: _Optional[str] = ..., retryable: _Optional[bool] = ...) -> None: ...

class ProgressRequest(_message.Message):
    __slots__ = ("task_id", "files_changed", "tests_passing", "errors", "progress_pct")
    TASK_ID_FIELD_NUMBER: _ClassVar[int]
    FILES_CHANGED_FIELD_NUMBER: _ClassVar[int]
    TESTS_PASSING_FIELD_NUMBER: _ClassVar[int]
    ERRORS_FIELD_NUMBER: _ClassVar[int]
    PROGRESS_PCT_FIELD_NUMBER: _ClassVar[int]
    task_id: str
    files_changed: _containers.RepeatedScalarFieldContainer[str]
    tests_passing: bool
    errors: _containers.RepeatedScalarFieldContainer[str]
    progress_pct: int
    def __init__(self, task_id: _Optional[str] = ..., files_changed: _Optional[_Iterable[str]] = ..., tests_passing: _Optional[bool] = ..., errors: _Optional[_Iterable[str]] = ..., progress_pct: _Optional[int] = ...) -> None: ...

class ProgressResponse(_message.Message):
    __slots__ = ("acknowledged",)
    ACKNOWLEDGED_FIELD_NUMBER: _ClassVar[int]
    acknowledged: bool
    def __init__(self, acknowledged: _Optional[bool] = ...) -> None: ...

class ListTasksRequest(_message.Message):
    __slots__ = ("status_filter", "role_filter", "node_filter", "limit")
    STATUS_FILTER_FIELD_NUMBER: _ClassVar[int]
    ROLE_FILTER_FIELD_NUMBER: _ClassVar[int]
    NODE_FILTER_FIELD_NUMBER: _ClassVar[int]
    LIMIT_FIELD_NUMBER: _ClassVar[int]
    status_filter: TaskStatus
    role_filter: str
    node_filter: str
    limit: int
    def __init__(self, status_filter: _Optional[_Union[TaskStatus, str]] = ..., role_filter: _Optional[str] = ..., node_filter: _Optional[str] = ..., limit: _Optional[int] = ...) -> None: ...

class ListTasksResponse(_message.Message):
    __slots__ = ("tasks", "total_count")
    TASKS_FIELD_NUMBER: _ClassVar[int]
    TOTAL_COUNT_FIELD_NUMBER: _ClassVar[int]
    tasks: _containers.RepeatedCompositeFieldContainer[Task]
    total_count: int
    def __init__(self, tasks: _Optional[_Iterable[_Union[Task, _Mapping]]] = ..., total_count: _Optional[int] = ...) -> None: ...

class GetTaskRequest(_message.Message):
    __slots__ = ("task_id",)
    TASK_ID_FIELD_NUMBER: _ClassVar[int]
    task_id: str
    def __init__(self, task_id: _Optional[str] = ...) -> None: ...

class TaskResponse(_message.Message):
    __slots__ = ("task",)
    TASK_FIELD_NUMBER: _ClassVar[int]
    task: Task
    def __init__(self, task: _Optional[_Union[Task, _Mapping]] = ...) -> None: ...

class StreamTasksRequest(_message.Message):
    __slots__ = ("status_filter", "node_filter")
    STATUS_FILTER_FIELD_NUMBER: _ClassVar[int]
    NODE_FILTER_FIELD_NUMBER: _ClassVar[int]
    status_filter: _containers.RepeatedScalarFieldContainer[TaskStatus]
    node_filter: str
    def __init__(self, status_filter: _Optional[_Iterable[_Union[TaskStatus, str]]] = ..., node_filter: _Optional[str] = ...) -> None: ...

class TaskEvent(_message.Message):
    __slots__ = ("type", "task", "timestamp")
    class EventType(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
        __slots__ = ()
        EVENT_TYPE_UNSPECIFIED: _ClassVar[TaskEvent.EventType]
        CREATED: _ClassVar[TaskEvent.EventType]
        CLAIMED: _ClassVar[TaskEvent.EventType]
        PROGRESS: _ClassVar[TaskEvent.EventType]
        COMPLETED: _ClassVar[TaskEvent.EventType]
        FAILED: _ClassVar[TaskEvent.EventType]
        CANCELLED: _ClassVar[TaskEvent.EventType]
    EVENT_TYPE_UNSPECIFIED: TaskEvent.EventType
    CREATED: TaskEvent.EventType
    CLAIMED: TaskEvent.EventType
    PROGRESS: TaskEvent.EventType
    COMPLETED: TaskEvent.EventType
    FAILED: TaskEvent.EventType
    CANCELLED: TaskEvent.EventType
    TYPE_FIELD_NUMBER: _ClassVar[int]
    TASK_FIELD_NUMBER: _ClassVar[int]
    TIMESTAMP_FIELD_NUMBER: _ClassVar[int]
    type: TaskEvent.EventType
    task: Task
    timestamp: _timestamp_pb2.Timestamp
    def __init__(self, type: _Optional[_Union[TaskEvent.EventType, str]] = ..., task: _Optional[_Union[Task, _Mapping]] = ..., timestamp: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ...) -> None: ...
