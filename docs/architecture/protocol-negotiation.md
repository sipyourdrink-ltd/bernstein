# Protocol version negotiation

**Two peers each support a range of MCP/A2A/ACP versions - which one do they actually speak?**

Bernstein talks to external peers over three wire protocols - MCP, A2A,
and ACP - and each protocol accumulates versions over time (new
capabilities added in a minor bump, breaking changes in a major bump).
`protocol_negotiation.py` is a small, dependency-free computation
library that takes the local and remote version lists for one protocol
and returns the highest version both sides can speak, plus a list of
capabilities that get dropped if the negotiated version is lower than
the local best.

If you only have time for one sentence: **`negotiate_version(local,
remote)` picks the highest common major version, then the highest
common minor within it, and reports which capabilities were lost in
`degraded_capabilities`**. The rest of this page covers the algorithm
and how to call it.

---

## What it computes

Three protocol identifiers are recognised: `mcp`, `a2a`, `acp`
(`ProtocolName` enum). Each version is a `ProtocolVersion(protocol,
major, minor, patch, capabilities)` dataclass - `capabilities` is a
frozenset of capability strings supported at that version.

`negotiate_version(local_versions, remote_versions)` returns a
`NegotiationResult`:

| Field | Meaning |
|---|---|
| `local_version` | The local side's highest version, regardless of outcome. |
| `remote_version` | The remote side's highest version, regardless of outcome. |
| `negotiated_version` | The agreed version, or `None` if negotiation failed. |
| `degraded_capabilities` | Capabilities present on `local_version` but absent from `negotiated_version`. |
| `success` | `True` if a common major version exists. |

## The algorithm

Source: `negotiate_version` in `protocol_negotiation.py`.

1. Both version lists are sorted ascending by `(major, minor, patch)`.
2. Negotiation fails (`success=False`, `negotiated_version=None`) if
   local and remote share no major version at all.
3. Otherwise the **highest common major** version is selected.
4. Within that major, the **highest common minor** is preferred. If no
   minor is shared, the algorithm falls back to the lower side's
   highest minor (so negotiation degrades gracefully instead of
   failing outright).
5. `degraded_capabilities` is computed as `local_best.capabilities -
   negotiated.capabilities` - i.e. what the local side loses by
   settling on the negotiated version, not what the remote side loses.

`version_is_compatible(local, remote)` is a cheaper standalone check:
it only compares major versions, with no minor-resolution or
capability diff.

## Usage

There is no CLI command for this module - it is a pure Python API
called by whatever component owns a given protocol connection.

```python
from bernstein.core.protocols.protocol_negotiation import (
    ProtocolVersion,
    get_supported_versions,
    negotiate_version,
)

local = get_supported_versions("mcp")
remote = [ProtocolVersion(protocol="mcp", major=1, minor=0, patch=0)]

result = negotiate_version(local, remote)
if result.success:
    print(result.negotiated_version.version_string)   # "1.0.0"
    print(sorted(result.degraded_capabilities))        # capabilities lost vs. local best
```

`get_supported_versions(protocol)` returns Bernstein's own hardcoded
version list for `mcp`, `a2a`, or `acp` (`_SUPPORTED_VERSIONS` in
`protocol_negotiation.py`). For `mcp` specifically, the returned
capability sets are filtered through the stateless-MCP compatibility
shim (`bernstein.core.protocols.mcp.stateless_core`): the deprecated
Roots/Sampling/Logging capabilities are advertised only while the shim
window is open, and disappear from the returned `ProtocolVersion` once
`months_since_deprecation` passes the shim's removal date. Pass
`months_since_deprecation` explicitly to test the post-removal shape
without waiting on the wall clock.

## Limitations

- **Pure computation, no wire I/O.** The module does not open a
  connection, read a handshake payload, or call `negotiate_version`
  automatically on any Bernstein transport. It is a version-arithmetic
  library; the caller is responsible for fetching the remote peer's
  supported-version list and acting on the `NegotiationResult` (e.g.
  refusing a connection, or advertising a reduced capability set).
- Only major/minor/patch and a flat capability-string set are modeled.
  There is no concept of capability *dependencies* (e.g. "streaming
  requires push_notifications") - two capabilities are independent as
  far as this module is concerned.

## Source

`src/bernstein/core/protocols/protocol_negotiation.py`
