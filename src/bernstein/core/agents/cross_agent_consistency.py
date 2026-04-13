"""Cross-agent consistency checker for multi-agent feature implementations.

When multiple agents implement different parts of the same feature (e.g.
backend agent creates the API, frontend agent creates the UI), this module
verifies that their contracts are compatible:

- API endpoint paths and HTTP methods match across producers and consumers
- Request/response schemas are field-compatible (no missing required fields)
- Error codes referenced by consumers are all produced by the backend

Usage example::

    from bernstein.core.agents.cross_agent_consistency import (
        AgentImplementation,
        ApiContract,
        check_consistency,
    )

    backend = AgentImplementation(
        agent_id="backend-001",
        role="backend",
        contracts=[
            ApiContract(
                endpoint="/api/items",
                method="POST",
                request_fields={"name": "str", "quantity": "int"},
                response_fields={"id": "str", "name": "str"},
                error_codes=[400, 422, 500],
            ),
        ],
    )

    frontend = AgentImplementation(
        agent_id="frontend-001",
        role="frontend",
        contracts=[
            ApiContract(
                endpoint="/api/items",
                method="POST",
                request_fields={"name": "str", "quantity": "int"},
                response_fields={"id": "str"},
                error_codes=[400, 422],
            ),
        ],
    )

    report = check_consistency([backend, frontend])
    assert report.is_consistent
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


class ConsistencyIssueType(Enum):
    """Category of a cross-agent consistency issue."""

    ENDPOINT_MISSING = "endpoint_missing"
    METHOD_MISMATCH = "method_mismatch"
    MISSING_REQUEST_FIELD = "missing_request_field"
    MISSING_RESPONSE_FIELD = "missing_response_field"
    FIELD_TYPE_MISMATCH = "field_type_mismatch"
    UNHANDLED_ERROR_CODE = "unhandled_error_code"


@dataclass(frozen=True)
class ApiContract:
    """A single API contract declared by an agent implementation.

    Attributes:
        endpoint: URL path (e.g. ``/api/items``).
        method: HTTP method in uppercase (e.g. ``POST``).
        request_fields: Mapping of field name → type hint string for the
            request body.  Empty dict means no body / body not specified.
        response_fields: Mapping of field name → type hint string for the
            success response body.  Empty dict means response not specified.
        error_codes: HTTP error status codes that this contract declares
            (producers) or handles (consumers).
        role: Optional role hint for the declaring agent (``"backend"`` /
            ``"frontend"``).  Used only for reporting.
    """

    endpoint: str
    method: str
    request_fields: dict[str, str] = field(default_factory=dict)
    response_fields: dict[str, str] = field(default_factory=dict)
    error_codes: list[int] = field(default_factory=list)
    role: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "method", self.method.upper())


@dataclass
class AgentImplementation:
    """A set of API contracts declared by a single agent.

    Attributes:
        agent_id: Unique identifier for the agent / task.
        role: Agent role (e.g. ``backend``, ``frontend``, ``qa``).
        contracts: API contracts this agent declares (produces or consumes).
    """

    agent_id: str
    role: str
    contracts: list[ApiContract] = field(default_factory=lambda: list[ApiContract]())


@dataclass(frozen=True)
class ConsistencyIssue:
    """A single consistency problem found across agent implementations.

    Attributes:
        issue_type: Category of the problem.
        endpoint: API endpoint the issue relates to.
        method: HTTP method the issue relates to.
        description: Human-readable explanation.
        agents_involved: IDs of agents whose contracts conflict.
    """

    issue_type: ConsistencyIssueType
    endpoint: str
    method: str
    description: str
    agents_involved: list[str] = field(default_factory=lambda: list[str]())


@dataclass
class ConsistencyReport:
    """Result of a cross-agent consistency check.

    Attributes:
        issues: All consistency issues found.
        is_consistent: True when no issues were detected.
        checked_endpoints: Number of distinct endpoint+method pairs examined.
    """

    issues: list[ConsistencyIssue] = field(default_factory=lambda: list[ConsistencyIssue]())
    checked_endpoints: int = 0

    @property
    def is_consistent(self) -> bool:
        """True when no consistency issues were found."""
        return len(self.issues) == 0

    def issues_by_type(self, issue_type: ConsistencyIssueType) -> list[ConsistencyIssue]:
        """Return all issues of a specific type.

        Args:
            issue_type: The issue category to filter by.

        Returns:
            Filtered list of :class:`ConsistencyIssue`.
        """
        return [i for i in self.issues if i.issue_type == issue_type]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _check_field_set(
    consumer_fields: dict[str, str],
    producer_fields: dict[str, str],
    direction: str,
    missing_type: ConsistencyIssueType,
    endpoint: str,
    method: str,
    producer_id: str,
    consumer_id: str,
    agents: list[str],
) -> list[ConsistencyIssue]:
    """Check one direction (request or response) of field compatibility.

    Args:
        consumer_fields: Fields the consumer declares.
        producer_fields: Fields the producer declares.
        direction: 'request' or 'response' (for error messages).
        missing_type: Issue type for missing fields.
        endpoint: API endpoint path.
        method: HTTP method.
        producer_id: Agent ID of the producer.
        consumer_id: Agent ID of the consumer.
        agents: List of agent IDs involved.

    Returns:
        List of consistency issues.
    """
    issues: list[ConsistencyIssue] = []
    verb = "sends" if direction == "request" else "reads"
    src_verb = "declares" if direction == "request" else "returns"
    dst_verb = "expects"

    for field_name, consumer_type in consumer_fields.items():
        if field_name not in producer_fields:
            issues.append(
                ConsistencyIssue(
                    issue_type=missing_type,
                    endpoint=endpoint,
                    method=method,
                    description=(
                        f"Consumer '{consumer_id}' {verb} field '{field_name}' "
                        f"in {direction} body, but producer '{producer_id}' does not declare it"
                    ),
                    agents_involved=agents,
                )
            )
        else:
            producer_type = producer_fields[field_name]
            if producer_type and consumer_type and producer_type != consumer_type:
                issues.append(
                    ConsistencyIssue(
                        issue_type=ConsistencyIssueType.FIELD_TYPE_MISMATCH,
                        endpoint=endpoint,
                        method=method,
                        description=(
                            f"{direction.capitalize()} field '{field_name}' type mismatch: "
                            f"producer '{producer_id}' {src_verb} '{producer_type}', "
                            f"consumer '{consumer_id}' {dst_verb} '{consumer_type}'"
                        ),
                        agents_involved=agents,
                    )
                )
    return issues


def _check_schema_compatibility(
    endpoint: str,
    method: str,
    producer: ApiContract,
    consumer: ApiContract,
    producer_id: str,
    consumer_id: str,
) -> list[ConsistencyIssue]:
    """Compare schemas between a producer and consumer for the same endpoint.

    Args:
        endpoint: API endpoint path.
        method: HTTP method.
        producer: Contract from the backend / producing agent.
        consumer: Contract from the frontend / consuming agent.
        producer_id: Agent ID of the producer.
        consumer_id: Agent ID of the consumer.

    Returns:
        List of :class:`ConsistencyIssue` for field-level mismatches.
    """
    agents = [producer_id, consumer_id]

    issues = _check_field_set(
        consumer.request_fields,
        producer.request_fields,
        "request",
        ConsistencyIssueType.MISSING_REQUEST_FIELD,
        endpoint,
        method,
        producer_id,
        consumer_id,
        agents,
    )
    issues.extend(
        _check_field_set(
            consumer.response_fields,
            producer.response_fields,
            "response",
            ConsistencyIssueType.MISSING_RESPONSE_FIELD,
            endpoint,
            method,
            producer_id,
            consumer_id,
            agents,
        )
    )

    return issues


def _check_error_codes(
    endpoint: str,
    method: str,
    producer: ApiContract,
    consumer: ApiContract,
    producer_id: str,
    consumer_id: str,
) -> list[ConsistencyIssue]:
    """Find error codes handled by the consumer but not declared by the producer.

    Args:
        endpoint: API endpoint path.
        method: HTTP method.
        producer: Contract from the producing agent.
        consumer: Contract from the consuming agent.
        producer_id: Agent ID of the producer.
        consumer_id: Agent ID of the consumer.

    Returns:
        Issues for any error codes the consumer handles that the producer never emits.
    """
    if not producer.error_codes or not consumer.error_codes:
        return []

    producer_codes = set(producer.error_codes)
    issues: list[ConsistencyIssue] = []
    for code in consumer.error_codes:
        if code not in producer_codes:
            issues.append(
                ConsistencyIssue(
                    issue_type=ConsistencyIssueType.UNHANDLED_ERROR_CODE,
                    endpoint=endpoint,
                    method=method,
                    description=(
                        f"Consumer '{consumer_id}' handles error code {code} "
                        f"for {method} {endpoint}, but producer '{producer_id}' never emits it"
                    ),
                    agents_involved=[producer_id, consumer_id],
                )
            )
    return issues


def _build_endpoint_index(
    implementations: list[AgentImplementation],
) -> dict[str, list[tuple[str, ApiContract]]]:
    """Index implementations by endpoint."""
    endpoint_index: dict[str, list[tuple[str, ApiContract]]] = {}
    for impl in implementations:
        for contract in impl.contracts:
            endpoint_index.setdefault(contract.endpoint, []).append((impl.agent_id, contract))
    return endpoint_index


def _elect_producer(
    endpoint: str,
    entries: list[tuple[str, ApiContract]],
    implementations: list[AgentImplementation],
) -> tuple[str, ApiContract]:
    """Elect the producer for an endpoint (prefer backend role)."""
    for impl in implementations:
        for contract in impl.contracts:
            if contract.endpoint == endpoint and impl.role == "backend":
                return impl.agent_id, contract
    return entries[0]


def _check_endpoint_consistency(
    endpoint: str,
    entries: list[tuple[str, ApiContract]],
    implementations: list[AgentImplementation],
    all_issues: list[ConsistencyIssue],
) -> None:
    """Check method, schema, and error-code consistency for one endpoint."""
    methods = {contract.method for _, contract in entries}
    if len(methods) > 1:
        agent_ids = [aid for aid, _ in entries]
        all_issues.append(
            ConsistencyIssue(
                issue_type=ConsistencyIssueType.METHOD_MISMATCH,
                endpoint=endpoint,
                method=", ".join(sorted(methods)),
                description=(f"Endpoint '{endpoint}' has conflicting HTTP methods across agents: {sorted(methods)}"),
                agents_involved=agent_ids,
            )
        )
        return

    method = next(iter(methods))
    producer_id, producer_contract = _elect_producer(endpoint, entries, implementations)

    for consumer_id, consumer_contract in entries:
        if consumer_id == producer_id:
            continue
        all_issues.extend(
            _check_schema_compatibility(
                endpoint,
                method,
                producer_contract,
                consumer_contract,
                producer_id,
                consumer_id,
            )
        )
        all_issues.extend(
            _check_error_codes(
                endpoint,
                method,
                producer_contract,
                consumer_contract,
                producer_id,
                consumer_id,
            )
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def check_consistency(implementations: list[AgentImplementation]) -> ConsistencyReport:
    """Check cross-agent consistency for a set of agent implementations.

    Identifies the "producer" for each endpoint+method as the agent with role
    ``"backend"`` (or the first agent to declare the endpoint when no backend
    role is present), then validates all other agents' contracts against it.

    Checks performed for each endpoint:
    1. Every agent that declares the endpoint uses the same HTTP method.
    2. Request/response schema fields are compatible between producer and consumers.
    3. Error codes handled by consumers are a subset of codes declared by the producer.

    Args:
        implementations: List of :class:`AgentImplementation` objects — one per
            agent that worked on the feature.

    Returns:
        :class:`ConsistencyReport` listing all issues found.
    """
    if len(implementations) < 2:
        logger.debug("cross_agent_consistency: fewer than 2 implementations — nothing to compare")
        return ConsistencyReport(issues=[], checked_endpoints=0)

    endpoint_index = _build_endpoint_index(implementations)

    all_issues: list[ConsistencyIssue] = []
    checked = 0

    for endpoint, entries in endpoint_index.items():
        if len(entries) < 2:
            continue

        checked += 1
        _check_endpoint_consistency(endpoint, entries, implementations, all_issues)

    logger.info(
        "cross_agent_consistency: checked %d endpoints, found %d issues",
        checked,
        len(all_issues),
    )
    return ConsistencyReport(issues=all_issues, checked_endpoints=checked)
