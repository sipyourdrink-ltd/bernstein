"""Codex adapter driving Cloudflare's first-party sandbox bridge (issue #2969).

The previous implementation refused every operation, and the refusal was
correct at the time: it targeted
``https://api.cloudflare.com/client/v4/accounts/{id}/sandbox/...``, a REST
route family that has never existed, and the sandbox product was reachable
only as a Worker binding - not callable from a Python process.

That premise expired. ``@cloudflare/sandbox`` now ships a first-party HTTP
bridge (``@cloudflare/sandbox/bridge``): a small Worker the operator deploys
that exposes the sandbox as REST + SSE with Bearer auth. An external process
can create a sandbox, seed a workspace, stream a command, collect a workspace
tar, and destroy the container over plain HTTP. This module drives that
bridge.

Pinned contract
---------------

Implemented against ``@cloudflare/sandbox`` :data:`BRIDGE_SDK_VERSION`, whose
bridge serves API contract :data:`BRIDGE_API_CONTRACT_VERSION` at
``GET /v1/openapi.json``.

Those two version lines are deliberately separate, and conflating them is a
trap worth naming: the served document's ``info.version`` is the *API contract*
version (``1.0.0`` - the schema is titled "Cloudflare Sandbox Service API"), not
the npm package version. A healthy deployment of the pinned SDK reports
``1.0.0``, so comparing ``info.version`` against the package version would
refuse every real bridge. :meth:`CodexCloudflareAdapter.preflight` therefore
checks the API contract major series, and separately validates that the served
``paths`` still carry every route this adapter drives - a capability probe that
stays honest even if the hand-maintained ``info.version`` goes stale while
routes move underneath it.

Three behaviours are load-bearing and deliberate:

* **Cancellation issues the delete, and does not resurrect what it deleted.**
  Closing the ``/exec`` SSE stream does not stop the container process - the
  command keeps running and keeps billing. :meth:`CodexCloudflareAdapter.cancel`
  issues ``DELETE /v1/sandbox/:id``, which the bridge routes through an
  allocation-free pool lookup. It deliberately does **not** confirm with
  ``GET /v1/sandbox/:id/running``: that route sits behind the warm-pool
  middleware, which allocates (and will start) a container for the id before
  answering, so a post-delete ``running`` probe both reports ``true`` on a
  perfectly healthy bridge and creates a fresh billable container. Teardown is
  confirmed instead against ``GET /v1/pool/stats``, which reads pool state
  without allocating.

* **Preflight refuses an unauthenticated bridge.** The bridge's auth checks are
  conditional on its ``SANDBOX_API_KEY`` secret: with the secret unset both the
  ``/v1/sandbox/*`` middleware and the inline check on ``POST /v1/sandbox`` pass
  every request through. Preflight probes ``POST /v1/sandbox`` with no
  ``Authorization`` header and refuses when the call succeeds - a world-writable
  sandbox endpoint is a misconfigured deployment, not a usable one.

* **The run lands sandbox evidence, not a gap.** ``POST /v1/sandbox/:id/persist``
  returns the workspace as a tar; the adapter hashes those bytes into a content
  address and records it on the result as :class:`SandboxEvidence`, which
  converts to the same
  :class:`~bernstein.core.sandbox.selection_receipt.RaceCandidate` a local
  backend produces. Where a task ran is an attribute of the record - the
  ``isolation`` field carries :data:`REMOTE_ISOLATION` - rather than a hole in
  it.

Configuration honesty
---------------------

Fields the bridge cannot honour are gone rather than accepted-and-ignored:
per-request ``memory_mb`` / ``cpu_cores`` (container sizing is the deploy-time
wrangler ``instance_type``), ``network_access`` (the bridge applies no egress
restrictions), ``sandbox_image`` (deploy-time wrangler ``image``), ``r2_bucket``
(bucket mounts need a deploy-time binding), and the account-scoped
``cloudflare_account_id`` / ``cloudflare_api_token`` (the bridge authenticates
with its own secret). Removing them is a breaking config change, so
:meth:`CodexSandboxConfig.from_mapping` rejects each retired key by name with
the reason and the replacement - an operator carrying an old config gets an
explanation, not a silently ignored knob.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import hashlib
import io
import json
import logging
import tarfile
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from bernstein.core.sandbox.selection_receipt import RaceCandidate

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Pinned bridge contract
# ---------------------------------------------------------------------------

#: npm package that ships the bridge Worker the adapter talks to.
BRIDGE_PACKAGE = "@cloudflare/sandbox"

#: Exact package version this adapter was written and tested against. Recorded
#: for operators and release notes; it is deliberately **not** compared against
#: anything the bridge serves (see :data:`BRIDGE_API_CONTRACT_VERSION`).
BRIDGE_SDK_VERSION = "0.12.4"

#: ``info.version`` the pinned bridge publishes in ``GET /v1/openapi.json``.
#: This is the API contract version, independent of the package version, and it
#: is what preflight compares against.
BRIDGE_API_CONTRACT_VERSION = "1.0.0"

#: Route prefix every bridge endpoint lives under.
BRIDGE_API_PREFIX = "/v1"

#: Templated OpenAPI paths the adapter drives. Preflight requires all of them
#: to be present in the served document, so a bridge that has moved or dropped
#: a route is caught even when its ``info.version`` has not been bumped.
REQUIRED_BRIDGE_PATHS: tuple[str, ...] = (
    "/v1/sandbox",
    "/v1/sandbox/{id}",
    "/v1/sandbox/{id}/exec",
    "/v1/sandbox/{id}/hydrate",
    "/v1/sandbox/{id}/persist",
    "/v1/sandbox/{id}/session",
    "/v1/pool/stats",
)

#: Hard cap the bridge enforces on ``PUT /file/*`` and ``POST /hydrate``
#: payloads. Exceeding it is rejected locally so the operator gets a sized
#: error instead of an opaque bridge rejection.
BRIDGE_MAX_PAYLOAD_BYTES = 32 * 1024 * 1024

#: Value recorded in the ``isolation`` field of the sandbox evidence.
#:
#: Prefixed ``requested:`` for the same reason
#: :mod:`bernstein.core.sandbox.fork_race` does it: the adapter asks a remote
#: bridge to run the work, it does not boot or probe the isolation boundary,
#: so it cannot attest the posture was in effect. The genuinely verified part
#: is the content-addressed terminal digest, which is re-hashable offline.
REMOTE_ISOLATION = "requested:codex-cloudflare"

#: Default workspace root inside the sandbox. Bridge path containment rejects
#: anything that resolves outside it.
DEFAULT_WORKDIR = "/workspace"

#: Config keys the real bridge surface cannot honour, mapped to the reason and
#: the replacement. Consumed by :meth:`CodexSandboxConfig.from_mapping`.
RETIRED_CONFIG_FIELDS: dict[str, str] = {
    "cloudflare_account_id": (
        "the bridge authenticates with its own SANDBOX_API_KEY secret, not an "
        "account-scoped credential; set `bridge_url` and `bridge_api_key` instead"
    ),
    "cloudflare_api_token": (
        "the bridge authenticates with its own SANDBOX_API_KEY secret, not a "
        "Cloudflare API token; set `bridge_api_key` instead"
    ),
    "sandbox_image": (
        "the container image is a deploy-time wrangler setting on the bridge "
        "Worker (`containers[].image`); it cannot be chosen per request"
    ),
    "memory_mb": (
        "container memory is a deploy-time wrangler setting "
        "(`containers[].instance_type`); it cannot be sized per request"
    ),
    "cpu_cores": (
        "container vCPU is a deploy-time wrangler setting "
        "(`containers[].instance_type`); it cannot be sized per request"
    ),
    "network_access": (
        "the bridge applies no egress restrictions and exposes no domain "
        "allowlist, so this could only ever have been ignored"
    ),
    "r2_bucket": (
        "bucket mounts require a deploy-time R2 binding on the bridge Worker; "
        "workspace transfer here uses hydrate/persist instead"
    ),
    "tokens_used": "the bridge reports process exit codes and output, never token counts",
}


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class CodexCloudflareError(RuntimeError):
    """Base class for every Codex-on-Cloudflare failure."""


class CodexCloudflareNotConfiguredError(CodexCloudflareError):
    """Raised when no bridge deployment has been configured.

    The adapter never falls back to local execution: an unconfigured remote
    sandbox is a refusal, because silently running the agent on the
    orchestrator host would discard the isolation the caller asked for.
    """

    def __init__(self, missing: tuple[str, ...]) -> None:
        self.missing = missing
        names = ", ".join(missing)
        super().__init__(
            "Codex-on-Cloudflare adapter is not configured: missing "
            f"{names}. Deploy the {BRIDGE_PACKAGE} bridge Worker "
            f"(pinned {BRIDGE_SDK_VERSION}), then set `bridge_url` to its URL "
            "and `bridge_api_key` to its SANDBOX_API_KEY secret. This adapter "
            "does not fall back to local execution - use the `codex` adapter "
            "to run Codex on this host on purpose.",
        )


class CodexCloudflareConfigError(CodexCloudflareError):
    """Raised when a config mapping carries a key the bridge cannot honour."""


class CodexCloudflareBridgeAuthError(CodexCloudflareError):
    """Raised when the bridge serves ``/v1/sandbox`` without authentication.

    The bridge's auth checks only run when its ``SANDBOX_API_KEY`` secret is
    set; with the secret unset it passes every request through. A bridge in
    that state hands any caller a container on the operator's account, so the
    adapter refuses rather than using it.
    """

    def __init__(self, bridge_url: str) -> None:
        self.bridge_url = bridge_url
        super().__init__(
            f"Cloudflare sandbox bridge at {bridge_url} accepted POST "
            f"{BRIDGE_API_PREFIX}/sandbox with no Authorization header: its "
            "SANDBOX_API_KEY secret is unset, so it skips authentication and "
            "will create containers for any caller. Refusing to use it. Set "
            "the secret on the Worker (`npx wrangler secret put "
            "SANDBOX_API_KEY`), redeploy, and put the same value in "
            "`bridge_api_key`.",
        )


class CodexCloudflareBridgeVersionError(CodexCloudflareError):
    """Raised when the bridge serves an unsupported API contract version."""

    def __init__(self, observed: str, bridge_url: str) -> None:
        self.observed = observed
        self.bridge_url = bridge_url
        seen = observed or "<absent>"
        super().__init__(
            f"Cloudflare sandbox bridge at {bridge_url} serves API contract "
            f"version {seen}; this adapter implements the "
            f"{BRIDGE_API_CONTRACT_VERSION} contract (as published by "
            f"{BRIDGE_PACKAGE}@{BRIDGE_SDK_VERSION}). A different major "
            "contract changes route semantics, so it is not safe to drive "
            "blindly. Redeploy the bridge from "
            f"{BRIDGE_PACKAGE}@{BRIDGE_SDK_VERSION}, or set "
            "`require_supported_api_version=False` to accept the drift "
            "deliberately - the route probe stays strict either way.",
        )


class CodexCloudflareBridgeContractError(CodexCloudflareError):
    """Raised when the served OpenAPI document is missing routes the adapter needs."""

    def __init__(self, missing: tuple[str, ...], bridge_url: str) -> None:
        self.missing = missing
        self.bridge_url = bridge_url
        names = ", ".join(missing)
        super().__init__(
            f"Cloudflare sandbox bridge at {bridge_url} does not publish "
            f"route(s) this adapter drives: {names}. The {BRIDGE_PACKAGE} "
            "package moves and removes routes across releases, so a bridge "
            "missing them cannot complete a run. Redeploy the bridge from "
            f"{BRIDGE_PACKAGE}@{BRIDGE_SDK_VERSION}.",
        )


class CodexCloudflareBridgeApiError(CodexCloudflareError):
    """Raised when a bridge route returns a non-2xx response."""

    def __init__(self, route: str, status_code: int, body: str) -> None:
        self.route = route
        self.status_code = status_code
        self.body = body
        super().__init__(
            f"Cloudflare sandbox bridge returned HTTP {status_code} for {route}: {body[:1024]}",
        )


class CodexCloudflarePayloadTooLargeError(CodexCloudflareError):
    """Raised before upload when a payload exceeds the bridge's 32 MiB cap."""

    def __init__(self, route: str, size_bytes: int) -> None:
        self.route = route
        self.size_bytes = size_bytes
        super().__init__(
            f"Payload for {route} is {size_bytes} bytes, over the bridge's "
            f"{BRIDGE_MAX_PAYLOAD_BYTES}-byte cap. Trim the workspace (the "
            "`persist_excludes` setting drops paths from the returned tar) or "
            "seed large inputs by cloning them inside the sandbox instead.",
        )


