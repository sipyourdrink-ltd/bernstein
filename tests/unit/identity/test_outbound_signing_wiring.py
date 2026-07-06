"""Tests that outbound agent-facing call sites carry install-identity signatures.

Covers issue #2305 AC1 wiring for the f06 (browser/research) and f15 (A2A)
outbound paths.
"""

from __future__ import annotations

import pytest

from bernstein.core.identity import http_signing
from bernstein.core.security.agent_card_keystore import AgentCardKeystore


@pytest.fixture(autouse=True)
def _isolated_keystore(tmp_path, monkeypatch):
    monkeypatch.setenv(http_signing.ENV_KEY_DIR, str(tmp_path / "keys"))
    monkeypatch.delenv(http_signing.ENV_SIGNING_REQUIRED, raising=False)
    yield


def _keydir(tmp_path):
    return http_signing.build_key_directory(AgentCardKeystore(tmp_path / "keys"))


class TestA2AFetchSigned:
    @pytest.mark.asyncio
    async def test_fetch_peer_card_http_signs_request(self, tmp_path):
        import httpx

        from bernstein.core.interop import a2a_consume

        captured: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured.update(dict(request.headers))
            return httpx.Response(200, json={"card": {}, "detached_jws": "x", "kid": "k"})

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            with pytest.raises(Exception):  # noqa: B017 - parse failure is fine; we only assert headers
                await a2a_consume.fetch_peer_card_http("https://peer.example", client=client)

        assert "signature-input" in {k.lower() for k in captured}
        assert "signature" in {k.lower() for k in captured}
        # The signature verifies against the published key directory.
        url = "https://peer.example" + a2a_consume.PEER_CARD_PATH
        assert http_signing.verify_request(
            method="GET",
            url=url,
            headers=captured,
            key_directory=_keydir(tmp_path),
        )


class TestBrowserRenderingSigned:
    @pytest.mark.asyncio
    async def test_render_signs_request(self, tmp_path):
        import httpx

        from bernstein.bridges.browser_rendering import BrowserConfig, BrowserRenderingBridge

        captured: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured.update(dict(request.headers))
            return httpx.Response(200, json={"success": True, "title": "t", "content": "c"})

        config = BrowserConfig(account_id="acct", api_token="tok")
        bridge = BrowserRenderingBridge(config)
        bridge._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            await bridge.render("https://target.example/page")
        finally:
            await bridge._client.aclose()

        keys = {k.lower() for k in captured}
        assert "signature-input" in keys
        assert "signature" in keys


class TestSignatureAttestation:
    @pytest.mark.asyncio
    async def test_signature_is_recorded_in_audit_chain(self, tmp_path):
        import httpx

        from bernstein.bridges.browser_rendering import BrowserConfig, BrowserRenderingBridge
        from bernstein.core.security.audit import AuditLog

        audit_dir = tmp_path / "audit"
        audit_dir.mkdir()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"success": True, "title": "t", "content": "c"})

        config = BrowserConfig(account_id="acct", api_token="tok")
        bridge = BrowserRenderingBridge(config, audit_dir=audit_dir)
        bridge._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            await bridge.render("https://target.example/page")
        finally:
            await bridge._client.aclose()

        log = AuditLog(audit_dir=audit_dir)
        events = log.query(event_type="identity.http_signature")
        assert len(events) == 1
        assert events[0].details["call_site"] == "browser.render"
        assert events[0].details["signature"].startswith("sig1=:")
        # The chain the attestation lands in verifies.
        valid, errors = log.verify()
        assert valid, errors
