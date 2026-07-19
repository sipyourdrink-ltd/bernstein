"""Unit tests for the SLA contract store (#2549)."""

from __future__ import annotations

import logging
import os.path
from pathlib import Path

import pytest

from bernstein.core.planning.sla_store import (
    SLAContractError,
    SLAContractIdError,
    SLAStore,
    build_contract,
)


def test_add_is_idempotent_by_derived_id(tmp_path: Path) -> None:
    store = SLAStore(tmp_path / ".sdd")
    contract = build_contract(subject_type="schedule", subject_id="sched_x", fire_frequency_s=3600)
    first = store.add(contract, now=1.0)
    second = store.add(contract, now=999.0)
    assert first.id == second.id
    assert first.created_at == second.created_at == 1.0
    assert len(store.list()) == 1


def test_persist_and_reload_preserves_hash(tmp_path: Path) -> None:
    store = SLAStore(tmp_path / ".sdd")
    contract = build_contract(
        subject_type="envelope",
        subject_id="subscription",
        spend_rate_usd_per_hour=2.5,
        remediation_cost_usd=1.0,
    )
    stored = store.add(contract)
    reloaded = store.get(stored.id)
    assert reloaded is not None
    assert reloaded.contract_hash == stored.contract_hash
    assert reloaded.spend_rate_usd_per_hour == 2.5


def test_for_subject_filters(tmp_path: Path) -> None:
    store = SLAStore(tmp_path / ".sdd")
    a = store.add(build_contract(subject_type="schedule", subject_id="sched_a", fire_frequency_s=60))
    store.add(build_contract(subject_type="schedule", subject_id="sched_b", fire_frequency_s=60))
    hits = store.for_subject("schedule", "sched_a")
    assert [c.id for c in hits] == [a.id]


def test_remove(tmp_path: Path) -> None:
    store = SLAStore(tmp_path / ".sdd")
    contract = store.add(build_contract(subject_type="schedule", subject_id="s", fire_frequency_s=60))
    assert store.remove(contract.id) is True
    assert store.get(contract.id) is None
    assert store.remove(contract.id) is False


def test_contract_with_no_axis_is_rejected() -> None:
    with pytest.raises(SLAContractError):
        build_contract(subject_type="schedule", subject_id="s")


def test_freshness_axis_requires_artifact_path() -> None:
    with pytest.raises(SLAContractError):
        build_contract(subject_type="schedule", subject_id="s", artifact_freshness_s=3600)


def test_unknown_subject_type_is_rejected() -> None:
    with pytest.raises(SLAContractError):
        build_contract(subject_type="galaxy", subject_id="s", fire_frequency_s=60)


