import datetime

from google.protobuf import timestamp_pb2 as _timestamp_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class NodeStatus(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    NODE_STATUS_UNSPECIFIED: _ClassVar[NodeStatus]
    NODE_STATUS_ONLINE: _ClassVar[NodeStatus]
    NODE_STATUS_READY: _ClassVar[NodeStatus]
    NODE_STATUS_DEGRADED: _ClassVar[NodeStatus]
    NODE_STATUS_CORDONED: _ClassVar[NodeStatus]
    NODE_STATUS_DRAINING: _ClassVar[NodeStatus]
    NODE_STATUS_OFFLINE: _ClassVar[NodeStatus]
NODE_STATUS_UNSPECIFIED: NodeStatus
NODE_STATUS_ONLINE: NodeStatus
NODE_STATUS_READY: NodeStatus
NODE_STATUS_DEGRADED: NodeStatus
NODE_STATUS_CORDONED: NodeStatus
NODE_STATUS_DRAINING: NodeStatus
NODE_STATUS_OFFLINE: NodeStatus

class NodeCapacity(_message.Message):
    __slots__ = ("max_agents", "available_slots", "active_agents", "gpu_available", "supported_models")
    MAX_AGENTS_FIELD_NUMBER: _ClassVar[int]
    AVAILABLE_SLOTS_FIELD_NUMBER: _ClassVar[int]
    ACTIVE_AGENTS_FIELD_NUMBER: _ClassVar[int]
    GPU_AVAILABLE_FIELD_NUMBER: _ClassVar[int]
    SUPPORTED_MODELS_FIELD_NUMBER: _ClassVar[int]
    max_agents: int
    available_slots: int
    active_agents: int
    gpu_available: bool
    supported_models: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, max_agents: _Optional[int] = ..., available_slots: _Optional[int] = ..., active_agents: _Optional[int] = ..., gpu_available: _Optional[bool] = ..., supported_models: _Optional[_Iterable[str]] = ...) -> None: ...

class NodeInfo(_message.Message):
    __slots__ = ("id", "name", "url", "capacity", "status", "last_heartbeat", "registered_at", "labels", "cell_ids")
    class LabelsEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...
    ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    URL_FIELD_NUMBER: _ClassVar[int]
    CAPACITY_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    LAST_HEARTBEAT_FIELD_NUMBER: _ClassVar[int]
    REGISTERED_AT_FIELD_NUMBER: _ClassVar[int]
    LABELS_FIELD_NUMBER: _ClassVar[int]
    CELL_IDS_FIELD_NUMBER: _ClassVar[int]
    id: str
    name: str
    url: str
    capacity: NodeCapacity
    status: NodeStatus
    last_heartbeat: _timestamp_pb2.Timestamp
    registered_at: _timestamp_pb2.Timestamp
    labels: _containers.ScalarMap[str, str]
    cell_ids: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, id: _Optional[str] = ..., name: _Optional[str] = ..., url: _Optional[str] = ..., capacity: _Optional[_Union[NodeCapacity, _Mapping]] = ..., status: _Optional[_Union[NodeStatus, str]] = ..., last_heartbeat: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., registered_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., labels: _Optional[_Mapping[str, str]] = ..., cell_ids: _Optional[_Iterable[str]] = ...) -> None: ...

class RegisterNodeRequest(_message.Message):
    __slots__ = ("name", "url", "capacity", "labels", "cell_ids")
    class LabelsEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...
    NAME_FIELD_NUMBER: _ClassVar[int]
    URL_FIELD_NUMBER: _ClassVar[int]
    CAPACITY_FIELD_NUMBER: _ClassVar[int]
    LABELS_FIELD_NUMBER: _ClassVar[int]
    CELL_IDS_FIELD_NUMBER: _ClassVar[int]
    name: str
    url: str
    capacity: NodeCapacity
    labels: _containers.ScalarMap[str, str]
    cell_ids: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, name: _Optional[str] = ..., url: _Optional[str] = ..., capacity: _Optional[_Union[NodeCapacity, _Mapping]] = ..., labels: _Optional[_Mapping[str, str]] = ..., cell_ids: _Optional[_Iterable[str]] = ...) -> None: ...

class RegisterNodeResponse(_message.Message):
    __slots__ = ("node", "auth_token")
    NODE_FIELD_NUMBER: _ClassVar[int]
    AUTH_TOKEN_FIELD_NUMBER: _ClassVar[int]
    node: NodeInfo
    auth_token: str
    def __init__(self, node: _Optional[_Union[NodeInfo, _Mapping]] = ..., auth_token: _Optional[str] = ...) -> None: ...

