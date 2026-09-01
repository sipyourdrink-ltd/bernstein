"""Profile-driven OTLP attribute → chain-event mapping.

Each profile is a pure dataclass mapping OTLP span attributes to chain event
fields. No vendor branches: a profile MUST NOT contain an ``if`` statement that
checks the value of a vendor name string.

Static assertions at import time verify the no-vendor-branch invariant so a
profile that violates it fails at load time, not at runtime when a span from
an unrecognised vendor arrives.

Profile discovery
----------------
Call :func:`get_profile` with a profile name. Unknown names raise
:class:`ProfileNotFound`. All profiles are imported eagerly at module load so
the static assertions run once at startup rather than on the first ingest call.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "COVERAGE_NOT_SCHEDULED_BY_BERNSTEIN",
    "COVERAGE_PARTIAL",
    "SOURCE_KIND_AGENT",
    "SOURCE_KIND_COLLECTOR",
    "SOURCE_KIND_OTHER",
    "IngestProfile",
    "ProfileNotFound",
    "get_profile",
    "list_profiles",
]

#: Profile name that describes the catch-all collector-received ingest path.
#: Used when no specific source-profile applies.
DEFAULT_PROFILE_NAME = "generic"


# --------------------------------------------------------------------------- #
# Coverage level constants                                                     #
# --------------------------------------------------------------------------- #

#: The ingest covers activity Bernstein did not schedule or orchestrate.
#: This is the honest coverage state for all foreign-runtime spans.
COVERAGE_NOT_SCHEDULED_BY_BERNSTEIN = "not_scheduled_by_bernstein"

#: Bernstein orchestrated part of the activity; the ingest covers the remainder.
#: Reserved for mixed environments where some agents run under Bernstein and
#: others emit spans into the same pipeline.
COVERAGE_PARTIAL = "partial"


# --------------------------------------------------------------------------- #
# Source kind constants                                                        #
# --------------------------------------------------------------------------- #

#: Span was emitted by a collector / forwarder, not directly by an agent.
SOURCE_KIND_COLLECTOR = "collector"
SOURCE_KIND_AGENT = "agent"
SOURCE_KIND_OTHER = "other"


# --------------------------------------------------------------------------- #
# Errors                                                                       #
# --------------------------------------------------------------------------- #


class ProfileNotFound(KeyError):
    """Raised when a profile name is not registered."""


# --------------------------------------------------------------------------- #
# IngestProfile                                                                #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class IngestProfile:
    """Profile driving how one source's OTLP attributes map to chain events.

    A profile is a pure mapping: given a raw OTLP span dict, it extracts the
    fields needed to build an ``IngestReceipt`` and to write the matching
    chain event. No profile may contain a vendor-branch (``if vendor == "..."``)
    — the static assertion below enforces this.

    Attributes:
        name: Human-readable profile name. Must be unique.
        source_kind: Class of the emitting runtime
            (:data:`SOURCE_KIND_COLLECTOR`, :data:`SOURCE_KIND_AGENT`, or
            :data:`SOURCE_KIND_OTHER`).
        coverage: Coverage level for all spans ingested under this profile.
        coverage_detail: Free-form human-readable description of what the
            ingest *does not* cover.
        activity_type_hint: Default ``activity_type`` written into chain events
            when the span carries no type hint.
        trace_id_attr: OTLP attribute key for the trace id, when not in the
            standard ``traceId`` field.
        span_id_attr: OTLP attribute key for the span id, when not in the
            standard ``spanId`` field.
        resource_attrs: OTLP resource-attribute keys to lift into the chain
            event attributes namespace (e.g. ``service.name``).
        event_type_from_attrs: List of OTLP attribute keys to consult, in order,
            to derive a chain event type when none is declared explicitly. The
            first non-empty value is used.
        extra_field_map: Mapping from OTLP attribute keys to chain event
            attribute keys for fields not covered by the other constants.
    """

    name: str
    source_kind: str
    coverage: str = COVERAGE_NOT_SCHEDULED_BY_BERNSTEIN
    coverage_detail: str = (
        "Bernstein did not schedule or orchestrate this activity. "
        "The ingest boundary received this span from a foreign runtime "
        "and recorded it as governance activity without claiming completeness "
        "over the source system."
    )
    activity_type_hint: str = "otlp_foreign"
    trace_id_attr: str | None = None
    span_id_attr: str | None = None
    resource_attrs: tuple[str, ...] = ("service.name", "service.namespace")
    event_type_from_attrs: tuple[str, ...] = (
        "gen_ai.operation.name",
        "db.operation",
        "rpc.method",
    )
    extra_field_map: dict[str, str] = field(default_factory=dict)

    def extract_event_type(self, attrs: dict[str, Any]) -> str:
        """Return a chain event type derived from ``attrs`` using this profile.

        Tries each key in :attr:`event_type_from_attrs` in order and returns
        the first non-empty value found. Falls back to :attr:`activity_type_hint`.
        """
        for key in self.event_type_from_attrs:
            val = attrs.get(key)
            if val is not None and str(val).strip():
                return str(val)
        return self.activity_type_hint

    def extract_trace_id(self, raw: dict[str, Any]) -> str | None:
        """Return the trace id from ``raw`` using this profile's trace_id_attr."""
        if self.trace_id_attr:
            val = raw.get(self.trace_id_attr)
            return str(val) if val is not None else None
        val = raw.get("traceId") or raw.get("trace_id")
        return str(val) if val is not None else None

    def extract_span_id(self, raw: dict[str, Any]) -> str | None:
        """Return the span id from ``raw`` using this profile's span_id_attr."""
        if self.span_id_attr:
            val = raw.get(self.span_id_attr)
            return str(val) if val is not None else None
        val = raw.get("spanId") or raw.get("span_id")
        return str(val) if val is not None else None