class CodexCloudflareCancelError(CodexCloudflareError):
    """Raised when the delete that stops the remote container did not land."""

    def __init__(self, sandbox_id: str, detail: str) -> None:
        self.sandbox_id = sandbox_id
        self.detail = detail
        super().__init__(
            f"Could not stop Cloudflare sandbox {sandbox_id}: "
            f"DELETE {BRIDGE_API_PREFIX}/sandbox/{sandbox_id} failed ({detail}). "
            "The remote command may still be running and billing; retry the "
            "delete, then check the bridge Worker logs and the Cloudflare "
            "dashboard.",
        )


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CodexSandboxConfig:
    """Configuration for Codex execution on a deployed sandbox bridge.

    Every field here maps to something the bridge actually honours. Knobs the
    bridge cannot implement are listed in :data:`RETIRED_CONFIG_FIELDS` and
    rejected by :meth:`from_mapping`.

    Attributes:
        bridge_url: Base URL of the operator-deployed bridge Worker, e.g.
            ``https://sandbox-bridge.example.workers.dev``. The ``/v1``
            prefix is appended by the adapter.
        bridge_api_key: The Worker's ``SANDBOX_API_KEY`` secret, sent as
            ``Authorization: Bearer``.
        openai_api_key: API key the Codex CLI needs *inside* the sandbox.
            Injected as session environment, never as an argv element.
        workdir: Workspace root inside the sandbox. The bridge rejects paths
            resolving outside ``/workspace``.
        agent_command: argv prefix for the coding agent inside the sandbox.
            The model flag and the prompt are appended.
        extra_env: Additional environment pairs for the sandbox session.
            Tuple-of-pairs so the config stays frozen and hashable.
        max_execution_minutes: Default wall-clock ceiling for one run,
            forwarded as the bridge's ``timeout_ms``.
        request_timeout_seconds: Per-request HTTP timeout for the short
            control-plane calls (create, persist, delete). The ``/exec``
            stream gets ``max_execution_minutes`` plus this as headroom.
        persist_excludes: Relative paths dropped from the persisted workspace
            tar, forwarded as the ``excludes`` query parameter. The bridge
            rejects excludes containing ``..``.
        require_supported_api_version: When true (default) preflight refuses a
            bridge whose served API contract major differs from
            :data:`BRIDGE_API_CONTRACT_VERSION`. The route probe is
            unconditional either way.
    """

    bridge_url: str = ""
    bridge_api_key: str = ""
    openai_api_key: str = ""
    workdir: str = DEFAULT_WORKDIR
    agent_command: tuple[str, ...] = ("codex", "exec")
    extra_env: tuple[tuple[str, str], ...] = ()
    max_execution_minutes: int = 30
    request_timeout_seconds: float = 60.0
    persist_excludes: tuple[str, ...] = (".git",)
    require_supported_api_version: bool = True

    @property
    def is_configured(self) -> bool:
        """True when both the bridge URL and its API key are present."""
        return not self.missing_fields()

    def missing_fields(self) -> tuple[str, ...]:
        """Names of the required fields that are empty."""
        missing: list[str] = []
        if not self.bridge_url.strip():
            missing.append("bridge_url")
        if not self.bridge_api_key.strip():
            missing.append("bridge_api_key")
        return tuple(missing)

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> CodexSandboxConfig:
        """Build a config from a mapping, refusing keys the bridge cannot honour.

        This is the construction path for operator-supplied config. A key the
        bridge has no way to implement is rejected by name with the reason and
        the replacement, so an operator carrying a pre-bridge config learns why
        the knob is gone instead of watching it be ignored.

        Args:
            values: Mapping of config field names to values.

        Returns:
            The constructed :class:`CodexSandboxConfig`.

        Raises:
            CodexCloudflareConfigError: If any key is retired or unknown.
        """
        retired = [key for key in values if key in RETIRED_CONFIG_FIELDS]
        if retired:
            reasons = "; ".join(f"`{key}`: {RETIRED_CONFIG_FIELDS[key]}" for key in sorted(retired))
            raise CodexCloudflareConfigError(
                "Codex-on-Cloudflare config carries settings the deployed "
                f"bridge cannot honour, so they are refused rather than ignored - {reasons}.",
            )
        known = set(cls.__dataclass_fields__)
        unknown = sorted(set(values) - known)
        if unknown:
            raise CodexCloudflareConfigError(
                f"Unknown Codex-on-Cloudflare config key(s): {', '.join(unknown)}. "
                f"Known keys: {', '.join(sorted(known))}.",
            )
        payload = dict(values)
        for tuple_field in ("agent_command", "persist_excludes"):
            if tuple_field in payload:
                payload[tuple_field] = tuple(payload[tuple_field])
        if "extra_env" in payload:
            raw_env = payload["extra_env"]
            pairs = raw_env.items() if isinstance(raw_env, dict) else raw_env
            payload["extra_env"] = tuple((str(k), str(v)) for k, v in pairs)
        return cls(**payload)