class HeartbeatRequest(_message.Message):
    __slots__ = ("node_id", "capacity")
    NODE_ID_FIELD_NUMBER: _ClassVar[int]
    CAPACITY_FIELD_NUMBER: _ClassVar[int]
    node_id: str
    capacity: NodeCapacity
    def __init__(self, node_id: _Optional[str] = ..., capacity: _Optional[_Union[NodeCapacity, _Mapping]] = ...) -> None: ...

class HeartbeatResponse(_message.Message):
    __slots__ = ("acknowledged", "node")
    ACKNOWLEDGED_FIELD_NUMBER: _ClassVar[int]
    NODE_FIELD_NUMBER: _ClassVar[int]
    acknowledged: bool
    node: NodeInfo
    def __init__(self, acknowledged: _Optional[bool] = ..., node: _Optional[_Union[NodeInfo, _Mapping]] = ...) -> None: ...

class UnregisterNodeRequest(_message.Message):
    __slots__ = ("node_id",)
    NODE_ID_FIELD_NUMBER: _ClassVar[int]
    node_id: str
    def __init__(self, node_id: _Optional[str] = ...) -> None: ...

class UnregisterNodeResponse(_message.Message):
    __slots__ = ("removed",)
    REMOVED_FIELD_NUMBER: _ClassVar[int]
    removed: bool
    def __init__(self, removed: _Optional[bool] = ...) -> None: ...

class CordonRequest(_message.Message):
    __slots__ = ("node_id",)
    NODE_ID_FIELD_NUMBER: _ClassVar[int]
    node_id: str
    def __init__(self, node_id: _Optional[str] = ...) -> None: ...

class UncordonRequest(_message.Message):
    __slots__ = ("node_id",)
    NODE_ID_FIELD_NUMBER: _ClassVar[int]
    node_id: str
    def __init__(self, node_id: _Optional[str] = ...) -> None: ...

class DrainRequest(_message.Message):
    __slots__ = ("node_id",)
    NODE_ID_FIELD_NUMBER: _ClassVar[int]
    node_id: str
    def __init__(self, node_id: _Optional[str] = ...) -> None: ...

class NodeStatusResponse(_message.Message):
    __slots__ = ("node_id", "status")
    NODE_ID_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    node_id: str
    status: NodeStatus
    def __init__(self, node_id: _Optional[str] = ..., status: _Optional[_Union[NodeStatus, str]] = ...) -> None: ...

class ListNodesRequest(_message.Message):
    __slots__ = ("status_filter",)
    STATUS_FILTER_FIELD_NUMBER: _ClassVar[int]
    status_filter: NodeStatus
    def __init__(self, status_filter: _Optional[_Union[NodeStatus, str]] = ...) -> None: ...

class ListNodesResponse(_message.Message):
    __slots__ = ("nodes",)
    NODES_FIELD_NUMBER: _ClassVar[int]
    nodes: _containers.RepeatedCompositeFieldContainer[NodeInfo]
    def __init__(self, nodes: _Optional[_Iterable[_Union[NodeInfo, _Mapping]]] = ...) -> None: ...

class ClusterStatusRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class ClusterStatusResponse(_message.Message):
    __slots__ = ("topology", "total_nodes", "online_nodes", "offline_nodes", "total_capacity", "available_slots", "active_agents", "nodes")
    TOPOLOGY_FIELD_NUMBER: _ClassVar[int]
    TOTAL_NODES_FIELD_NUMBER: _ClassVar[int]
    ONLINE_NODES_FIELD_NUMBER: _ClassVar[int]
    OFFLINE_NODES_FIELD_NUMBER: _ClassVar[int]
    TOTAL_CAPACITY_FIELD_NUMBER: _ClassVar[int]
    AVAILABLE_SLOTS_FIELD_NUMBER: _ClassVar[int]
    ACTIVE_AGENTS_FIELD_NUMBER: _ClassVar[int]
    NODES_FIELD_NUMBER: _ClassVar[int]
    topology: str
    total_nodes: int
    online_nodes: int
    offline_nodes: int
    total_capacity: int
    available_slots: int
    active_agents: int
    nodes: _containers.RepeatedCompositeFieldContainer[NodeInfo]
    def __init__(self, topology: _Optional[str] = ..., total_nodes: _Optional[int] = ..., online_nodes: _Optional[int] = ..., offline_nodes: _Optional[int] = ..., total_capacity: _Optional[int] = ..., available_slots: _Optional[int] = ..., active_agents: _Optional[int] = ..., nodes: _Optional[_Iterable[_Union[NodeInfo, _Mapping]]] = ...) -> None: ...

