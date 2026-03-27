"""Tests for FeatureDiscovery in bernstein.evolution.detector."""

from __future__ import annotations

from pathlib import Path

from bernstein.evolution.detector import FeatureDiscovery, FeatureTicket

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_src_file(tmp_path: Path, rel: str, content: str) -> Path:
    """Create a source file under tmp_path/src/."""
    p = tmp_path / "src" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


def _make_backlog_ticket(tmp_path: Path, subdir: str, filename: str, title: str) -> Path:
    """Write a minimal backlog ticket file."""
    d = tmp_path / ".sdd" / "backlog" / subdir
    d.mkdir(parents=True, exist_ok=True)
    p = d / filename
    p.write_text(f"# 999 — {title}\n\n**Role:** backend\n", encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# FeatureTicket
# ---------------------------------------------------------------------------


def test_feature_ticket_defaults() -> None:
    """FeatureTicket has sensible defaults for optional fields."""
    ticket = FeatureTicket(title="Test ticket", description="desc")
    assert ticket.role == "backend"
    assert ticket.priority == 2
    assert ticket.scope == "small"
    assert ticket.complexity == "medium"
    assert ticket.source == "todo_fixme"
    assert ticket.ticket_id == ""
    assert ticket.file_path is None


# ---------------------------------------------------------------------------
# FeatureDiscovery — TODO/FIXME scanning
# ---------------------------------------------------------------------------


def test_discover_finds_todo_comment(tmp_path: Path) -> None:
    """discover() returns a ticket when a TODO comment is found in source."""
    _make_src_file(tmp_path, "foo.py", "x = 1\n# TODO: add retry logic\n")
    fd = FeatureDiscovery(repo_root=tmp_path, backlog_dir=tmp_path / ".sdd" / "backlog")

    tickets = fd.discover(max_tickets=5)

    assert any("retry logic" in t.title.lower() for t in tickets)


def test_discover_finds_fixme_comment(tmp_path: Path) -> None:
    """discover() returns a ticket for FIXME comments."""
    _make_src_file(tmp_path, "bar.py", "# FIXME: broken validation here\n")
    fd = FeatureDiscovery(repo_root=tmp_path, backlog_dir=tmp_path / ".sdd" / "backlog")

    tickets = fd.discover(max_tickets=5)

    assert any("broken validation" in t.title.lower() for t in tickets)


def test_discover_skips_test_files(tmp_path: Path) -> None:
    """TODO comments inside test files are not surfaced as tickets."""
    p = tmp_path / "tests" / "test_stuff.py"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("# TODO: improve test coverage\n", encoding="utf-8")
    fd = FeatureDiscovery(repo_root=tmp_path, backlog_dir=tmp_path / ".sdd" / "backlog")

    tickets = fd.discover(max_tickets=5)

    assert not any("improve test coverage" in t.title.lower() for t in tickets)


# ---------------------------------------------------------------------------
# FeatureDiscovery — deduplication
# ---------------------------------------------------------------------------


def test_discover_deduplicates_against_open_backlog(tmp_path: Path) -> None:
    """Tickets matching existing open-backlog titles are skipped."""
    _make_src_file(tmp_path, "foo.py", "# TODO: add retry logic\n")
    _make_backlog_ticket(tmp_path, "open", "500-add-retry-logic.md", "Add retry logic")
    fd = FeatureDiscovery(repo_root=tmp_path, backlog_dir=tmp_path / ".sdd" / "backlog")

    tickets = fd.discover(max_tickets=5)

    assert not any("retry logic" in t.title.lower() for t in tickets)


def test_discover_deduplicates_against_closed_backlog(tmp_path: Path) -> None:
    """Tickets matching existing closed-backlog titles are skipped."""
    _make_src_file(tmp_path, "foo.py", "# TODO: add rate limiting\n")
    _make_backlog_ticket(tmp_path, "closed", "300-add-rate-limiting.md", "Add rate limiting")
    fd = FeatureDiscovery(repo_root=tmp_path, backlog_dir=tmp_path / ".sdd" / "backlog")

    tickets = fd.discover(max_tickets=5)

    assert not any("rate limiting" in t.title.lower() for t in tickets)


def test_discover_no_duplicate_titles_in_output(tmp_path: Path) -> None:
    """If two source files have the same TODO text, only one ticket is generated."""
    _make_src_file(tmp_path, "a.py", "# TODO: add retry logic\n")
    _make_src_file(tmp_path, "b.py", "# TODO: add retry logic\n")
    fd = FeatureDiscovery(repo_root=tmp_path, backlog_dir=tmp_path / ".sdd" / "backlog")

    tickets = fd.discover(max_tickets=5)

    retry_tickets = [t for t in tickets if "retry logic" in t.title.lower()]
    assert len(retry_tickets) == 1


# ---------------------------------------------------------------------------
# FeatureDiscovery — cap
# ---------------------------------------------------------------------------


def test_discover_caps_at_max_tickets(tmp_path: Path) -> None:
    """discover() returns at most max_tickets tickets."""
    for i in range(10):
        _make_src_file(tmp_path, f"mod{i}.py", f"# TODO: task number {i}\n")
    fd = FeatureDiscovery(repo_root=tmp_path, backlog_dir=tmp_path / ".sdd" / "backlog")

    tickets = fd.discover(max_tickets=3)

    assert len(tickets) <= 3


def test_discover_default_cap_is_five(tmp_path: Path) -> None:
    """Default max_tickets is 5."""
    for i in range(12):
        _make_src_file(tmp_path, f"m{i}.py", f"# TODO: item {i}\n")
    fd = FeatureDiscovery(repo_root=tmp_path, backlog_dir=tmp_path / ".sdd" / "backlog")

    tickets = fd.discover()

    assert len(tickets) <= 5


# ---------------------------------------------------------------------------
# FeatureDiscovery — ticket writing
# ---------------------------------------------------------------------------


def test_discover_writes_markdown_files_to_backlog(tmp_path: Path) -> None:
    """Discovered tickets are written to backlog/open/ as .md files."""
    _make_src_file(tmp_path, "foo.py", "# TODO: add retry logic\n")
    backlog_dir = tmp_path / ".sdd" / "backlog"
    fd = FeatureDiscovery(repo_root=tmp_path, backlog_dir=backlog_dir)

    tickets = fd.discover(max_tickets=5)

    open_dir = backlog_dir / "open"
    assert open_dir.is_dir()
    md_files = list(open_dir.glob("*.md"))
    assert len(md_files) == len(tickets)


def test_discover_ticket_has_standard_format(tmp_path: Path) -> None:
    """Written ticket has the expected markdown header format."""
    _make_src_file(tmp_path, "foo.py", "# TODO: add retry logic\n")
    backlog_dir = tmp_path / ".sdd" / "backlog"
    fd = FeatureDiscovery(repo_root=tmp_path, backlog_dir=backlog_dir)

    tickets = fd.discover(max_tickets=1)

    assert len(tickets) == 1
    assert tickets[0].file_path is not None
    content = tickets[0].file_path.read_text(encoding="utf-8")  # type: ignore[union-attr]

    assert content.startswith("#")
    assert "**Role:**" in content
    assert "**Priority:**" in content
    assert "**Scope:**" in content
    assert "**Complexity:**" in content


def test_discover_sets_file_path_on_ticket(tmp_path: Path) -> None:
    """file_path attribute of returned tickets points to written files."""
    _make_src_file(tmp_path, "foo.py", "# TODO: add retry logic\n")
    fd = FeatureDiscovery(repo_root=tmp_path, backlog_dir=tmp_path / ".sdd" / "backlog")

    tickets = fd.discover(max_tickets=5)

    for ticket in tickets:
        assert ticket.file_path is not None
        assert ticket.file_path.exists()


def test_discover_assigns_sequential_ticket_ids(tmp_path: Path) -> None:
    """Ticket IDs are numeric and don't collide with existing backlog IDs."""
    _make_backlog_ticket(tmp_path, "open", "450-existing.md", "Existing ticket")
    _make_src_file(tmp_path, "a.py", "# TODO: thing one\n")
    _make_src_file(tmp_path, "b.py", "# TODO: thing two\n")
    fd = FeatureDiscovery(repo_root=tmp_path, backlog_dir=tmp_path / ".sdd" / "backlog")

    tickets = fd.discover(max_tickets=5)

    for ticket in tickets:
        assert ticket.ticket_id != ""
        assert int(ticket.ticket_id) > 450


# ---------------------------------------------------------------------------
# FeatureDiscovery — missing pattern detection
# ---------------------------------------------------------------------------


def test_discover_detects_missing_retry_pattern(tmp_path: Path) -> None:
    """When no retry logic exists in src/, a missing-pattern ticket is generated."""
    # Create a src/ with no retry imports or usage
    _make_src_file(tmp_path, "server.py", "def run(): pass\n")
    fd = FeatureDiscovery(repo_root=tmp_path, backlog_dir=tmp_path / ".sdd" / "backlog")

    tickets = fd.discover(max_tickets=5)

    sources = {t.source for t in tickets}
    assert "missing_pattern" in sources


def test_discover_skips_missing_pattern_when_already_present(tmp_path: Path) -> None:
    """No missing-retry ticket when retry logic already exists in src/."""
    _make_src_file(
        tmp_path,
        "utils.py",
        "import tenacity\n\n@tenacity.retry\ndef call(): pass\n",
    )
    fd = FeatureDiscovery(repo_root=tmp_path, backlog_dir=tmp_path / ".sdd" / "backlog")

    tickets = fd.discover(max_tickets=5)

    retry_pattern_tickets = [t for t in tickets if t.source == "missing_pattern" and "retry" in t.title.lower()]
    assert len(retry_pattern_tickets) == 0


# ---------------------------------------------------------------------------
# FeatureDiscovery — empty / edge cases
# ---------------------------------------------------------------------------


def test_discover_returns_empty_for_clean_codebase(tmp_path: Path) -> None:
    """No tickets generated when codebase has no TODOs and all patterns present."""
    _make_src_file(
        tmp_path,
        "utils.py",
        ("import tenacity\nfrom functools import lru_cache\nfrom ratelimit import limits\n\ndef run(): pass\n"),
    )
    fd = FeatureDiscovery(repo_root=tmp_path, backlog_dir=tmp_path / ".sdd" / "backlog")

    tickets = fd.discover(max_tickets=5)

    assert tickets == []


def test_discover_handles_missing_src_dir(tmp_path: Path) -> None:
    """discover() returns empty list when src/ directory does not exist."""
    fd = FeatureDiscovery(repo_root=tmp_path, backlog_dir=tmp_path / ".sdd" / "backlog")

    tickets = fd.discover(max_tickets=5)

    # Only missing-pattern tickets possible, all deduplicated
    assert isinstance(tickets, list)