# ---------------------------------------------------------------------------
# Results and evidence
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SandboxEvidence:
    """Content-addressed evidence that a remote run produced a workspace.

    A remote sandbox must not be a weaker link in the chain than a local one.
    The local sandbox path records a candidate's terminal workspace as a CAS
    digest a verifier re-hashes offline; this is that same record for a
    bridge-executed run. The digest is the SHA-256 of the exact tar bytes the
    bridge returned from ``POST /v1/sandbox/:id/persist``, so a verifier
    holding the blob re-derives it without reaching the network - and
    ``isolation`` names where the work ran.

    Attributes:
        terminal_snapshot_digest: SHA-256 hex digest of the persisted
            workspace tar, in the same 64-lowercase-hex shape the CAS verifier
            expects.
        isolation: :data:`REMOTE_ISOLATION`.
        sandbox_id: Bridge-assigned sandbox identifier the run used.
        bridge_api_version: API contract version observed at preflight, or the
            pinned contract version when preflight was skipped.
        bridge_sdk_version: Package version the adapter implements against.
        workspace_bytes: Size of the persisted tar in bytes.
    """

    terminal_snapshot_digest: str
    isolation: str = REMOTE_ISOLATION
    sandbox_id: str = ""
    bridge_api_version: str = BRIDGE_API_CONTRACT_VERSION
    bridge_sdk_version: str = BRIDGE_SDK_VERSION
    workspace_bytes: int = 0

    def to_race_candidate(self, task_id: str, score_vector: Mapping[str, float]) -> RaceCandidate:
        """Project the evidence onto the signed selection-receipt candidate shape.

        Args:
            task_id: Stable candidate identifier.
            score_vector: Deterministic per-axis scores for the ranker.

        Returns:
            A :class:`~bernstein.core.sandbox.selection_receipt.RaceCandidate`
            carrying this run's terminal digest and isolation tier, ready to be
            signed into a selection receipt alongside locally-produced
            candidates.
        """
        # Imported here rather than at module scope: the receipt module pulls
        # in `cryptography`, and importing an adapter should not pay for
        # signing machinery the caller may never use.
        from bernstein.core.sandbox.selection_receipt import RaceCandidate

        return RaceCandidate(
            task_id=task_id,
            terminal_snapshot_digest=self.terminal_snapshot_digest,
            score_vector=dict(score_vector),
            isolation=self.isolation,
        )


