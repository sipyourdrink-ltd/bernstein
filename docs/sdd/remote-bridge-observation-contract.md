# Observed remote bridge outcomes

`bernstein.bridges.base` provides an additive typed observation contract for
RuntimeBridge implementations. This is one complete partial component of
#6992, under #6991. It supplies types and optional capability methods; the
observed-event journal and fake-service acceptance criteria remain open.

The contract lives in the existing base module and imports only the Python
standard library. The legacy `AgentState`, `spawn`, `status`, `cancel`, `logs`
and `name` API remains unchanged. Existing concrete bridges remain valid.

## Types and identity

`RemoteRunHandle(service, remote_run_id, adapter_version, submitted_at,
console_url=None)` freezes the complete remote identity binding. The three
identity strings must be nonblank; the optional console URL must be a nonblank
string when supplied. Values are preserved exactly, without trimming or
coercion. The console URL is a label; the contract does not open or authenticate
it. Identity validation is not remote identity authentication.

`RemoteRunStatus(handle, state, observed_at, detail="")` and
`RemoteCancel(handle, outcome, observed_at, detail="")` retain the full typed
handle. All three DTOs are frozen and contain no mutable metadata maps.
Timestamps are finite nonnegative epoch seconds supplied as integers or
floats; booleans and numeric strings are rejected. Values outside the finite
floating-point range are rejected. No clock read or timestamp normalization
occurs. Detail must be a string and is excluded from the observation repr.

## Observed lifecycle

`RemoteRunState` has exactly these values:

| State | Observed lifecycle terminal? |
| --- | --- |
| `submitted` | No |
| `working` | No |
| `input-required` | No |
| `auth-required` | No |
| `completed` | Yes |
| `failed` | Yes |
| `canceled` | Yes |
| `unknown` | No |

`state.is_terminal` describes the service lifecycle observation only. It does
not establish TaskStore acceptance, delivery, authorization or local closure.
`RemoteCancelOutcome` is independently typed as `confirmed`, `unconfirmed` or
`unknown`; these outcomes grant no fallback or closure permission themselves.
DTO constructors require the declared enum instance and reject strings, other
enums and unknown values rather than translating them.

## Optional capabilities

`await bridge.remote_status(handle)` returns `RemoteRunStatus` when implemented.
`await bridge.cancel_remote(handle)` returns `RemoteCancel` when implemented.
Both base implementations raise `BridgeCapabilityError`, a `BridgeError` with
an explicit `capability` attribute. They call no legacy bridge method. In
particular, legacy `cancel(agent_id) -> None` cannot become confirmed remote
termination through this API.

## Decision and remaining integration

Decision `d-261115-0fa4` selected an additive contract to preserve existing
bridge implementations. ACH dissented because no current observed-event
journal consumer exists. That limitation remains material: the component
makes no remote governance readiness claim.

Submission and frozen execution rules, remote-event observation and dedup,
chain identity, restart reattachment, approvals, artifacts, budget/wall-clock
enforcement, fallback and closure belong to later work. No actual service,
credential flow or execution route is selected here.

See [operator status](../operations/remote-bridge-observations.md) for the
meaning of this partial component in an installed system.
