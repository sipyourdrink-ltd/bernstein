"""Canonical event-type catalogue for tracker ingest (#5132)."""

from __future__ import annotations

import ast
import hashlib
import hmac
import json
import logging
import textwrap
from pathlib import Path

import pytest
import yaml

from bernstein.core.trackers.event_catalogue import (
    DEFAULT_CATALOGUE_PATH,
    EventCatalogue,
    EventCatalogueError,
    catalogue_content_hash,
    get_event_catalogue_runtime,
    load_event_catalogue,
    reset_event_catalogue_runtime,
)
from bernstein.core.trackers.webhook_receiver import (
    ReplayLedger,
    WebhookConfig,
    WebhookReceiver,
    _github_parse,
    _gitlab_parse,
    register_builtin_handlers,
)

_WEBHOOK_RECEIVER_PATH = (
    Path(__file__).resolve().parents[3] / "src" / "bernstein" / "core" / "trackers" / "webhook_receiver.py"
)

# Source-event names that must not appear as string-literal comparisons inside
# the GitHub/GitLab ingest parsers (they live in the catalogue instead).
_FORBIDDEN_EVENT_LITERALS = frozenset(
    {
        "issues",
        "issue_comment",
        "pull_request",
        "issue",
        "note",
        "workflow_run",
        "push",
        "ping",
    }
)


def _string_constants(node: ast.AST) -> list[str]:
    found: list[str] = []
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        found.append(node.value)
    elif isinstance(node, (ast.Set, ast.List, ast.Tuple)):
        for elt in node.elts:
            found.extend(_string_constants(elt))
    return found


@pytest.fixture(autouse=True)
def _restore_catalogue_runtime() -> None:
    """Keep the packaged catalogue as the process default after each test."""

    yield
    reset_event_catalogue_runtime()


def test_catalogue_loads_and_validates_at_start() -> None:
    catalogue = load_event_catalogue()
    assert catalogue.version >= 1
    assert catalogue.unmapped_canonical == "unmapped"
    assert DEFAULT_CATALOGUE_PATH.exists()
    assert any(e.adapter == "github" and e.source_event == "issues" for e in catalogue.entries)
    assert any(e.adapter == "gitlab" and e.source_event == "note" and e.shape == "note" for e in catalogue.entries)
    register_builtin_handlers()
    runtime = get_event_catalogue_runtime()
    assert runtime.content_hash == catalogue_content_hash(catalogue)