@dataclass(frozen=True)
class CodexSandboxResult:
    """Result of one Codex run on the bridge.

    Attributes:
        sandbox_id: Bridge-assigned sandbox identifier.
        status: ``"completed"``, ``"failed"``, or ``"timeout"``.
        files_changed: Workspace-relative paths whose content differs from the
            seeded workspace (or that are new/removed), derived by diffing the
            seeded tar against the persisted one.
        stdout: Decoded stdout, reassembled from the base64 SSE frames.
        stderr: Decoded stderr, reassembled from the base64 SSE frames.
        exit_code: Process exit code from the terminal ``exit`` event.
        execution_time_seconds: Wall-clock duration of the exec stream.
        workspace_tar: Raw tar bytes returned by ``/persist``.
        evidence: Content-addressed sandbox evidence for the run.
        error: Message from a terminal ``error`` SSE event, empty otherwise.
    """

    sandbox_id: str
    status: str
    files_changed: list[str] = field(default_factory=list[str])
    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0
    execution_time_seconds: float = 0.0
    workspace_tar: bytes | None = None
    evidence: SandboxEvidence | None = None
    error: str = ""


@dataclass(frozen=True)
class WarmPoolStats:
    """Allocation-free snapshot of the bridge's warm-pool state.

    Read from ``GET /v1/pool/stats``, which reports pool bookkeeping without
    acquiring or starting a container - unlike the per-sandbox routes.

    Attributes:
        warm: Pre-warmed containers not yet assigned to a sandbox id.
        assigned: Containers currently assigned to a sandbox id.
        total: ``warm + assigned``.
    """

    warm: int = 0
    assigned: int = 0
    total: int = 0


@dataclass(frozen=True)
class CancelOutcome:
    """Result of :meth:`CodexCloudflareAdapter.cancel`.

    Attributes:
        sandbox_id: The sandbox that was deleted.
        deleted: ``True`` when the bridge accepted the delete.
        pool_after: Warm-pool state read after the delete, without allocating.
    """

    sandbox_id: str
    deleted: bool
    pool_after: WarmPoolStats


@dataclass(frozen=True)
class BridgePreflight:
    """Outcome of :meth:`CodexCloudflareAdapter.preflight`.

    Attributes:
        bridge_url: Base URL that was probed.
        api_version: ``info.version`` from the served OpenAPI document - the
            API contract version, not the npm package version.
        auth_enforced: Always ``True`` when preflight returns - an
            unauthenticated bridge raises instead of reporting ``False``.
        api_version_supported: ``True`` when ``api_version`` shares a major
            with :data:`BRIDGE_API_CONTRACT_VERSION`.
        routes_verified: The :data:`REQUIRED_BRIDGE_PATHS` found in the served
            document.
    """

    bridge_url: str
    api_version: str
    auth_enforced: bool
    api_version_supported: bool
    routes_verified: tuple[str, ...]


@dataclass(frozen=True)
class _ExecOutcome:
    """Folded result of one ``/exec`` SSE stream."""

    status: str
    exit_code: int
    stdout: bytes
    stderr: bytes
    error: str


# ---------------------------------------------------------------------------
# SSE framing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _SseFrame:
    """One parsed ``event:``/``data:`` frame from an SSE stream."""

    event: str
    data: str


def _parse_sse_frames(lines: list[str]) -> list[_SseFrame]:
    """Parse SSE body lines into frames.

    The bridge emits one ``event:`` line followed by one or more ``data:``
    lines, terminated by a blank line; its writer splits a multi-line payload
    across ``data:`` lines, and they are rejoined here with newlines.

    Args:
        lines: Lines of the SSE body, without trailing newlines.

    Returns:
        The frames in arrival order.
    """
    frames: list[_SseFrame] = []
    event = ""
    data: list[str] = []
    for raw in lines:
        line = raw.rstrip("\r")
        if not line:
            if event or data:
                frames.append(_SseFrame(event=event or "message", data="\n".join(data)))
            event = ""
            data = []
            continue
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            event = line[len("event:") :].strip()
        elif line.startswith("data:"):
            data.append(line[len("data:") :].removeprefix(" "))
    if event or data:
        frames.append(_SseFrame(event=event or "message", data="\n".join(data)))
    return frames


def _decode_stream_payload(payload: str) -> bytes:
    """Decode a base64 ``stdout``/``stderr`` SSE payload.

    Args:
        payload: The frame's ``data`` value.

    Returns:
        The decoded bytes, or the payload's UTF-8 bytes when it is not valid
        base64 - a malformed frame must not silently lose operator output.
    """
    compact = "".join(payload.split())
    if not compact:
        return b""
    try:
        return base64.b64decode(compact, validate=True)
    except (binascii.Error, ValueError):
        logger.warning("Cloudflare bridge emitted a non-base64 output frame; passing it through verbatim")
        return payload.encode("utf-8")


# ---------------------------------------------------------------------------
# Workspace diffing
# ---------------------------------------------------------------------------


def _tar_index(tar_bytes: bytes) -> dict[str, str]:
    """Map each regular file in a tar to the SHA-256 of its content.

    The bridge builds the persist archive with ``tar cf ... -C /workspace .``,
    so members arrive as ``./path``; the leading ``./`` is normalised away.
    Members are only *read*, never extracted, so the sandbox's symlink caveat
    (a symlink inside the workspace can name a path outside it) cannot turn a
    diff into a host write.

    Args:
        tar_bytes: Raw tar archive bytes.

    Returns:
        Mapping of normalised member path to content digest. Non-regular
        members (directories, symlinks, devices) are skipped.
    """
    index: dict[str, str] = {}
    try:
        with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:*") as archive:
            for member in archive:
                if not member.isfile():
                    continue
                handle = archive.extractfile(member)
                if handle is None:
                    continue
                name = member.name.removeprefix("./").lstrip("/")
                index[name] = hashlib.sha256(handle.read()).hexdigest()
    except (tarfile.TarError, OSError) as exc:
        logger.warning("Could not index workspace tar for diffing: %s", exc)
        return {}
    return index


