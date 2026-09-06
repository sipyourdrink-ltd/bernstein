"""Shared support for exact-set orphan ratchets."""

from __future__ import annotations

import ast
import io
import json
import os
import re
import subprocess
import tarfile
from collections.abc import Set
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import cast

_SHA_RE = re.compile(r"[0-9a-f]{40}")


@dataclass(frozen=True)
class PythonSourceTree:
    """Python sources from either the checkout or one immutable Git tree."""

    repo_root: Path
    source_root: Path
    ref: str | None = None

    @cached_property
    def sources(self) -> dict[Path, str]:
        """Return absolute source paths and text without changing the checkout."""
        if self.ref is None:
            sources: dict[Path, str] = {}
            for path in self.source_root.rglob("*.py"):
                try:
                    sources[path] = path.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    continue
            return sources

        source_rel = self.source_root.relative_to(self.repo_root).as_posix()
        archived = subprocess.run(
            ["git", "archive", "--format=tar", self.ref, source_rel],
            cwd=self.repo_root,
            check=True,
            capture_output=True,
        ).stdout
        sources = {}
        with tarfile.open(fileobj=io.BytesIO(archived), mode="r:") as archive:
            for member in archive.getmembers():
                if not member.isfile() or not member.name.endswith(".py"):
                    continue
                extracted = archive.extractfile(member)
                if extracted is None:
                    continue
                try:
                    text = extracted.read().decode("utf-8")
                except UnicodeDecodeError:
                    continue
                sources[self.repo_root / member.name] = text
        return sources

    def import_index(
        self,
        excluded: Set[Path],
    ) -> tuple[dict[str, list[Path]], dict[tuple[str, str], list[Path]]]:
        """Build the dotted and from-package import indexes in one AST pass."""
        dotted: dict[str, list[Path]] = {}
        from_package: dict[tuple[str, str], list[Path]] = {}
        for path, text in self.sources.items():
            if path in excluded:
                continue
            try:
                tree = ast.parse(text)
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    source = node.module or ""
                    dotted.setdefault(source, []).append(path)
                    for alias in node.names:
                        from_package.setdefault((source, alias.name), []).append(path)
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        dotted.setdefault(alias.name, []).append(path)
        return dotted, from_package

    @cached_property
    def redirects(self) -> dict[str, str]:
        """Read the redirect table from this tree rather than another revision."""
        path = self.repo_root / "src" / "bernstein" / "core" / "__init__.py"
        module = ast.parse(self.sources[path])
        for node in module.body:
            if (
                isinstance(node, ast.AnnAssign)
                and isinstance(node.target, ast.Name)
                and node.target.id == "_REDIRECT_MAP"
                and node.value is not None
            ):
                value = ast.literal_eval(node.value)
                if not isinstance(value, dict):
                    continue
                entries = cast("dict[object, object]", value)
                if all(isinstance(key, str) and isinstance(target, str) for key, target in entries.items()):
                    return cast("dict[str, str]", entries)
        raise AssertionError(f"could not read _REDIRECT_MAP from {path}")


def pull_request_head_sha(repo_root: Path) -> str | None:
    """Return the PR head SHA when this checkout is its synthetic merge."""
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if not event_path:
        return None
    try:
        event = json.loads(Path(event_path).read_text(encoding="utf-8"))
        head_sha = event["pull_request"]["head"]["sha"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError):
        return None
    if not isinstance(head_sha, str) or _SHA_RE.fullmatch(head_sha) is None:
        return None

    current = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if current == head_sha:
        return None

    available = subprocess.run(
        ["git", "cat-file", "-e", f"{head_sha}^{{commit}}"],
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
    )
    if available.returncode != 0:
        raise AssertionError(
            f"cannot inspect pull request head {head_sha}; fetch the PR head before running orphan ratchets"
        )
    return head_sha


def assert_orphans_match(
    *,
    known: Set[str],
    current: Set[str],
    branch_current: Set[str] | None,
    subject: str,
    wire_or_delete: str,
) -> None:
    """Enforce an orphan ratchet without blaming base-branch drift on a PR."""
    if branch_current is not None and branch_current == known and current != known:
        appeared = sorted(current - known)
        removed = sorted(known - current)
        directions: list[str] = []
        if appeared:
            directions.append(f"became caller-less on the base branch: {appeared}")
        if removed:
            directions.append(f"gained a caller or was deleted on the base branch: {removed}")
        raise AssertionError(
            "KNOWN_ORPHANS baseline is stale; the branch is not at fault. " + "; ".join(directions) + "."
        )

    comparison = current if branch_current is None else branch_current
    appeared = sorted(comparison - known)
    if appeared:
        prefix = "new caller-less" if branch_current is None else "branch introduced new caller-less"
        raise AssertionError(f"{prefix} {subject}: {appeared}. {wire_or_delete}")

    removed = sorted(known - comparison)
    if removed:
        raise AssertionError(
            f"{removed} now has a caller or is gone from the tree; strike it from "
            "KNOWN_ORPHANS so the list keeps shrinking."
        )

    if current != known:
        appeared = sorted(current - known)
        removed = sorted(known - current)
        raise AssertionError(
            "the merge result differs from the branch without a clean branch baseline; "
            f"became caller-less after merging: {appeared}; gained a caller or was deleted after merging: {removed}."
        )