def test_malformed_catalogue_fails_validation(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("version: 1\nentries: []\n", encoding="utf-8")
    with pytest.raises(EventCatalogueError, match="validation"):
        load_event_catalogue(bad)

    missing_keys = tmp_path / "missing.yaml"
    missing_keys.write_text("version: 1\n", encoding="utf-8")
    with pytest.raises(EventCatalogueError):
        load_event_catalogue(missing_keys)


def test_unknown_source_event_maps_to_unmapped_and_is_counted(caplog: pytest.LogCaptureFixture) -> None:
    runtime = reset_event_catalogue_runtime()
    with caplog.at_level(logging.INFO, logger="bernstein.core.trackers.event_catalogue"):
        event = _github_parse({"x-github-event": "workflow_run"}, {"action": "completed"})
    assert event is None
    assert runtime.unmapped_counts()["github:workflow_run"] == 1
    assert any("Unmapped tracker source event" in r.message and "workflow_run" in r.message for r in caplog.records)

    # Second sight increments the counter but does not log again.
    with caplog.at_level(logging.INFO, logger="bernstein.core.trackers.event_catalogue"):
        caplog.clear()
        assert _github_parse({"x-github-event": "workflow_run"}, {"action": "completed"}) is None
    assert runtime.unmapped_counts()["github:workflow_run"] == 2
    assert not any("Unmapped tracker source event" in r.message for r in caplog.records)


def test_unmapped_delivery_leaves_queue_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unmapped events are classified/counted but must not reach task ingestion."""

    monkeypatch.setenv("TEST_WH_SECRET", "shh")
    register_builtin_handlers()
    receiver = WebhookReceiver()
    receiver.configure("github", WebhookConfig(enabled=True, secret_env="TEST_WH_SECRET"))
    body = json.dumps({"action": "completed"}).encode("utf-8")
    headers = {
        "x-hub-signature-256": "sha256=" + hmac.new(b"shh", body, hashlib.sha256).hexdigest(),
        "x-github-event": "ping",
        "x-github-delivery": "ping-1",
    }
    result = receiver.receive("github", headers, body)
    assert result.status == "ignored"
    assert result.event is None

    # Mirror routes/tracker_webhooks.py:109-116 enqueue gate.
    queue: list[object] = []
    if result.status == "accepted" and result.event is not None:
        queue.append(result.event)
    assert queue == []
    assert get_event_catalogue_runtime().unmapped_counts()["github:ping"] == 1


def test_added_catalogue_entry_accepted_without_code_change(tmp_path: Path) -> None:
    """Extending the YAML alone is enough for ingest to accept a new event type."""

    packaged = yaml.safe_load(DEFAULT_CATALOGUE_PATH.read_text(encoding="utf-8"))
    packaged["entries"].append(
        {
            "adapter": "github",
            "source_event": "discussion",
            "canonical": "github.discussion",
        }
    )
    path = tmp_path / "event_catalogue.yaml"
    path.write_text(yaml.safe_dump(packaged), encoding="utf-8")
    reset_event_catalogue_runtime(path=path)

    payload = {
        "action": "created",
        "issue": {
            "id": 9,
            "number": 9,
            "html_url": "https://github.com/acme/repo/discussions/9",
            "title": "RFC",
            "body": "body",
            "state": "open",
            "labels": [],
        },
        "repository": {"full_name": "acme/repo"},
    }
    event = _github_parse({"x-github-event": "discussion"}, payload)
    assert event is not None
    assert event.action == "created"
    assert event.ticket.id == "acme/repo#9"
    assert event.catalogue_content_hash == get_event_catalogue_runtime().content_hash


def test_github_parser_has_no_literal_event_name_comparisons() -> None:
    """Regression: ingest parsers must map through the catalogue, not literals."""

    source = _WEBHOOK_RECEIVER_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    targets = {"_github_parse", "_gitlab_parse"}
    functions = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in targets
    }
    assert targets <= set(functions), f"missing parser(s): {targets - set(functions)}"

    offenders: list[str] = []
    for name, fn in functions.items():
        for node in ast.walk(fn):
            if not isinstance(node, ast.Compare):
                continue
            candidates = _string_constants(node.left)
            for comparator in node.comparators:
                candidates.extend(_string_constants(comparator))
            hit = sorted(_FORBIDDEN_EVENT_LITERALS.intersection(candidates))
            if hit:
                offenders.append(f"{name}:{node.lineno}:{hit}")
    assert not offenders, "ingest parsers must not compare event-name literals; found: " + ", ".join(offenders)


def test_journal_entry_records_catalogue_content_hash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_WH_SECRET", "shh")
    register_builtin_handlers()
    runtime = get_event_catalogue_runtime()
    ledger_path = tmp_path / "ledger.jsonl"
    receiver = WebhookReceiver(ledger=ReplayLedger(ledger_path))
    receiver.configure("github", WebhookConfig(enabled=True, secret_env="TEST_WH_SECRET"))

    payload = {
        "action": "opened",
        "issue": {
            "id": 1,
            "number": 42,
            "html_url": "https://github.com/acme/repo/issues/42",
            "title": "Bug",
            "body": "x",
            "state": "open",
            "labels": [],
        },
        "repository": {"full_name": "acme/repo"},
    }
    body = json.dumps(payload).encode("utf-8")
    sig = "sha256=" + hmac.new(b"shh", body, hashlib.sha256).hexdigest()
    result = receiver.receive(
        "github",
        {
            "x-hub-signature-256": sig,
            "x-github-event": "issues",
            "x-github-delivery": "hash-1",
        },
        body,
    )
    assert result.status == "accepted"
    assert result.event is not None
    assert result.event.action == "opened"
    assert result.event.catalogue_content_hash == runtime.content_hash

    lines = [ln for ln in ledger_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["delivery_id"] == "github:hash-1"
    assert row["catalogue_content_hash"] == runtime.content_hash
    assert row["catalogue_content_hash"] == catalogue_content_hash(runtime.catalogue)


def test_catalogue_content_hash_stable_for_same_document(tmp_path: Path) -> None:
    raw = textwrap.dedent(
        """\
        version: 1
        unmapped_canonical: unmapped
        entries:
          - adapter: github
            source_event: issues
            canonical: github.issues
        """
    )
    path = tmp_path / "c.yaml"
    path.write_text(raw, encoding="utf-8")
    a = load_event_catalogue(path)
    b = EventCatalogue.model_validate(yaml.safe_load(raw))
    assert catalogue_content_hash(a) == catalogue_content_hash(b)


def test_gitlab_note_shape_still_parses_via_catalogue() -> None:
    reset_event_catalogue_runtime()
    payload = {
        "object_kind": "note",
        "object_attributes": {"action": "create", "note": "hi"},
        "issue": {
            "iid": 17,
            "title": "Bug",
            "description": "desc",
            "state": "opened",
            "url": "https://gitlab.example.com/acme/repo/-/issues/17",
        },
        "project": {"path_with_namespace": "acme/repo"},
        "labels": [],
    }
    event = _gitlab_parse({}, payload)
    assert event is not None
    assert event.ticket.id == "acme/repo#17"
    assert event.action == "create"
    assert event.catalogue_content_hash is not None