def _diff_paths(before: Mapping[str, str], after: Mapping[str, str]) -> list[str]:
    """Return sorted paths that were added, removed, or modified."""
    changed = {path for path, digest in after.items() if before.get(path) != digest}
    changed |= set(before) - set(after)
    return sorted(changed)


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class CodexCloudflareAdapter:
    """Runs Codex inside a Cloudflare sandbox via the deployed HTTP bridge.

    Args:
        config: Bridge connection and run settings.
        transport: Optional :class:`httpx.AsyncBaseTransport` used for every
            request. Tests inject an :class:`httpx.MockTransport` carrying
            recorded bridge responses; production leaves it ``None``.
    """

    def __init__(
        self,
        config: CodexSandboxConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._config = config
        self._transport = transport
        self._api_version = BRIDGE_API_CONTRACT_VERSION
        self._transcripts: dict[str, str] = {}

    @property
    def name(self) -> str:
        """Return adapter name."""
        return "codex-cloudflare"

    @property
    def config(self) -> CodexSandboxConfig:
        """The configuration this adapter was constructed with."""
        return self._config

    @property
    def is_configured(self) -> bool:
        """True when a bridge URL and API key are present."""
        return self._config.is_configured

    # -- HTTP plumbing ------------------------------------------------------

    def _require_configured(self) -> None:
        """Raise when no bridge is configured.

        Raises:
            CodexCloudflareNotConfiguredError: If the bridge URL or API key is
                missing. Never degrades to local execution.
        """
        missing = self._config.missing_fields()
        if missing:
            raise CodexCloudflareNotConfiguredError(missing)

    def _base_url(self) -> str:
        return self._config.bridge_url.rstrip("/")

    def _client(self, *, timeout: float | None = None) -> httpx.AsyncClient:
        """Build a client with no default Authorization header.

        Auth is passed per request so :meth:`preflight` can issue one
        deliberately unauthenticated call without fighting client defaults.
        """
        return httpx.AsyncClient(
            base_url=self._base_url(),
            timeout=timeout if timeout is not None else self._config.request_timeout_seconds,
            transport=self._transport,
        )

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._config.bridge_api_key}"}

    @staticmethod
    def _check(route: str, response: httpx.Response) -> None:
        """Raise :class:`CodexCloudflareBridgeApiError` for a non-2xx response."""
        if response.status_code < 400:
            return
        body = ""
        with contextlib.suppress(httpx.HTTPError, UnicodeDecodeError, RuntimeError):
            body = response.text
        raise CodexCloudflareBridgeApiError(route, response.status_code, body)

    # -- Preflight ----------------------------------------------------------

    async def preflight(self) -> BridgePreflight:
        """Verify the bridge is authenticated and serving the expected contract.

        Three checks, each with its own error:

        1. ``POST /v1/sandbox`` is issued **without** an ``Authorization``
           header. The bridge only authenticates when its ``SANDBOX_API_KEY``
           secret is set, so a success here proves the secret is unset and the
           deployment is world-writable; the probe deletes any sandbox it
           accidentally created, then refuses. The create route carries no
           sandbox-id path segment, so this probe cannot be short-circuited by
           the bridge's id-format validation (which runs before the auth check
           on ``/v1/sandbox/:id/*``).
        2. ``GET /v1/openapi.json`` is read with auth and its ``info.version``
           - the API *contract* version, not the npm package version -
           compared against :data:`BRIDGE_API_CONTRACT_VERSION` by major.
        3. Every path in :data:`REQUIRED_BRIDGE_PATHS` must be present in the
           served document. This is the check that actually protects against
           the package's route churn: ``info.version`` is hand-maintained and
           can lag, the published paths cannot.

        Returns:
            The :class:`BridgePreflight` record for the deployment.

        Raises:
            CodexCloudflareNotConfiguredError: If no bridge is configured.
            CodexCloudflareBridgeAuthError: If the bridge answers
                ``/v1/sandbox`` without authentication.
            CodexCloudflareBridgeVersionError: If the served contract major
                differs and drift was not opted into.
            CodexCloudflareBridgeContractError: If a required route is absent.
            CodexCloudflareBridgeApiError: If a probe fails for another reason.
        """
        self._require_configured()
        async with self._client() as client:
            await self._assert_auth_enforced(client)
            document = await self._read_openapi_document(client)

        version = _document_api_version(document)
        supported = _same_major_series(version, BRIDGE_API_CONTRACT_VERSION)
        if not supported and self._config.require_supported_api_version:
            raise CodexCloudflareBridgeVersionError(version, self._base_url())
        if not supported:
            logger.warning(
                "Cloudflare sandbox bridge at %s serves API contract %s, not the expected %s; "
                "proceeding because require_supported_api_version is disabled",
                self._base_url(),
                version or "<absent>",
                BRIDGE_API_CONTRACT_VERSION,
            )

        published = _document_paths(document)
        missing = tuple(path for path in REQUIRED_BRIDGE_PATHS if path not in published)
        if missing:
            raise CodexCloudflareBridgeContractError(missing, self._base_url())

        self._api_version = version or BRIDGE_API_CONTRACT_VERSION
        return BridgePreflight(
            bridge_url=self._base_url(),
            api_version=self._api_version,
            auth_enforced=True,
            api_version_supported=supported,
            routes_verified=REQUIRED_BRIDGE_PATHS,
        )

    async def _assert_auth_enforced(self, client: httpx.AsyncClient) -> None:
        """Refuse a bridge that creates sandboxes for unauthenticated callers."""
        route = f"{BRIDGE_API_PREFIX}/sandbox"
        response = await client.post(route)
        if response.status_code in (401, 403):
            return
        if response.status_code < 400:
            # The bridge just handed an anonymous caller a container. Clean it
            # up before refusing so the probe does not leak a billable sandbox.
            leaked = _json_str_field(response, "id")
            if leaked:
                with contextlib.suppress(httpx.HTTPError):
                    await client.delete(f"{route}/{leaked}")
            raise CodexCloudflareBridgeAuthError(self._base_url())
        self._check(f"POST {route} (unauthenticated auth probe)", response)

    async def _read_openapi_document(self, client: httpx.AsyncClient) -> dict[str, Any]:
        """Fetch and parse the bridge's served OpenAPI document."""
        route = f"{BRIDGE_API_PREFIX}/openapi.json"
        response = await client.get(route, headers=self._auth_headers())
        self._check(f"GET {route}", response)
        try:
            document: Any = response.json()
        except ValueError as exc:
            raise CodexCloudflareBridgeContractError(REQUIRED_BRIDGE_PATHS, self._base_url()) from exc
        if not isinstance(document, dict):
            raise CodexCloudflareBridgeContractError(REQUIRED_BRIDGE_PATHS, self._base_url())
        return document

    # -- Lifecycle ----------------------------------------------------------

    async def create_sandbox(self) -> str:
        """Create a sandbox and return its bridge-assigned id.

        Returns:
            The sandbox identifier.

        Raises:
            CodexCloudflareNotConfiguredError: If no bridge is configured.
            CodexCloudflareBridgeApiError: If the bridge refuses the create.
        """
        self._require_configured()
        async with self._client() as client:
            return await self._create_sandbox(client)

    async def _create_sandbox(self, client: httpx.AsyncClient) -> str:
        route = f"{BRIDGE_API_PREFIX}/sandbox"
        response = await client.post(route, headers=self._auth_headers())
        self._check(f"POST {route}", response)
        sandbox_id = _json_str_field(response, "id")
        if not sandbox_id:
            raise CodexCloudflareBridgeApiError(f"POST {route}", response.status_code, response.text)
        return sandbox_id

    async def is_running(self, sandbox_id: str) -> bool:
        """Return the bridge's ``running`` verdict for *sandbox_id*.

        **This call can start a container.** ``GET /v1/sandbox/:id/running``
        sits behind the bridge's warm-pool middleware, which acquires - and if
        necessary *starts* - a container for the id before the handler runs
        ``true`` inside it. On a bridge with no container for this id the
        answer is therefore ``True``, produced by the container the probe just
        created.

        Do not use this to confirm a teardown: see :meth:`cancel`, which reads
        ``GET /v1/pool/stats`` instead because that route does not allocate.
        This method is for asking whether a sandbox you intend to keep using is
        up.

        Args:
            sandbox_id: The sandbox identifier.

        Returns:
            ``True`` when a container for the id is up (possibly because this
            call started one), ``False`` otherwise.

        Raises:
            CodexCloudflareNotConfiguredError: If no bridge is configured.
        """
        self._require_configured()
        async with self._client() as client:
            return await self._is_running(client, sandbox_id)

    async def _is_running(self, client: httpx.AsyncClient, sandbox_id: str) -> bool:
        route = f"{BRIDGE_API_PREFIX}/sandbox/{sandbox_id}/running"
        response = await client.get(route, headers=self._auth_headers())
        self._check(f"GET {route}", response)
        try:
            payload: Any = response.json()
        except ValueError:
            return False
        return bool(payload.get("running", False)) if isinstance(payload, dict) else False

    async def get_status(self, sandbox_id: str) -> str:
        """Return ``"running"`` or ``"stopped"`` for *sandbox_id*.

        Carries the same allocation caveat as :meth:`is_running`.

        Args:
            sandbox_id: The sandbox identifier.

        Returns:
            ``"running"`` when a container for the id is up, else ``"stopped"``.
        """
        return "running" if await self.is_running(sandbox_id) else "stopped"

    async def pool_stats(self) -> WarmPoolStats:
        """Read the bridge's warm-pool bookkeeping without allocating.

        Returns:
            The current :class:`WarmPoolStats`.

        Raises:
            CodexCloudflareNotConfiguredError: If no bridge is configured.
        """
        self._require_configured()
        async with self._client() as client:
            return await self._pool_stats(client)

    async def _pool_stats(self, client: httpx.AsyncClient) -> WarmPoolStats:
        route = f"{BRIDGE_API_PREFIX}/pool/stats"
        response = await client.get(route, headers=self._auth_headers())
        self._check(f"GET {route}", response)
        try:
            payload: Any = response.json()
        except ValueError:
            return WarmPoolStats()
        if not isinstance(payload, dict):
            return WarmPoolStats()
        return WarmPoolStats(
            warm=_as_int(payload.get("warm")),
            assigned=_as_int(payload.get("assigned")),
            total=_as_int(payload.get("total")),
        )

    async def cancel(self, sandbox_id: str) -> CancelOutcome:
        """Stop the remote work, and confirm teardown without restarting it.

        Closing the ``/exec`` SSE stream does not stop the container process -
        the command keeps running on the operator's account. Cancellation is
        therefore the explicit ``DELETE /v1/sandbox/:id``, which the bridge
        serves through an allocation-free pool lookup (it returns 204 outright
        when the id has no container, and otherwise destroys the container and
        drops it from the pool).

        Confirmation deliberately does **not** read
        ``GET /v1/sandbox/:id/running``. That route is behind the warm-pool
        middleware, which allocates a container for the id before answering, so
        probing it after a delete would report ``true`` on a healthy bridge and
        - worse - start a fresh billable container, undoing the cancel. The
        allocation-free ``GET /v1/pool/stats`` is read instead and returned on
        the outcome.

        Args:
            sandbox_id: The sandbox identifier.

        Returns:
            The :class:`CancelOutcome`, carrying the post-delete pool state.

        Raises:
            CodexCloudflareNotConfiguredError: If no bridge is configured.
            CodexCloudflareCancelError: If the bridge refused the delete, so
                the container may still be running.
        """
        self._require_configured()
        async with self._client() as client:
            try:
                await self._destroy(client, sandbox_id)
            except (CodexCloudflareBridgeApiError, httpx.HTTPError) as exc:
                raise CodexCloudflareCancelError(sandbox_id, str(exc)) from exc
            pool = await self._pool_stats(client)
        return CancelOutcome(sandbox_id=sandbox_id, deleted=True, pool_after=pool)

    async def _destroy(self, client: httpx.AsyncClient, sandbox_id: str) -> None:
        route = f"{BRIDGE_API_PREFIX}/sandbox/{sandbox_id}"
        response = await client.delete(route, headers=self._auth_headers())
        if response.status_code == 404:
            return
        self._check(f"DELETE {route}", response)

    async def _reap(self, client: httpx.AsyncClient, sandbox_id: str) -> None:
        """Delete *sandbox_id*, completing the delete even under a mid-teardown cancel.

        A cancel can land while this coroutine is suspended inside the delete,
        which would leave a container running and billing. Catch the
        cancellation, drive one more bounded delete, then re-raise so the task
        still terminates. No :func:`asyncio.shield`: shielding an unbounded
        teardown turns a cancel into a hang, one bounded retry does not.
        """
        try:
            await self._destroy(client, sandbox_id)
        except asyncio.CancelledError:
            with contextlib.suppress(BaseException):
                await self._destroy(client, sandbox_id)
            raise
        except (CodexCloudflareError, httpx.HTTPError) as exc:
            logger.warning("Could not delete Cloudflare sandbox %s: %s", sandbox_id, exc)

    # -- Workspace transfer -------------------------------------------------

    async def _hydrate(self, client: httpx.AsyncClient, sandbox_id: str, tar_bytes: bytes) -> None:
        # No Session-Id header: the bridge's hydrate handler does not read one,
        # it always writes into /workspace on the container itself.
        route = f"{BRIDGE_API_PREFIX}/sandbox/{sandbox_id}/hydrate"
        if len(tar_bytes) > BRIDGE_MAX_PAYLOAD_BYTES:
            raise CodexCloudflarePayloadTooLargeError(f"POST {route}", len(tar_bytes))
        response = await client.post(
            route,
            content=tar_bytes,
            headers={**self._auth_headers(), "Content-Type": "application/octet-stream"},
        )
        self._check(f"POST {route}", response)

    async def _persist(self, client: httpx.AsyncClient, sandbox_id: str) -> bytes:
        route = f"{BRIDGE_API_PREFIX}/sandbox/{sandbox_id}/persist"
        params = {"excludes": ",".join(self._config.persist_excludes)} if self._config.persist_excludes else None
        response = await client.post(route, headers=self._auth_headers(), params=params)
        self._check(f"POST {route}", response)
        return response.content

    async def write_file(self, sandbox_id: str, path: str, content: bytes) -> None:
        """Write raw bytes to *path* inside the sandbox workspace.

        Args:
            sandbox_id: The sandbox identifier.
            path: Workspace-relative path. The bridge rejects anything that
                resolves outside ``/workspace``.
            content: Raw file bytes, at most 32 MiB.

        Raises:
            CodexCloudflareNotConfiguredError: If no bridge is configured.
            CodexCloudflarePayloadTooLargeError: If *content* exceeds the cap.
            CodexCloudflareBridgeApiError: If the bridge refuses the write.
        """
        self._require_configured()
        route = f"{BRIDGE_API_PREFIX}/sandbox/{sandbox_id}/file/{path.lstrip('/')}"
        if len(content) > BRIDGE_MAX_PAYLOAD_BYTES:
            raise CodexCloudflarePayloadTooLargeError(f"PUT {route}", len(content))
        async with self._client() as client:
            response = await client.put(
                route,
                content=content,
                headers={**self._auth_headers(), "Content-Type": "application/octet-stream"},
            )
            self._check(f"PUT {route}", response)

    # -- Sessions -----------------------------------------------------------

    async def _create_session(self, client: httpx.AsyncClient, sandbox_id: str) -> str:
        """Create a session carrying the workdir and the agent's environment.

        The exec route's request schema is ``{argv, timeout_ms, cwd}`` - there
        is no ``env`` field. The session route takes ``{id, cwd, env}``, so
        that is where the coding-agent API key goes; it also keeps the key out
        of argv, and therefore out of the sandbox process table and the shell
        command string the bridge assembles. Sessions do not survive container
        sleep, so one is created per run.
        """
        route = f"{BRIDGE_API_PREFIX}/sandbox/{sandbox_id}/session"
        env = dict(self._config.extra_env)
        if self._config.openai_api_key:
            env["OPENAI_API_KEY"] = self._config.openai_api_key
        body: dict[str, Any] = {"cwd": self._config.workdir}
        if env:
            body["env"] = env
        response = await client.post(route, json=body, headers=self._auth_headers())
        self._check(f"POST {route}", response)
        return _json_str_field(response, "id")

    # -- Execution ----------------------------------------------------------

    async def execute(
        self,
        prompt: str,
        workspace_id: str,
        *,
        model: str = "codex-mini",
        timeout_minutes: int | None = None,
        workspace_tar: bytes | None = None,
        on_output: Callable[[str, bytes], None] | None = None,
    ) -> CodexSandboxResult:
        """Run Codex on the bridge and return the result with its evidence.

        The full lifecycle: create the sandbox, seed the workspace when a tar
        is supplied, open a session carrying the agent's environment, stream
        ``/exec`` decoding base64 output frames as they arrive, persist the
        workspace, and delete the sandbox. The delete runs in a ``finally``, so
        a cancelled or failed run still stops the remote container instead of
        leaving it running and billing.

        Args:
            prompt: Task prompt handed to the coding agent. Passed as a single
                argv element, never through a shell.
            workspace_id: Caller-side workspace identifier, used for logging
                and correlation only.
            model: Model name passed to the agent CLI.
            timeout_minutes: Wall-clock ceiling for this run. Defaults to the
                config's ``max_execution_minutes``.
            workspace_tar: Optional tar of the starting workspace, hydrated
                into the sandbox before the run and used as the baseline for
                the returned diff.
            on_output: Optional callback invoked as ``(stream, chunk)`` for
                each decoded output frame, where ``stream`` is ``"stdout"`` or
                ``"stderr"``. Called during the stream, not after it.

        Returns:
            The :class:`CodexSandboxResult` for the run.

        Raises:
            CodexCloudflareNotConfiguredError: If no bridge is configured. The
                adapter never falls back to running the agent locally.
            CodexCloudflarePayloadTooLargeError: If *workspace_tar* exceeds the
                bridge's 32 MiB cap.
            CodexCloudflareBridgeApiError: If a bridge route fails.
        """
        self._require_configured()
        minutes = timeout_minutes if timeout_minutes is not None else self._config.max_execution_minutes
        timeout_ms = max(1, int(minutes * 60_000))
        stream_timeout = timeout_ms / 1000.0 + self._config.request_timeout_seconds
        before = _tar_index(workspace_tar) if workspace_tar else {}
        argv = [*self._config.agent_command, "--model", model, prompt]

        async with self._client(timeout=stream_timeout) as client:
            sandbox_id = await self._create_sandbox(client)
            logger.info(
                "Cloudflare sandbox %s created for workspace %s (bridge API %s)",
                sandbox_id,
                workspace_id,
                self._api_version,
            )
            try:
                if workspace_tar:
                    await self._hydrate(client, sandbox_id, workspace_tar)
                session_id = await self._create_session(client, sandbox_id)
                started = time.monotonic()
                outcome = await self._stream_exec(
                    client,
                    sandbox_id,
                    session_id=session_id,
                    argv=argv,
                    timeout_ms=timeout_ms,
                    on_output=on_output,
                )
                elapsed = time.monotonic() - started
                tar_bytes = await self._persist(client, sandbox_id)
            finally:
                await self._reap(client, sandbox_id)

        evidence = SandboxEvidence(
            terminal_snapshot_digest=hashlib.sha256(tar_bytes).hexdigest(),
            isolation=REMOTE_ISOLATION,
            sandbox_id=sandbox_id,
            bridge_api_version=self._api_version,
            bridge_sdk_version=BRIDGE_SDK_VERSION,
            workspace_bytes=len(tar_bytes),
        )
        stdout = outcome.stdout.decode("utf-8", errors="replace")
        stderr = outcome.stderr.decode("utf-8", errors="replace")
        self._transcripts[sandbox_id] = stdout + stderr
        return CodexSandboxResult(
            sandbox_id=sandbox_id,
            status=outcome.status,
            files_changed=_diff_paths(before, _tar_index(tar_bytes)),
            stdout=stdout,
            stderr=stderr,
            exit_code=outcome.exit_code,
            execution_time_seconds=elapsed,
            workspace_tar=tar_bytes,
            evidence=evidence,
            error=outcome.error,
        )

    async def _stream_exec(
        self,
        client: httpx.AsyncClient,
        sandbox_id: str,
        *,
        session_id: str,
        argv: list[str],
        timeout_ms: int,
        on_output: Callable[[str, bytes], None] | None,
    ) -> _ExecOutcome:
        """Drive ``POST /exec`` and fold its SSE frames into an outcome.

        The body carries exactly the published ``ExecRequest`` fields
        (``argv``, ``timeout_ms``, ``cwd``); environment is delivered by the
        session referenced through the ``Session-Id`` header, which this route
        honours.
        """
        route = f"{BRIDGE_API_PREFIX}/sandbox/{sandbox_id}/exec"
        headers = {**self._auth_headers(), "Accept": "text/event-stream"}
        if session_id:
            headers["Session-Id"] = session_id
        body = {"argv": argv, "timeout_ms": timeout_ms, "cwd": self._config.workdir}
        stdout = bytearray()
        stderr = bytearray()
        exit_code = 0
        status = "failed"
        error = ""
        pending: list[str] = []

        async with client.stream("POST", route, json=body, headers=headers) as response:
            if response.status_code >= 400:
                await response.aread()
                self._check(f"POST {route}", response)
            async for line in response.aiter_lines():
                pending.append(line)
                if line.strip():
                    continue
                for frame in _parse_sse_frames(pending):
                    if frame.event in ("stdout", "stderr"):
                        chunk = _decode_stream_payload(frame.data)
                        if frame.event == "stdout":
                            stdout.extend(chunk)
                        else:
                            stderr.extend(chunk)
                        if on_output is not None:
                            on_output(frame.event, chunk)
                    elif frame.event == "exit":
                        exit_code = _int_field(frame.data, "exit_code")
                        status = "completed" if exit_code == 0 else "failed"
                    elif frame.event == "error":
                        error = _str_field(frame.data, "error")
                        status = "timeout" if _looks_like_timeout(error) else "failed"
                pending = []

        return _ExecOutcome(
            status=status,
            exit_code=exit_code,
            stdout=bytes(stdout),
            stderr=bytes(stderr),
            error=error,
        )

    async def get_logs(self, sandbox_id: str) -> str:
        """Return the transcript this adapter buffered for *sandbox_id*.

        The bridge exposes no log-retrieval route: output exists only on the
        ``/exec`` SSE stream while the command runs. What survives afterwards
        is what the adapter recorded, and that is what this returns rather than
        pretending a remote log store exists.

        Args:
            sandbox_id: The sandbox identifier.

        Returns:
            The merged stdout+stderr transcript, or an empty string when this
            adapter instance did not run that sandbox.
        """
        return self._transcripts.get(sandbox_id, "")


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


