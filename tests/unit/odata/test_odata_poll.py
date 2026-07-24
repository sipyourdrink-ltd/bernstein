"""Tests for :mod:`bernstein.core.trigger_sources.odata_poll`.

Covers acceptance criteria 1 (watermark loop + byte-identical restart replay),
2 (delta probe / fallback), 4 (429 + min-interval), and 5 (log sanitisation)
from issue #2886, driven entirely against the hermetic fake service.
"""

from __future__ import annotations

import logging

import pytest

from bernstein.core.trigger_sources.odata_poll import (
    OdataAuth,
    OdataConnection,
    OdataCursor,
    OdataHttpClient,
    OdataPollSource,
    discover_keys,
    load_cursor,
    save_cursor,
)
from tests.unit.odata.fake_service import FakeClock, FakeODataService


def _connection(**overrides: object) -> OdataConnection:
    base: dict[str, object] = {
        "service_root": "http://odata.test",
        "entity_set": "Widgets",
        "timestamp_property": "modified",
        "key_properties": ("id",),
        "name": "erp",
    }
    base.update(overrides)
    return OdataConnection(**base)  # type: ignore[arg-type]


def _source(svc: FakeODataService, *, clock: FakeClock | None = None, **conn: object) -> OdataPollSource:
    client = OdataHttpClient(_connection(**conn), http_client=svc.client(), clock=clock)
    return OdataPollSource(_connection(**conn), http_client=client)


# ---------------------------------------------------------------------------
# metadata / key discovery
# ---------------------------------------------------------------------------


def test_discover_keys_from_metadata() -> None:
    svc = FakeODataService()
    client = OdataHttpClient(_connection(key_properties=()), http_client=svc.client())
    assert discover_keys(_connection(key_properties=()), client) == ("id",)


# ---------------------------------------------------------------------------
# AC1 -- watermark loop + deterministic restart replay
# ---------------------------------------------------------------------------


def test_watermark_emits_ordered_events_and_advances_cursor() -> None:
    svc = FakeODataService(page_size=2)
    svc.seed(1, timestamp="2026-01-01T00:00:01Z", name="alpha")
    svc.seed(2, timestamp="2026-01-01T00:00:02Z", name="beta")
    svc.seed(3, timestamp="2026-01-01T00:00:03Z", name="gamma")

    source = _source(svc)
    result = source.poll(None)

    assert [e.metadata["entity_key"] for e in result.events] == ["id=1", "id=2", "id=3"]
    assert [e.raw_payload["name"] for e in result.events] == ["alpha", "beta", "gamma"]
    assert result.cursor.mode == "watermark"
    assert result.cursor.watermark == "2026-01-01T00:00:03Z"


def test_watermark_second_poll_only_returns_new_rows() -> None:
    svc = FakeODataService(page_size=10)
    svc.seed(1, timestamp="2026-01-01T00:00:01Z", name="alpha")

    source = _source(svc)
    first = source.poll(None)
    assert len(first.events) == 1

    svc.seed(2, timestamp="2026-01-01T00:00:05Z", name="beta")
    second = source.poll(first.cursor)
    assert [e.raw_payload["id"] for e in second.events] == [2]
    assert second.cursor.watermark == "2026-01-01T00:00:05Z"


def test_restart_from_persisted_cursor_no_dup_or_drop(tmp_path: object) -> None:
    import pathlib

    cursor_path = pathlib.Path(str(tmp_path)) / "cursor.json"
    svc = FakeODataService(page_size=1)
    svc.seed(1, timestamp="2026-01-01T00:00:01Z", name="alpha")
    svc.seed(2, timestamp="2026-01-01T00:00:02Z", name="beta")

    source = _source(svc)
    first = source.poll(None)
    save_cursor(cursor_path, first.cursor)

    # New process: reload the cursor and add a change.
    svc.seed(3, timestamp="2026-01-01T00:00:03Z", name="gamma")
    resumed = load_cursor(cursor_path)
    assert resumed == first.cursor

    source2 = _source(svc)
    second = source2.poll(resumed)
    ids = [e.raw_payload["id"] for e in second.events]
    assert ids == [3]  # no duplicate of 1/2, no drop of 3


def test_replay_is_byte_identical(tmp_path: object) -> None:
    import pathlib

    def run() -> tuple[list[dict[str, object]], OdataCursor]:
        svc = FakeODataService(page_size=2)
        svc.seed(1, timestamp="2026-01-01T00:00:01Z", name="alpha")
        svc.seed(2, timestamp="2026-01-01T00:00:02Z", name="beta")
        src = _source(svc)
        res = src.poll(None)
        payloads = [dict(e.raw_payload) for e in res.events]
        return payloads, res.cursor

    payloads_a, cursor_a = run()
    payloads_b, cursor_b = run()
    assert payloads_a == payloads_b
    assert cursor_a == cursor_b
    # And the persisted cursor form is byte-identical.
    p1 = pathlib.Path(str(tmp_path)) / "a.json"
    p2 = pathlib.Path(str(tmp_path)) / "b.json"
    save_cursor(p1, cursor_a)
    save_cursor(p2, cursor_b)
    assert p1.read_bytes() == p2.read_bytes()


