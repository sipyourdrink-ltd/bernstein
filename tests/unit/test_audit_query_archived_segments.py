"""``query`` can read the same segments ``verify`` does (#1835 follow-up).

``verify`` has replayed archived ``*.jsonl.gz`` segments since #1835, but
``query`` reads only live ``*.jsonl``. Any caller reasoning about *linkage*
between events therefore disagrees with the verifier once retention archives
an early segment: the chain is intact, yet the predecessor an event names is
simply not in the window the caller read.

The recipe definition lineage is one such caller, and the visible symptom is
``recipes history --verify`` reporting a broken link for a recipe that is
entirely healthy.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from bernstein.core.security.audit import AuditLog, RetentionPolicy
from bernstein.core.security.audit_chain import AuditChainStore
from bernstein.core.workflows.recipe_registry import RecipePins, RecipeRegistry
from bernstein.core.workflows.recipe_spec import load_recipe_spec_from_text

if TYPE_CHECKING:
    from pathlib import Path

_KEY = b"0" * 32
_V1 = 'name: nightly\ndescription: d\nversion: "1.0.0"\nnodes:\n  - id: n\n    command: "echo a"\n'
_V2 = 'name: nightly\ndescription: d\nversion: "2.0.0"\nnodes:\n  - id: n\n    command: "echo b"\n'


def _registry(sdd: Path) -> RecipeRegistry:
    sdd.mkdir(parents=True, exist_ok=True)
    return RecipeRegistry(
        sdd,
        chain=AuditChainStore(sdd / "audit", key=_KEY),
        hmac_key=_KEY,
        lineage_key=_KEY,
    )


def _register_then_archive_genesis(sdd: Path) -> None:
    """Register v1, age its segment, register v2, run ordinary retention."""
    audit = sdd / "audit"
    _registry(sdd).register(spec=load_recipe_spec_from_text(_V1), pins=RecipePins(git_commit="c1"))
    sorted(audit.glob("*.jsonl"))[0].rename(audit / "2020-01-01.jsonl")
    _registry(sdd).register(spec=load_recipe_spec_from_text(_V2), pins=RecipePins(git_commit="c2"))
    AuditLog(audit_dir=audit, key=_KEY).archive(RetentionPolicy())


class TestQueryReadsArchivedSegments:
    def test_archived_events_are_invisible_by_default(self, tmp_path: Path) -> None:
        """The default window is unchanged: live segments only."""
        sdd = tmp_path / ".sdd"
        _register_then_archive_genesis(sdd)
        log = AuditLog(audit_dir=sdd / "audit", key=_KEY)
        assert len(log.query(event_type="recipe.register")) == 0

    def test_include_archived_sees_them(self, tmp_path: Path) -> None:
        sdd = tmp_path / ".sdd"
        _register_then_archive_genesis(sdd)
        log = AuditLog(audit_dir=sdd / "audit", key=_KEY)
        assert len(log.query(event_type="recipe.register", include_archived=True)) == 1

    def test_archived_events_come_before_live_ones(self, tmp_path: Path) -> None:
        """Chronological order across the retention boundary, as verify walks it."""
        sdd = tmp_path / ".sdd"
        _register_then_archive_genesis(sdd)
        log = AuditLog(audit_dir=sdd / "audit", key=_KEY)
        events = log.query(include_archived=True)
        assert [e.event_type for e in events] == ["recipe.register", "recipe.supersede"]


class TestLineageSurvivesRetention:
    def test_an_archived_genesis_no_longer_reads_as_a_broken_link(self, tmp_path: Path) -> None:
        """The reported defect: a healthy recipe accused of a broken lineage."""
        sdd = tmp_path / ".sdd"
        _register_then_archive_genesis(sdd)
        registry = _registry(sdd)

        chain_ok, _errors = registry._get_chain().verify()
        assert chain_ok, "precondition: the chain is intact, only a segment was archived"

        ok, errors = registry.verify_history("nightly")
        assert ok is True, f"an archived-but-intact lineage must verify: {errors}"
        assert len(registry.history("nightly")) == 2, "both receipts re-walked, including the archived one"
        assert registry.live_hash("nightly")

    def test_a_recipe_with_no_archived_history_is_unaffected(self, tmp_path: Path) -> None:
        sdd = tmp_path / ".sdd"
        registry = _registry(sdd)
        registry.register(spec=load_recipe_spec_from_text(_V1), pins=RecipePins(git_commit="c1"))
        ok, errors = registry.verify_history("nightly")
        assert ok is True, errors


class TestInvalidUtf8IsSkippedNeverLaundered:
    """Invalid bytes are evidence, and ``verify`` hashes the raw bytes.

    Substituting replacement characters would make ``query`` return a
    clean-looking record that does not match what is on disk - the projection
    and the verifier would then disagree about what the log says. So an
    undecodable line is dropped whole, never returned in smoothed form, and
    ``verify`` keeps naming it. ``query`` itself stays total: torn bytes are
    permanent (the log is append-only and a tear seal never truncates), so a
    raising read path would turn one acknowledged crash into a forever-broken
    query surface for every consumer of the chain.
    """

    @staticmethod
    def _corrupt_a_string_value(audit: Path) -> None:
        segment = sorted(audit.glob("*.jsonl"))[0]
        raw = segment.read_bytes()
        corrupted = raw.replace(b'"nightly"', b'"night\xffy"')
        assert corrupted != raw, "fixture did not corrupt anything"
        segment.write_bytes(corrupted)

    def test_live_segment_with_invalid_utf8_is_skipped_and_stays_loud(self, tmp_path: Path) -> None:
        sdd = tmp_path / ".sdd"
        sdd.mkdir(parents=True)
        _registry(sdd).register(spec=load_recipe_spec_from_text(_V1), pins=RecipePins(git_commit="c1"))
        self._corrupt_a_string_value(sdd / "audit")

        log = AuditLog(audit_dir=sdd / "audit", key=_KEY)
        events = log.query(event_type="recipe.register")
        # The damaged record is omitted, not returned with replacement
        # characters; nothing query yields differs from the on-disk bytes.
        assert events == []
        ok, errors = log.verify()
        assert ok is False
        assert any("undecodable" in error for error in errors)

    def test_archived_segment_with_invalid_utf8_is_skipped_and_stays_loud(self, tmp_path: Path) -> None:
        sdd = tmp_path / ".sdd"
        sdd.mkdir(parents=True)
        audit = sdd / "audit"
        _registry(sdd).register(spec=load_recipe_spec_from_text(_V1), pins=RecipePins(git_commit="c1"))
        self._corrupt_a_string_value(audit)
        sorted(audit.glob("*.jsonl"))[0].rename(audit / "2020-01-01.jsonl")
        AuditLog(audit_dir=audit, key=_KEY).archive(RetentionPolicy())

        log = AuditLog(audit_dir=audit, key=_KEY)
        # Live-only read sees nothing and must not raise...
        assert log.query(event_type="recipe.register") == []
        # ...and the archived read applies the same skip-not-launder rule.
        assert log.query(event_type="recipe.register", include_archived=True) == []
        ok, errors = log.verify()
        assert ok is False
        assert any("undecodable" in error for error in errors)
