"""End-to-end tests for ``bernstein readme-l10n`` (issue #3425).

Follows the ``test_agents_md_cmd.py`` pattern: ``CliRunner.invoke``
against ``tmp_path`` fixture repos, then assert exit codes + output.

The fixture repo builds its binding hashes from the live English source
via ``section_hash``, so the "gate catches drift" tests edit the English
fixture and assert the verify run names the stale section - exactly the
RED-GREEN shape the issue asks to demonstrate.
"""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner, Result

from bernstein.cli.commands.readme_l10n_cmd import readme_l10n_cmd
from bernstein.core.knowledge.readme_l10n import (
    HEADER_SECTION,
    Section,
    section_hash,
    split_sections,
)

EN_README = """# Demo

Intro line.

### install in 30 seconds

```bash
pipx install demo
demo run
```

pip and uv are covered in the install guide.

### how it works

Each goal moves through four stages.

---

Footer line.
"""


def _en_sections() -> dict[str, Section]:
    return {s.heading: s for s in split_sections(EN_README)}


def _zh_readme(en_sections: dict[str, Section]) -> str:
    h_install = section_hash(en_sections["install in 30 seconds"])
    h_how = section_hash(en_sections["how it works"])
    return f"""# Demo

Intro line.

### 30 秒安装
<!-- l10n: en="install in 30 seconds" hash="{h_install}" -->

```bash
pipx install demo
demo run
```

pip 和 uv 涵盖在安装指南中。

### 工作原理
<!-- l10n: en="how it works" hash="{h_how}" -->

每个目标经历四个阶段。

---

Footer line.
"""


def _write_fixture(tmp_path: Path, *, zh: str | None = None) -> Path:
    (tmp_path / "README.md").write_text(EN_README, encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = 'demo'\nversion = '0'\n\n[tool.bernstein.readme-l10n]\nlanguages = ['zh-Hans']\n",
        encoding="utf-8",
    )
    if zh is not None:
        (tmp_path / "README.zh-Hans.md").write_text(zh, encoding="utf-8")
    return tmp_path


def _run(*args: str) -> Result:
    runner = CliRunner()
    return runner.invoke(readme_l10n_cmd, list(args))


class TestVerify:
    def test_clean_fixture_passes(self, tmp_path: Path) -> None:
        repo = _write_fixture(tmp_path, zh=_zh_readme(_en_sections()))
        result = _run("verify", "--workdir", str(repo))
        assert result.exit_code == 0, result.output
        assert "OK" in result.output

    def test_stale_section_fails_naming_the_section(self, tmp_path: Path) -> None:
        repo = _write_fixture(tmp_path, zh=_zh_readme(_en_sections()))
        # Edit the English source: install guide prose changes.
        changed = EN_README.replace(
            "pip and uv are covered in the install guide.",
            "pip, uv, brew and dnf are covered in the install guide.",
        )
        (repo / "README.md").write_text(changed, encoding="utf-8")
        result = _run("verify", "--workdir", str(repo))
        assert result.exit_code == 1, result.output
        # The failure must name the language and the exact section.
        assert "README.zh-Hans.md" in result.output
        assert "install in 30 seconds" in result.output

    def test_translated_code_block_fails(self, tmp_path: Path) -> None:
        zh = _zh_readme(_en_sections()).replace(
            "pipx install demo",
            "pipx 安装 demo",  # a translated command name
        )
        repo = _write_fixture(tmp_path, zh=zh)
        result = _run("verify", "--workdir", str(repo))
        assert result.exit_code == 1, result.output
        assert "code block" in result.output
        assert "install in 30 seconds" in result.output

    def test_removed_code_block_fails(self, tmp_path: Path) -> None:
        zh = _zh_readme(_en_sections()).replace("```bash\npipx install demo\ndemo run\n```\n\n", "")
        repo = _write_fixture(tmp_path, zh=zh)
        result = _run("verify", "--workdir", str(repo))
        assert result.exit_code == 1, result.output
        assert "code block" in result.output

    def test_header_not_verbatim_fails(self, tmp_path: Path) -> None:
        zh = _zh_readme(_en_sections()).replace("# Demo", "# 演示")
        repo = _write_fixture(tmp_path, zh=zh)
        result = _run("verify", "--workdir", str(repo))
        assert result.exit_code == 1, result.output
        assert "(header)" in result.output

    def test_missing_binding_fails(self, tmp_path: Path) -> None:
        zh = _zh_readme(_en_sections()).replace('<!-- l10n: en="how it works"', '<!-- broken: en="how it works"')
        repo = _write_fixture(tmp_path, zh=zh)
        result = _run("verify", "--workdir", str(repo))
        assert result.exit_code == 1, result.output
        assert "how it works" in result.output
        assert "no l10n binding" in result.output

    def test_configured_file_missing_fails(self, tmp_path: Path) -> None:
        repo = _write_fixture(tmp_path)  # no zh file
        result = _run("verify", "--workdir", str(repo))
        assert result.exit_code == 1, result.output
        assert "MISSING" in result.output

    def test_no_languages_configured_skips(self, tmp_path: Path) -> None:
        repo = _write_fixture(tmp_path, zh=_zh_readme(_en_sections()))
        (repo / "pyproject.toml").write_text("[project]\nname = 'demo'\nversion = '0'\n", encoding="utf-8")
        result = _run("verify", "--workdir", str(repo))
        assert result.exit_code == 0, result.output
        assert "SKIP" in result.output

    def test_missing_readme_fails_with_usage_error(self, tmp_path: Path) -> None:
        result = _run("verify", "--workdir", str(tmp_path))
        assert result.exit_code == 2, result.output
        assert "README.md" in result.output