#: Substrings that mark a bridge ``error`` event as a timeout rather than a
#: generic failure. The terminal error frame carries only ``{error, code}``,
#: and ``code`` is ``exec_error``/``exec_transport_error`` for every cause, so
#: the human-readable message is the sole timeout signal available. Both
#: spellings are matched because the wording comes from the container runtime,
#: not from a stable enum.
_TIMEOUT_MARKERS = ("timeout", "timed out")


def _looks_like_timeout(message: str) -> bool:
    """True when a bridge error message reads as a timeout."""
    lowered = message.lower()
    return any(marker in lowered for marker in _TIMEOUT_MARKERS)


def _same_major_series(observed: str, expected: str) -> bool:
    """True when *observed* shares a major version with *expected*."""
    if not observed:
        return False
    obs = observed.strip().lstrip("v").split(".")
    return bool(obs[0]) and obs[0] == expected.split(".")[0]


def _document_api_version(document: Mapping[str, Any]) -> str:
    """Read ``info.version`` (the API contract version) from an OpenAPI document."""
    info = document.get("info")
    version = info.get("version") if isinstance(info, dict) else None
    return version if isinstance(version, str) else ""


def _document_paths(document: Mapping[str, Any]) -> frozenset[str]:
    """Read the templated path keys published by an OpenAPI document."""
    paths = document.get("paths")
    if not isinstance(paths, dict):
        return frozenset()
    return frozenset(str(key) for key in paths)


