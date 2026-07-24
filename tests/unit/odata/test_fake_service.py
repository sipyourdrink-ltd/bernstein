"""Trust tests for the hermetic fake OData service.

These pin the fake's behaviour so the ``odata_poll`` / ``odata_writeback`` suites
can rely on it as ground truth.
"""

from __future__ import annotations

from tests.unit.odata.fake_service import BASE_URL, FakeODataService, split_delta_token


def _svc() -> FakeODataService:
    svc = FakeODataService(page_size=2)
    svc.seed(1, timestamp="2026-01-01T00:00:01Z", name="alpha")
    svc.seed(2, timestamp="2026-01-01T00:00:02Z", name="beta")
    svc.seed(3, timestamp="2026-01-01T00:00:03Z", name="gamma")
    return svc


def test_metadata_names_the_key() -> None:
    svc = _svc()
    with svc.client() as client:
        resp = client.get("/$metadata")
    assert resp.status_code == 200
    assert '<PropertyRef Name="id"/>' in resp.text
    assert 'Name="Widgets"' in resp.text


def test_collection_paging_follows_next_link() -> None:
    svc = _svc()
    seen: list[int] = []
    with svc.client() as client:
        url = f"{BASE_URL}/Widgets?$orderby=modified"
        while url:
            body = client.get(url).json()
            seen.extend(row["id"] for row in body["value"])
            url = body.get("@odata.nextLink", "")
    assert seen == [1, 2, 3]


def test_watermark_filter_excludes_old_rows() -> None:
    svc = _svc()
    with svc.client() as client:
        body = client.get(f"{BASE_URL}/Widgets?$filter=modified gt 2026-01-01T00:00:02Z").json()
    assert [row["id"] for row in body["value"]] == [3]


def test_delta_link_only_with_prefer_and_enabled() -> None:
    svc = _svc()
    with svc.client() as client:
        # Walk to the last page carrying the Prefer header.
        url = f"{BASE_URL}/Widgets"
        body = client.get(url, headers={"Prefer": "odata.track-changes"}).json()
        while "@odata.nextLink" in body:
            body = client.get(body["@odata.nextLink"], headers={"Prefer": "odata.track-changes"}).json()
    assert "@odata.deltaLink" in body


def test_delta_disabled_service_never_returns_delta_link() -> None:
    svc = FakeODataService(page_size=10, delta_enabled=False)
    svc.seed(1, timestamp="2026-01-01T00:00:01Z", name="alpha")
    with svc.client() as client:
        body = client.get(f"{BASE_URL}/Widgets", headers={"Prefer": "odata.track-changes"}).json()
    assert "@odata.deltaLink" not in body


def test_delta_page_reports_changes_and_tombstones() -> None:
    svc = FakeODataService(page_size=10)
    svc.seed(1, timestamp="2026-01-01T00:00:01Z", name="alpha")
    with svc.client() as client:
        body = client.get(f"{BASE_URL}/Widgets", headers={"Prefer": "odata.track-changes"}).json()
        token = split_delta_token(body["@odata.deltaLink"])
        svc.seed(2, timestamp="2026-01-01T00:00:05Z", name="beta")
        svc.delete(1)
        delta = client.get(f"{BASE_URL}/Widgets?$deltatoken={token}").json()
    ids_added = [row["id"] for row in delta["value"] if "@removed" not in row]
    ids_removed = [row["id"] for row in delta["value"] if "@removed" in row]
    assert ids_added == [2]
    assert ids_removed == [1]


def test_patch_requires_if_match_428() -> None:
    svc = _svc()
    with svc.client() as client:
        resp = client.patch(f"{BASE_URL}/Widgets(1)", json={"name": "renamed"})
    assert resp.status_code == 428


def test_patch_stale_etag_412() -> None:
    svc = _svc()
    with svc.client() as client:
        etag = client.get(f"{BASE_URL}/Widgets(1)").json()["@odata.etag"]
        # A concurrent edit bumps the server etag.
        svc.seed(1, timestamp="2026-01-01T00:00:09Z", name="concurrent")
        resp = client.patch(f"{BASE_URL}/Widgets(1)", json={"name": "renamed"}, headers={"If-Match": etag})
    assert resp.status_code == 412


def test_patch_match_applies_and_bumps_etag() -> None:
    svc = _svc()
    with svc.client() as client:
        etag = client.get(f"{BASE_URL}/Widgets(1)").json()["@odata.etag"]
        resp = client.patch(f"{BASE_URL}/Widgets(1)", json={"name": "renamed"}, headers={"If-Match": etag})
    assert resp.status_code == 200
    assert resp.json()["name"] == "renamed"
    assert resp.json()["@odata.etag"] != etag


def test_draft_activate_flow() -> None:
    svc = _svc()
    with svc.client() as client:
        draft = client.post(f"{BASE_URL}/Widgets", json={"name": "draft"}).json()
        draft_key = draft["id"]
        etag = draft["@odata.etag"]
        client.patch(f"{BASE_URL}/Widgets({draft_key})", json={"name": "draft-edited"}, headers={"If-Match": etag})
        activated = client.post(f"{BASE_URL}/Widgets({draft_key})/Activate").json()
    assert activated["name"] == "draft-edited"
    with svc.client() as client:
        # The activated entity is now readable in the active set.
        assert client.get(f"{BASE_URL}/Widgets({draft_key})").status_code == 200


def test_throttle_plan_emits_429_then_recovers() -> None:
    svc = FakeODataService(page_size=10, throttle_first_n=1, throttle_retry_after=7)
    svc.seed(1, timestamp="2026-01-01T00:00:01Z", name="alpha")
    with svc.client() as client:
        first = client.get(f"{BASE_URL}/Widgets")
        second = client.get(f"{BASE_URL}/Widgets")
    assert first.status_code == 429
    assert first.headers["Retry-After"] == "7"
    assert second.status_code == 200
