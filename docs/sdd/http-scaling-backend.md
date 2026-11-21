# HTTP scaling backend

`core/protocols/cluster/http_scaling_backend.py` implements the existing
`ScalingBackend` contract with `httpx`. It is opt-in through explicit
construction and has no plugin registration or scheduler wiring.
Contract tests use mocked HTTP only in `tests/unit/test_http_scaling_backend.py`.