class StealTasksRequest(_message.Message):
    __slots__ = ("queue_depths",)
    class QueueDepthsEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: int
        def __init__(self, key: _Optional[str] = ..., value: _Optional[int] = ...) -> None: ...
    QUEUE_DEPTHS_FIELD_NUMBER: _ClassVar[int]
    queue_depths: _containers.ScalarMap[str, int]
    def __init__(self, queue_depths: _Optional[_Mapping[str, int]] = ...) -> None: ...

class StealAction(_message.Message):
    __slots__ = ("donor_node_id", "receiver_node_id", "task_ids")
    DONOR_NODE_ID_FIELD_NUMBER: _ClassVar[int]
    RECEIVER_NODE_ID_FIELD_NUMBER: _ClassVar[int]
    TASK_IDS_FIELD_NUMBER: _ClassVar[int]
    donor_node_id: str
    receiver_node_id: str
    task_ids: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, donor_node_id: _Optional[str] = ..., receiver_node_id: _Optional[str] = ..., task_ids: _Optional[_Iterable[str]] = ...) -> None: ...

class StealTasksResponse(_message.Message):
    __slots__ = ("actions", "total_stolen")
    ACTIONS_FIELD_NUMBER: _ClassVar[int]
    TOTAL_STOLEN_FIELD_NUMBER: _ClassVar[int]
    actions: _containers.RepeatedCompositeFieldContainer[StealAction]
    total_stolen: int
    def __init__(self, actions: _Optional[_Iterable[_Union[StealAction, _Mapping]]] = ..., total_stolen: _Optional[int] = ...) -> None: ...

class BulletinMessage(_message.Message):
    __slots__ = ("id", "agent_id", "type", "cell", "body", "timestamp")
    ID_FIELD_NUMBER: _ClassVar[int]
    AGENT_ID_FIELD_NUMBER: _ClassVar[int]
    TYPE_FIELD_NUMBER: _ClassVar[int]
    CELL_FIELD_NUMBER: _ClassVar[int]
    BODY_FIELD_NUMBER: _ClassVar[int]
    TIMESTAMP_FIELD_NUMBER: _ClassVar[int]
    id: str
    agent_id: str
    type: str
    cell: str
    body: str
    timestamp: _timestamp_pb2.Timestamp
    def __init__(self, id: _Optional[str] = ..., agent_id: _Optional[str] = ..., type: _Optional[str] = ..., cell: _Optional[str] = ..., body: _Optional[str] = ..., timestamp: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ...) -> None: ...

class PostBulletinRequest(_message.Message):
    __slots__ = ("agent_id", "type", "cell", "body")
    AGENT_ID_FIELD_NUMBER: _ClassVar[int]
    TYPE_FIELD_NUMBER: _ClassVar[int]
    CELL_FIELD_NUMBER: _ClassVar[int]
    BODY_FIELD_NUMBER: _ClassVar[int]
    agent_id: str
    type: str
    cell: str
    body: str
    def __init__(self, agent_id: _Optional[str] = ..., type: _Optional[str] = ..., cell: _Optional[str] = ..., body: _Optional[str] = ...) -> None: ...

class PostBulletinResponse(_message.Message):
    __slots__ = ("id",)
    ID_FIELD_NUMBER: _ClassVar[int]
    id: str
    def __init__(self, id: _Optional[str] = ...) -> None: ...

class ReadBulletinsRequest(_message.Message):
    __slots__ = ("since", "type_filter", "cell_filter", "limit")
    SINCE_FIELD_NUMBER: _ClassVar[int]
    TYPE_FILTER_FIELD_NUMBER: _ClassVar[int]
    CELL_FILTER_FIELD_NUMBER: _ClassVar[int]
    LIMIT_FIELD_NUMBER: _ClassVar[int]
    since: _timestamp_pb2.Timestamp
    type_filter: str
    cell_filter: str
    limit: int
    def __init__(self, since: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., type_filter: _Optional[str] = ..., cell_filter: _Optional[str] = ..., limit: _Optional[int] = ...) -> None: ...

class ReadBulletinsResponse(_message.Message):
    __slots__ = ("messages",)
    MESSAGES_FIELD_NUMBER: _ClassVar[int]
    messages: _containers.RepeatedCompositeFieldContainer[BulletinMessage]
    def __init__(self, messages: _Optional[_Iterable[_Union[BulletinMessage, _Mapping]]] = ...) -> None: ...

class StreamBulletinsRequest(_message.Message):
    __slots__ = ("type_filter", "cell_filter")
    TYPE_FILTER_FIELD_NUMBER: _ClassVar[int]
    CELL_FILTER_FIELD_NUMBER: _ClassVar[int]
    type_filter: str
    cell_filter: str
    def __init__(self, type_filter: _Optional[str] = ..., cell_filter: _Optional[str] = ...) -> None: ...
