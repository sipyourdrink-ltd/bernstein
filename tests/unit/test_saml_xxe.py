"""Regression tests for audit-054: SAML XXE / billion-laughs DoS.

``AuthService.parse_saml_assertion`` used to parse IdP-supplied XML with
stdlib ``xml.etree.ElementTree``. stdlib blocks *external* entities, but
it still expands *internal* entity declarations, which is enough for a
billion-laughs / quadratic-blowup DoS on the unauthenticated SAML ACS
endpoint (memory + CPU exhaustion blocking the event loop).

The fix switches to ``defusedxml.ElementTree``, which refuses DTDs,
external entities, and internal entity definitions outright. These tests
assert that:

* a nested-entity-bomb SAML payload is rejected *quickly* (well under
  the time it would take to actually expand it);
* a malformed XML document still gets a polite ``None`` return instead
  of crashing;
* a well-formed, entity-free payload still parses successfully (so the
  defusedxml swap is a true drop-in).

Time-based assertions use a generous upper bound to tolerate slow CI
runners while still catching a regression where the entities are
actually expanded (which would take seconds to minutes).
"""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import time
from pathlib import Path

import pytest

from bernstein.core.security.auth import (
    AuthService,
    AuthStore,
    SSOConfig,
)

# A classic "billion laughs" payload wrapped in a minimal SAML Response so
# _saml_status_ok has something to look at if parsing ever did succeed.
# Nine levels nested x 10 per level would expand to ~10**9 "lol" tokens
# under a naive parser; defusedxml must refuse it before expansion starts.
_BILLION_LAUGHS_SAML = """<?xml version="1.0"?>
<!DOCTYPE samlp:Response [
  <!ENTITY lol "lol">
  <!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
  <!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">
  <!ENTITY lol4 "&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;">
  <!ENTITY lol5 "&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;">
  <!ENTITY lol6 "&lol5;&lol5;&lol5;&lol5;&lol5;&lol5;&lol5;&lol5;&lol5;&lol5;">
  <!ENTITY lol7 "&lol6;&lol6;&lol6;&lol6;&lol6;&lol6;&lol6;&lol6;&lol6;&lol6;">
  <!ENTITY lol8 "&lol7;&lol7;&lol7;&lol7;&lol7;&lol7;&lol7;&lol7;&lol7;&lol7;">
  <!ENTITY lol9 "&lol8;&lol8;&lol8;&lol8;&lol8;&lol8;&lol8;&lol8;&lol8;&lol8;">
]>
<samlp:Response xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol"
    xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion">
  <samlp:Status>
    <samlp:StatusCode Value="urn:oasis:names:tc:SAML:2.0:status:Success"/>
  </samlp:Status>
  <saml:Assertion>
    <saml:Subject><saml:NameID>&lol9;</saml:NameID></saml:Subject>
  </saml:Assertion>
</samlp:Response>"""


_QUADRATIC_BLOWUP_SAML = """<?xml version="1.0"?>
<!DOCTYPE foo [
  <!ENTITY a "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa">
]>
<samlp:Response xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol">
    <samlp:Status>
        <samlp:StatusCode Value="urn:oasis:names:tc:SAML:2.0:status:Success"/>
    </samlp:Status>
    <data>&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;</data>
</samlp:Response>"""


# A sane, entity-free payload to confirm defusedxml is a true drop-in.
_BENIGN_SAML = """<samlp:Response
    xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol"
    xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion">
  <samlp:Status>
    <samlp:StatusCode Value="urn:oasis:names:tc:SAML:2.0:status:Success"/>
  </samlp:Status>
  <saml:Assertion>
    <saml:Subject><saml:NameID>bob@example.com</saml:NameID></saml:Subject>
    <saml:AttributeStatement>
      <saml:Attribute Name="email">
        <saml:AttributeValue>bob@example.com</saml:AttributeValue>
      </saml:Attribute>
    </saml:AttributeStatement>
  </saml:Assertion>
</samlp:Response>"""


@pytest.fixture()
def svc(tmp_path: Path) -> AuthService:
    """Minimal AuthService wired enough to call parse_saml_assertion."""
    config = SSOConfig(
        enabled=True,
        jwt_secret="test-jwt-secret-for-unit-tests",  # NOSONAR — test fixture
        jwt_expiry_seconds=3600,
        session_expiry_seconds=3600,
        default_role="viewer",
    )
    config.saml.attr_email = "email"
    store = AuthStore(tmp_path)
    return AuthService(config, store)


class TestSAMLXXEDefense:
    """audit-054: internal-entity expansion must be refused, not expanded."""

    def test_billion_laughs_is_rejected_quickly(self, svc: AuthService) -> None:
        """Nested entity bomb must be refused before expansion can blow up.

        If this ever regresses to stdlib ``xml.etree``, the call would
        either OOM the test runner or take many seconds expanding
        ~10**9 "lol" tokens. defusedxml raises immediately.
        """
        start = time.monotonic()
        result = svc.parse_saml_assertion(_BILLION_LAUGHS_SAML)
        elapsed = time.monotonic() - start

        assert result is None, "billion-laughs payload must be refused"
        # 2s is deliberately generous for slow CI; a regression would take
        # orders of magnitude longer.
        assert elapsed < 2.0, f"parse took {elapsed:.2f}s — entities may be expanding"

    def test_quadratic_blowup_is_rejected(self, svc: AuthService) -> None:
        """Even a shallow DTD with one entity must be refused."""
        start = time.monotonic()
        result = svc.parse_saml_assertion(_QUADRATIC_BLOWUP_SAML)
        elapsed = time.monotonic() - start

        assert result is None
        assert elapsed < 2.0

    def test_malformed_xml_returns_none(self, svc: AuthService) -> None:
        """Classic ParseError path still degrades gracefully."""
        assert svc.parse_saml_assertion("<not-xml") is None
        assert svc.parse_saml_assertion("garbage") is None

    def test_benign_assertion_still_parses(self, svc: AuthService) -> None:
        """defusedxml must remain a drop-in for well-formed entity-free XML."""
        result = svc.parse_saml_assertion(_BENIGN_SAML)
        assert result is not None, "plain SAML must still parse"
        assert result.subject == "bob@example.com"
        assert result.email == "bob@example.com"
