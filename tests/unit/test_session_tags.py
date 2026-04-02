"""Tests for session_tags — session tagging system."""

from __future__ import annotations

from pathlib import Path

import pytest

from bernstein.session_tags import (
    SessionTags,
    _normalise,
    add_tag,
    get_session_tags,
    list_session_tags,
    remove_tag,
)

# --- Fixtures ---


@pytest.fixture()
def fresh_tags(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reset the global session tag container before each test."""
    from bernstein import session_tags as st

    st._session_tags = SessionTags()


# --- TestNormalise ---


class TestNormalise:
    def test_lowercases(self) -> None:
        assert _normalise("MyTag") == "mytag"

    def test_spaces_to_hyphens(self) -> None:
        assert _normalise("my tag") == "my-tag"

    def test_underscores_to_hyphens(self) -> None:
        assert _normalise("__my_tag__") == "my-tag"

    def test_strips_hyphens(self) -> None:
        assert _normalise("---tag---") == "tag"

    def test_caps_at_40(self) -> None:
        assert len(_normalise("x" * 100)) == 40

    def test_empty(self) -> None:
        assert _normalise("   ") == ""
        assert _normalise("---") == ""


# --- TestSessionTags ---


class TestSessionTags:
    def test_add_returns_normalised(self) -> None:
        st = SessionTags()
        st.add("My Tag")
        assert st.has("my-tag")

    def test_remove_existing(self) -> None:
        st = SessionTags()
        st.add("alpha")
        removed = st.remove("alpha")
        assert removed is True
        assert not st.has("alpha")

    def test_remove_nonexistent(self) -> None:
        st = SessionTags()
        removed = st.remove("ghost")
        assert removed is False

    def test_list_sorted(self) -> None:
        st = SessionTags()
        st.add("zebra")
        st.add("alpha")
        st.add("middle")
        assert st.list_tags() == ["alpha", "middle", "zebra"]

    def test_add_empty_is_noop(self) -> None:
        st = SessionTags()
        st.add("   ")
        st.add("---")
        assert st.list_tags() == []

    def test_save_and_load(self, tmp_path: Path) -> None:
        st = SessionTags()
        st.add("save-test")
        st.add("another-tag")
        path = st.save(tmp_path)
        assert path.exists()
        loaded = SessionTags.load(tmp_path)
        assert loaded.list_tags() == ["another-tag", "save-test"]

    def test_load_missing_is_empty(self, tmp_path: Path) -> None:
        loaded = SessionTags.load(tmp_path)
        assert loaded.list_tags() == []

    def test_load_corrupt_json(self, tmp_path: Path) -> None:
        tag_file = tmp_path / ".sdd" / "runtime" / "session_tags.json"
        tag_file.parent.mkdir(parents=True)
        tag_file.write_text("not json {{{", encoding="utf-8")
        loaded = SessionTags.load(tmp_path)
        assert loaded.list_tags() == []

    def test_to_dict(self) -> None:
        st = SessionTags()
        st.add("x")
        d = st.to_dict()
        assert d == {"tags": ["x"]}


# --- TestModuleLevelAPI ---


class TestModuleLevelAPI:
    def test_add_and_list(self, fresh_tags: None) -> None:
        add_tag("global-test")
        assert "global-test" in list_session_tags()

    def test_get_returns_container(self, fresh_tags: None) -> None:
        assert get_session_tags() is not None

    def test_remove_module_level(self, fresh_tags: None) -> None:
        add_tag("remove-me")
        result = remove_tag("remove-me")
        assert result is True  # or returns truthy (removed)
        assert "remove-me" not in list_session_tags()