class TestContractIdPathContainment:
    """Contract ids reach the store from operator input and from request paths,
    and both ``get`` and ``remove`` turn one into a filesystem operation."""

    @pytest.mark.parametrize(
        "contract_id",
        [
            "../../../../etc/passwd",
            "../outside",
            "..",
            ".",
            "/absolute/path",
            "sla_deadbeefdead/../../escape",
            "sla_NOTHEX123456",
            "sla_deadbeef",
            "sla_deadbeefdeadbeef",
            "contract\x00null",
            "sla_deadbeefdead\r\ninjected",
            # Python's `$` also matches immediately before a trailing newline,
            # so this shape passed the id check until the anchor became `\Z`.
            "sla_deadbeefdead\n",
            "sla_aaaaaaaaaaaa\n",
            "",
        ],
    )
    def test_malformed_id_is_refused_before_touching_the_filesystem(self, tmp_path: Path, contract_id: str) -> None:
        store = SLAStore(tmp_path / ".sdd")
        with pytest.raises(SLAContractIdError):
            store.get(contract_id)
        with pytest.raises(SLAContractIdError):
            store.remove(contract_id)

    def test_traversal_id_cannot_read_a_file_outside_the_store(self, tmp_path: Path) -> None:
        secret = tmp_path / "outside.json"
        secret.write_text('{"subject_type": "schedule"}', encoding="utf-8")
        store = SLAStore(tmp_path / ".sdd")
        with pytest.raises(SLAContractIdError):
            store.get("../../outside")
        assert secret.exists()

    def test_traversal_id_cannot_unlink_a_file_outside_the_store(self, tmp_path: Path) -> None:
        """``remove`` unlinks, so an unchecked id is arbitrary file deletion."""
        victim = tmp_path / "victim.json"
        victim.write_text("keep me", encoding="utf-8")
        store = SLAStore(tmp_path / ".sdd")
        with pytest.raises(SLAContractIdError):
            store.remove("../../victim")
        assert victim.read_text(encoding="utf-8") == "keep me"

    def test_refusal_is_a_contract_error_subclass(self, tmp_path: Path) -> None:
        store = SLAStore(tmp_path / ".sdd")
        with pytest.raises(SLAContractError):
            store.get("../escape")

    def test_validation_performs_no_filesystem_access(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Regression guard against reintroducing ``Path.resolve()``.

        This is NOT evidence that containment works - the sibling tests in
        this class carry that, and they fail when `_path_for` is reverted.
        This one guards the narrower property that the containment decision is
        reached without touching disk, which an earlier revision of this fix
        got wrong: it called `.resolve()`, so the untrusted id reached a
        symlink-walking call before anything had validated where it pointed.

        Asserted by making any filesystem access explode rather than by
        planting a symlink and hoping - a symlink test passes trivially on any
        implementation that happens not to resolve.
        """

        def _boom(*_args: object, **_kwargs: object) -> Path:
            raise AssertionError("_path_for must decide containment without touching the filesystem")

        monkeypatch.setattr(Path, "resolve", _boom)
        monkeypatch.setattr(os.path, "realpath", _boom)

        store = SLAStore(tmp_path / ".sdd")
        assert store._path_for("sla_aaaaaaaaaaaa").name == "sla_aaaaaaaaaaaa.json"
        with pytest.raises(SLAContractIdError):
            store._path_for("../../escape")

    def test_derived_ids_still_round_trip(self, tmp_path: Path) -> None:
        store = SLAStore(tmp_path / ".sdd")
        stored = store.add(build_contract(subject_type="schedule", subject_id="s", fire_frequency_s=60))
        assert store.get(stored.id) is not None
        assert store.remove(stored.id) is True


class TestSubjectIdAnchor:
    """`_ID_RE` guards the subject id, which reaches a log record.

    This one is pinned at the constant, not through the public API:
    `build_contract` calls `subject_id.strip()` before matching, and `$`
    differs from `\\Z` only for a string ending in exactly one newline, which
    strip has already removed. The anchor is therefore unreachable defence in
    depth - it matters only if that strip is ever removed - so the test pins
    the property directly rather than pretending to exercise it end to end.
    """

    def test_regex_rejects_a_trailing_newline(self) -> None:
        from bernstein.core.planning.sla_store import _ID_RE

        assert _ID_RE.match("sched_x") is not None
        assert _ID_RE.match("sched_x\n") is None

    def test_strip_is_what_makes_it_unreachable_today(self) -> None:
        """Documents the precondition, so a future change that removes the
        strip has a failing test pointing at the consequence."""
        contract = build_contract(subject_type="schedule", subject_id="sched_x\n", fire_frequency_s=60)
        assert contract.subject_id == "sched_x"


class TestLogInjection:
    def test_crlf_in_a_store_path_cannot_forge_a_log_record(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A value carrying CR/LF must not open a second log line.

        ``_load_contract`` logs the path it failed on. A forged line is how an
        attacker plants a fake record an operator or log shipper then reads as
        real, so the value has to stay on one line.
        """
        from bernstein.core.planning.sla_store import _load_contract

        forged = "WARNING  bernstein: Registered SLA contract sla_000000000000"
        path = tmp_path / f"evil\r\n{forged}.json"
        with caplog.at_level(logging.WARNING, logger="bernstein.core.planning.sla_store"):
            assert _load_contract(path) is None

        assert caplog.records, "the failed load must still be logged"
        for record in caplog.records:
            rendered = record.getMessage()
            # The attacker's text is still present -- it cannot be stripped, only
            # neutralised. What must not survive is the line break that would
            # make it a separate record: the whole thing stays on one line, with
            # the CR/LF rendered as visible escapes.
            assert rendered.splitlines() == [rendered]
            assert "\n" not in rendered
            assert "\r" not in rendered
            assert "\\r\\n" in rendered
            assert not rendered.startswith(forged)

    def test_a_malformed_contract_body_is_logged_on_one_line(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The second log sink: valid JSON whose body fails validation.

        Reached through `list()`, so the path is a real store path, but the
        exception text is derived from stored content and gets the same
        single-line treatment as the load failure.
        """
        store = SLAStore(tmp_path / ".sdd")
        (store.directory / "sla_aaaaaaaaaaaa.json").write_text(
            '{"subject_type": "galaxy", "subject_id": "s", "fire_frequency_s": 60}', encoding="utf-8"
        )
        with caplog.at_level(logging.WARNING, logger="bernstein.core.planning.sla_store"):
            assert store.list() == []
        assert caplog.records, "a malformed contract body must be reported"
        rendered = caplog.records[0].getMessage()
        assert "Malformed SLA contract" in rendered
        assert rendered.splitlines() == [rendered]

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("plain", "plain"),
            ("a\nb", "a\\nb"),
            ("a\r\nb", "a\\r\\nb"),
            ("a\tb", "a\\x09b"),
            ("a\x1b[31mb", "a\\x1b[31mb"),
            ("line\u2028sep", "line\\x2028sep"),
        ],
    )
    def test_single_line_escapes_every_control_character(self, raw: str, expected: str) -> None:
        from bernstein.core.planning.sla_store import _single_line

        assert _single_line(raw) == expected

    def test_single_line_caps_length(self) -> None:
        from bernstein.core.planning.sla_store import _single_line

        out = _single_line("x" * 5000)
        assert out.endswith("...(truncated)")
        assert len(out) < 300