def _as_int(value: Any) -> int:
    """Coerce a JSON number to ``int``, defaulting to 0."""
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _json_str_field(response: httpx.Response, key: str) -> str:
    """Read a string field from a JSON response body, or ``""``."""
    try:
        payload: Any = response.json()
    except ValueError:
        return ""
    if not isinstance(payload, dict):
        return ""
    value = payload.get(key)
    return value if isinstance(value, str) else ""


def _int_field(payload: str, key: str) -> int:
    """Read an integer field from a JSON SSE payload, defaulting to 0."""
    try:
        document: Any = json.loads(payload)
    except (ValueError, TypeError):
        return 0
    return _as_int(document.get(key)) if isinstance(document, dict) else 0


def _str_field(payload: str, key: str) -> str:
    """Read a string field from a JSON SSE payload, defaulting to the raw text."""
    try:
        document: Any = json.loads(payload)
    except (ValueError, TypeError):
        return payload
    value = document.get(key) if isinstance(document, dict) else None
    return value if isinstance(value, str) else payload


__all__ = [
    "BRIDGE_API_CONTRACT_VERSION",
    "BRIDGE_API_PREFIX",
    "BRIDGE_MAX_PAYLOAD_BYTES",
    "BRIDGE_PACKAGE",
    "BRIDGE_SDK_VERSION",
    "DEFAULT_WORKDIR",
    "REMOTE_ISOLATION",
    "REQUIRED_BRIDGE_PATHS",
    "RETIRED_CONFIG_FIELDS",
    "BridgePreflight",
    "CancelOutcome",
    "CodexCloudflareAdapter",
    "CodexCloudflareBridgeApiError",
    "CodexCloudflareBridgeAuthError",
    "CodexCloudflareBridgeContractError",
    "CodexCloudflareBridgeVersionError",
    "CodexCloudflareCancelError",
    "CodexCloudflareConfigError",
    "CodexCloudflareError",
    "CodexCloudflareNotConfiguredError",
    "CodexCloudflarePayloadTooLargeError",
    "CodexSandboxConfig",
    "CodexSandboxResult",
    "SandboxEvidence",
    "WarmPoolStats",
]
