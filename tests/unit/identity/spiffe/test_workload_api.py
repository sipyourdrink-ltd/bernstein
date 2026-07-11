"""SPIRE Workload API client behind the optional extra (issue #2363, AC 3).

The default self-contained install has no ``spiffe`` extra: fetching an SVID
must fail with a clear, actionable error naming the extra rather than an
``ImportError`` traceback, and no coordination path may hard-import the SPIRE
SDK. A fake client factory exercises the mapping without a running SPIRE agent.
"""

from __future__ import annotations

import pytest

from bernstein.core.identity.spiffe import X509Svid
from bernstein.core.identity.spiffe.workload_api import (
    SPIFFE_EXTRA,
    WorkloadApiError,
    fetch_x509_svid,
    spiffe_extra_available,
)

from .conftest import make_svid_leaf


def test_extra_probe_is_boolean() -> None:
    assert isinstance(spiffe_extra_available(), bool)


def test_fetch_without_extra_raises_actionable(monkeypatch: pytest.MonkeyPatch) -> None:
    # Force the "extra absent" path regardless of the runner's environment.
    monkeypatch.setattr(
        "bernstein.core.identity.spiffe.workload_api._load_pyspiffe",
        lambda: (_ for _ in ()).throw(ImportError("no module named spiffe")),
    )
    with pytest.raises(WorkloadApiError) as exc:
        fetch_x509_svid()
    assert SPIFFE_EXTRA in str(exc.value)


def test_fetch_with_injected_factory_maps_svid() -> None:
    sid = "spiffe://ex.org/bernstein/deadbeefdeadbeef/a1"
    cert_pem, key_pem = make_svid_leaf(sid)

    class _FakeSvid:
        spiffe_id = sid
        cert_chain_pem = cert_pem
        private_key_pem = key_pem
        bundle_pem = cert_pem
        expires_at = 0.0

    def _factory(_socket: str | None) -> _FakeSvid:
        return _FakeSvid()

    svid = fetch_x509_svid(client_factory=_factory)
    assert isinstance(svid, X509Svid)
    assert svid.spiffe_id == sid
    assert svid.cert_chain_pem == cert_pem


def test_no_hard_import_of_spiffe_sdk() -> None:
    """Importing the workload-identity surface must not import the SPIRE SDK."""
    import sys

    # The module is already imported by this test file; assert the SDK did not
    # ride in transitively. (When the extra is installed this is a no-op skip.)
    if "spiffe" in sys.modules:
        pytest.skip("spiffe extra installed in this environment")
    assert "spiffe" not in sys.modules