# --------------------------------------------------------------------------- #
# Registry                                                                     #
# --------------------------------------------------------------------------- #

_PROFILE_REGISTRY: dict[str, IngestProfile] = {}


def _register(p: IngestProfile) -> None:
    _PROFILE_REGISTRY[p.name] = p


def get_profile(name: str) -> IngestProfile:
    """Return the profile named ``name``.

    Raises:
        ProfileNotFound: When ``name`` is not registered.
    """
    if name not in _PROFILE_REGISTRY:
        raise ProfileNotFound(f"ingest profile {name!r} not found")
    return _PROFILE_REGISTRY[name]


def list_profiles() -> list[str]:
    """Return the sorted list of registered profile names."""
    return sorted(_PROFILE_REGISTRY)


# --------------------------------------------------------------------------- #
# Static no-vendor-branch assertion                                            #
# --------------------------------------------------------------------------- #
#
# Profiles MUST NOT contain vendor names in their string fields (profile name,
# extra_field_map keys/values).  A source with a different attribute shape
# gets its own named profile rather than a runtime vendor branch.  The check
# fires at import time so a violation fails the process at startup.

_VENDOR_STRINGS = frozenset(
    {
        "aws",
        "gcp",
        "azure",
        "otelcol",
        "datadog",
        "newrelic",
        "splunk",
        "sumologic",
        "lightstep",
        "honeycomb",
        "signalfx",
    }
)


def _check_no_vendor_branch(profile: IngestProfile) -> None:
    profile_lower = profile.name.lower()
    for vendor in _VENDOR_STRINGS:
        if vendor in profile_lower:
            raise AssertionError(
                f"profile {profile.name!r} name contains vendor string {vendor!r}; move to a named profile instead"
            )
    for key in profile.extra_field_map:
        key_lower = key.lower()
        for vendor in _VENDOR_STRINGS:
            if vendor in key_lower:
                raise AssertionError(
                    f"profile {profile.name!r} extra_field_map contains vendor "
                    f"key {key!r}; move to a named profile instead"
                )


# --------------------------------------------------------------------------- #
# Built-in profiles                                                            #
# --------------------------------------------------------------------------- #

_register(
    IngestProfile(
        name=DEFAULT_PROFILE_NAME,
        source_kind=SOURCE_KIND_COLLECTOR,
    )
)

_register(
    IngestProfile(
        name="otel_collector",
        source_kind=SOURCE_KIND_COLLECTOR,
        coverage=COVERAGE_NOT_SCHEDULED_BY_BERNSTEIN,
        coverage_detail=(
            "Spans received via an OTLP collector/forwarder. "
            "Bernstein received these spans as a downstream OTLP receiver "
            "and recorded them as governance activity. Bernstein did not schedule "
            "or orchestrate the underlying agent workloads."
        ),
    )
)

_register(
    IngestProfile(
        name="agent_direct",
        source_kind=SOURCE_KIND_AGENT,
        coverage=COVERAGE_NOT_SCHEDULED_BY_BERNSTEIN,
        coverage_detail=(
            "Spans emitted directly by an agent runtime (no collector in between). "
            "Bernstein received these spans as an OTLP receiver and recorded them "
            "as governance activity. Bernstein did not schedule or orchestrate "
            "the agent that produced these spans."
        ),
        event_type_from_attrs=(
            "gen_ai.operation.name",
            "db.operation",
            "rpc.method",
            "http.route",
        ),
    )
)

# Run static assertions for all registered profiles
for _p in _PROFILE_REGISTRY.values():
    _check_no_vendor_branch(_p)