class TestParagraphParity:
    def test_paragraph_added_to_english_fails_even_after_sync(self, tmp_path: Path) -> None:
        """A paragraph added to the English source must not vanish silently.

        ``sync`` rebinds the hash without proving the translation followed,
        so the binding check alone goes green; the paragraph-parity check
        is what turns the missing translated paragraph red.
        """
        repo = _write_fixture(tmp_path, zh=_zh_readme(_en_sections()))
        changed = EN_README.replace(
            "pip and uv are covered in the install guide.",
            "pip and uv are covered in the install guide.\n\n"
            "Hygiene gates: `demo l10n verify` fails a PR whose translations drifted.",
        )
        (repo / "README.md").write_text(changed, encoding="utf-8")
        assert _run("sync", "--workdir", str(repo)).exit_code == 0
        result = _run("verify", "--workdir", str(repo))
        assert result.exit_code == 1, result.output
        assert "paragraph" in result.output
        assert "install in 30 seconds" in result.output

    def test_translated_paragraph_present_passes(self, tmp_path: Path) -> None:
        changed_en = EN_README.replace(
            "pip and uv are covered in the install guide.",
            "pip and uv are covered in the install guide.\n\n"
            "Hygiene gates: `demo l10n verify` fails a PR whose translations drifted.",
        )
        zh = _zh_readme(_en_sections()).replace(
            "pip 和 uv 涵盖在安装指南中。",
            "pip 和 uv 涵盖在安装指南中。\n\n卫生门禁 `demo l10n verify` 会在翻译漂移时让 PR 失败。",
        )
        repo = _write_fixture(tmp_path, zh=zh)
        (repo / "README.md").write_text(changed_en, encoding="utf-8")
        assert _run("sync", "--workdir", str(repo)).exit_code == 0
        result = _run("verify", "--workdir", str(repo))
        assert result.exit_code == 0, result.output

    def test_fence_adjacent_to_prose_is_its_own_block(self) -> None:
        """A fence with no blank line before or after it still counts once.

        Counting it as part of the neighbouring prose run would let a
        translation drop a paragraph next to a code block undetected.
        """
        from bernstein.core.knowledge.readme_l10n import paragraph_count

        assert paragraph_count("prose\n```\ncode\n```") == 2
        assert paragraph_count("```\ncode\n```\nprose") == 2
        assert paragraph_count("before\n```\ncode\n```\nafter") == 3

    def test_reflowed_translation_is_reported(self) -> None:
        """Merging two English paragraphs into one translated block fails.

        Documented contract: parity compares English against translation,
        where a merged pair and a dropped paragraph look identical, so a
        translation preserves the English block structure.
        """
        from bernstein.core.knowledge.readme_l10n import paragraph_count

        assert paragraph_count("A\n\nB\n") == 2
        assert paragraph_count("译A\n译B\n") == 1

    def test_code_block_counts_as_one_block(self) -> None:
        from bernstein.core.knowledge.readme_l10n import paragraph_count

        body = "```bash\nfirst\n\nsecond\n```\n\nprose paragraph\n"
        assert paragraph_count(body) == 2

    def test_binding_comment_is_not_a_block(self) -> None:
        from bernstein.core.knowledge.readme_l10n import paragraph_count

        body = '<!-- l10n: en="x" hash="sha256:00" -->\n\nprose paragraph\n'
        assert paragraph_count(body) == 1