# ---------------------------------------------------------------------------
# AC2 -- delta probe / fallback
# ---------------------------------------------------------------------------


def test_delta_mode_activates_only_when_probe_returns_delta_link() -> None:
    svc = FakeODataService(page_size=10, delta_enabled=True)
    svc.seed(1, timestamp="2026-01-01T00:00:01Z", name="alpha")

    source = _source(svc, prefer_delta=True)
    first = source.poll(None)
    assert first.cursor.mode == "delta"
    assert first.cursor.delta_link

    # A subsequent change is delivered through the delta link.
    svc.seed(2, timestamp="2026-01-01T00:00:05Z", name="beta")
    svc.delete(1)
    second = source.poll(first.cursor)
    kinds = {e.metadata["entity_key"]: e.metadata["change_kind"] for e in second.events}
    assert kinds == {"id=2": "upsert", "id=1": "delete"}
    assert second.cursor.mode == "delta"


def test_prefer_delta_but_service_omits_link_stays_watermark() -> None:
    svc = FakeODataService(page_size=10, delta_enabled=False)
    svc.seed(1, timestamp="2026-01-01T00:00:01Z", name="alpha")

    source = _source(svc, prefer_delta=True)
    first = source.poll(None)
    assert first.cursor.mode == "watermark"


def test_delta_failure_falls_back_to_watermark_without_losing_cursor() -> None:
    svc = FakeODataService(page_size=10, delta_enabled=True)
    svc.seed(1, timestamp="2026-01-01T00:00:01Z", name="alpha")

    source = _source(svc, prefer_delta=True)
    first = source.poll(None)
    assert first.cursor.mode == "delta"
    assert first.cursor.watermark == "2026-01-01T00:00:01Z"

    # The entity set silently drops delta support; the next delta read fails.
    svc.delta_broken = True
    svc.seed(2, timestamp="2026-01-01T00:00:05Z", name="beta")
    second = source.poll(first.cursor)

    assert second.cursor.mode == "watermark"  # downgraded
    assert [e.raw_payload["id"] for e in second.events] == [2]  # served via watermark
    assert second.cursor.watermark == "2026-01-01T00:00:05Z"  # position preserved + advanced


# ---------------------------------------------------------------------------
# AC4 -- 429 + Retry-After + minimum inter-call interval (fake clock)
# ---------------------------------------------------------------------------


def test_retry_after_honoured_with_fake_clock() -> None:
    svc = FakeODataService(page_size=10, throttle_first_n=1, throttle_retry_after=10)
    svc.seed(1, timestamp="2026-01-01T00:00:01Z", name="alpha")
    clock = FakeClock()

    source = _source(svc, clock=clock)
    result = source.poll(None)

    assert [e.raw_payload["id"] for e in result.events] == [1]
    assert 10 in clock.sleeps  # the Retry-After was slept off, not ignored


def test_minimum_inter_call_interval_honoured() -> None:
    svc = FakeODataService(page_size=1)
    svc.seed(1, timestamp="2026-01-01T00:00:01Z", name="alpha")
    svc.seed(2, timestamp="2026-01-01T00:00:02Z", name="beta")
    clock = FakeClock()

    # page_size=1 forces a nextLink follow, i.e. two HTTP calls in one poll.
    source = _source(svc, clock=clock, rate_limit_min_interval_s=5.0)
    source.poll(None)

    assert 5 in clock.sleeps


# ---------------------------------------------------------------------------
# AC5 -- credentials never appear in logs
# ---------------------------------------------------------------------------


def test_bearer_token_never_logged(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    monkeypatch.setenv("ODATA_TOKEN", "supersecret-bearer-value")
    svc = FakeODataService(page_size=10)
    svc.seed(1, timestamp="2026-01-01T00:00:01Z", name="alpha")
    auth = OdataAuth(kind="bearer", token_env="ODATA_TOKEN")

    caplog.set_level(logging.DEBUG, logger="bernstein.core.trigger_sources.odata_poll")
    source = _source(svc, auth=auth)
    # Also drive an error path (unknown entity) to exercise error logging.
    source.poll(None)
    with pytest.raises(Exception):  # noqa: B017 - any OData error is fine here
        _source(svc, entity_set="Missing").poll(None)

    assert "supersecret-bearer-value" not in caplog.text


def test_auth_header_is_sent_but_redacted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ODATA_TOKEN", "supersecret-bearer-value")
    svc = FakeODataService(page_size=10)
    svc.seed(1, timestamp="2026-01-01T00:00:01Z", name="alpha")
    auth = OdataAuth(kind="bearer", token_env="ODATA_TOKEN")
    client = OdataHttpClient(_connection(auth=auth), http_client=svc.client())
    headers = client.auth_headers()
    assert headers["Authorization"] == "Bearer supersecret-bearer-value"
    # The sanitiser must hide it.
    assert client.sanitize_headers(headers)["Authorization"] == "***"