class TestBindingPlacement:
    def test_duplicate_binding_for_one_section_fails(self, tmp_path: Path) -> None:
        """Two headings binding one English section is ambiguous, not a pick-first."""
        en = _en_sections()
        zh = _zh_readme(en).replace(
            "### 工作原理\n",
            f'### 别名\n<!-- l10n: en="install in 30 seconds" hash="{section_hash(en["install in 30 seconds"])}" -->\n\n占位。\n\n### 工作原理\n',
        )
        repo = _write_fixture(tmp_path, zh=zh)
        result = _run("verify", "--workdir", str(repo))
        assert result.exit_code == 1, result.output
        assert "bound by 2 translated headings" in result.output

    def test_binding_under_no_heading_fails(self, tmp_path: Path) -> None:
        """A binding above the first heading pins nothing."""
        en = _en_sections()
        h = section_hash(en["install in 30 seconds"])
        zh = _zh_readme(en).replace(
            "Intro line.\n",
            f'Intro line.\n\n<!-- l10n: en="install in 30 seconds" hash="{h}" -->\n',
        )
        repo = _write_fixture(tmp_path, zh=zh)
        result = _run("verify", "--workdir", str(repo))
        assert result.exit_code == 1, result.output
        assert "outside every" in result.output


class TestSync:
    def test_sync_rebinds_stale_sections(self, tmp_path: Path) -> None:
        repo = _write_fixture(tmp_path, zh=_zh_readme(_en_sections()))
        changed = EN_README.replace(
            "pip and uv are covered in the install guide.",
            "pip, uv, brew and dnf are covered in the install guide.",
        )
        (repo / "README.md").write_text(changed, encoding="utf-8")
        result = _run("sync", "--workdir", str(repo))
        assert result.exit_code == 0, result.output
        # After sync the binding matches the new English content.
        verify = _run("verify", "--workdir", str(repo))
        assert verify.exit_code == 0, verify.output

    def test_sync_reports_missing_binding(self, tmp_path: Path) -> None:
        zh = _zh_readme(_en_sections()).replace('<!-- l10n: en="how it works"', '<!-- broken: en="how it works"')
        repo = _write_fixture(tmp_path, zh=zh)
        result = _run("sync", "--workdir", str(repo))
        assert result.exit_code == 0, result.output
        assert "how it works" in result.output
        assert "no binding" in result.output


class TestCoreSplitting:
    def test_sections_and_footer_split(self) -> None:
        sections = split_sections(EN_README)
        headings = [s.heading for s in sections]
        assert headings == [
            HEADER_SECTION,
            "install in 30 seconds",
            "how it works",
            "(footer)",
        ]
        # Footer starts at the '---' separator, so it does not include
        # the last heading's prose.
        footer = sections[-1].body
        assert "Footer line" in footer
        assert "four stages" not in footer
