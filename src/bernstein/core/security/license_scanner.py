"""License scanner: detect copyleft license obligations in agent diffs.

Scans git diffs for GPL/LGPL/AGPL and other copyleft license indicators
introduced by added lines.  Hard-blocks strong copyleft (GPL/AGPL/OSL);
soft-flags weak copyleft (LGPL/MPL/EUPL).

Design constraints:
- No external API calls — 100% offline, regex + static database.
- Only inspects *added* diff lines; removed lines are ignored.
- One result per diff (aggregates all hits into worst-case severity).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from bernstein.core.security.policy_engine import DecisionType, PermissionDecision

# ---------------------------------------------------------------------------
# Static license database
# ---------------------------------------------------------------------------

# SPDX identifier → (display name, copyleft strength)
# "strong" : GPL/AGPL/OSL — derivative work must be licensed under the same terms
# "weak"   : LGPL/MPL/EUPL/CDDL — obligations exist but are not fully viral
_SPDX_LICENSE_DB: dict[str, tuple[str, str]] = {
    "GPL-2.0": ("GNU General Public License v2", "strong"),
    "GPL-2.0-only": ("GNU General Public License v2.0 only", "strong"),
    "GPL-2.0-or-later": ("GNU General Public License v2.0 or later", "strong"),
    "GPL-3.0": ("GNU General Public License v3", "strong"),
    "GPL-3.0-only": ("GNU General Public License v3.0 only", "strong"),
    "GPL-3.0-or-later": ("GNU General Public License v3.0 or later", "strong"),
    "AGPL-3.0": ("GNU Affero General Public License v3", "strong"),
    "AGPL-3.0-only": ("GNU Affero General Public License v3.0 only", "strong"),
    "AGPL-3.0-or-later": ("GNU Affero General Public License v3.0 or later", "strong"),
    "OSL-3.0": ("Open Software License 3.0", "strong"),
    "LGPL-2.0": ("GNU Library General Public License v2", "weak"),
    "LGPL-2.0-only": ("GNU Library General Public License v2 only", "weak"),
    "LGPL-2.0-or-later": ("GNU Library General Public License v2 or later", "weak"),
    "LGPL-2.1": ("GNU Lesser General Public License v2.1", "weak"),
    "LGPL-2.1-only": ("GNU Lesser General Public License v2.1 only", "weak"),
    "LGPL-2.1-or-later": ("GNU Lesser General Public License v2.1 or later", "weak"),
    "LGPL-3.0": ("GNU Lesser General Public License v3", "weak"),
    "LGPL-3.0-only": ("GNU Lesser General Public License v3.0 only", "weak"),
    "LGPL-3.0-or-later": ("GNU Lesser General Public License v3.0 or later", "weak"),
    "MPL-2.0": ("Mozilla Public License 2.0", "weak"),
    "EUPL-1.1": ("European Union Public License 1.1", "weak"),
    "EUPL-1.2": ("European Union Public License 1.2", "weak"),
    "CDDL-1.0": ("Common Development and Distribution License 1.0", "weak"),
}

# ---------------------------------------------------------------------------
# Detection patterns
# ---------------------------------------------------------------------------

# SPDX-License-Identifier line (most precise, no false positives)
_SPDX_RE = re.compile(
    r"SPDX-License-Identifier:\s*([A-Za-z0-9.\-+]+(?:\s+(?:AND|OR|WITH)\s+[A-Za-z0-9.\-+]+)*)",
    re.IGNORECASE,
)

# Prose patterns for copyleft license boilerplate text.
# Order matters: more-specific patterns (with version) appear before generic ones.
# Each entry: (canonical_label, compiled_pattern, copyleft_strength)
_PROSE_PATTERNS: list[tuple[str, re.Pattern[str], str]] = [
    ("AGPL-3.0", re.compile(r"GNU Affero General Public License", re.IGNORECASE), "strong"),
    ("GPL-3.0", re.compile(r"GNU General Public License[^;]*version\s+3", re.IGNORECASE), "strong"),
    ("GPL-2.0", re.compile(r"GNU General Public License[^;]*version\s+2", re.IGNORECASE), "strong"),
    ("GPL", re.compile(r"GNU General Public License", re.IGNORECASE), "strong"),
    ("LGPL-3.0", re.compile(r"GNU (?:Lesser|Library) General Public License[^;]*version\s+3", re.IGNORECASE), "weak"),
    (
        "LGPL-2.1",
        re.compile(r"GNU (?:Lesser|Library) General Public License[^;]*version\s+2\.1", re.IGNORECASE),
        "weak",
    ),
    ("LGPL-2.0", re.compile(r"GNU (?:Lesser|Library) General Public License[^;]*version\s+2", re.IGNORECASE), "weak"),
    ("LGPL", re.compile(r"GNU (?:Lesser|Library) General Public License", re.IGNORECASE), "weak"),
    ("MPL-2.0", re.compile(r"Mozilla Public License", re.IGNORECASE), "weak"),
    ("EUPL", re.compile(r"European Union Public License", re.IGNORECASE), "weak"),
    ("CDDL", re.compile(r"Common Development and Distribution License", re.IGNORECASE), "weak"),
    ("OSL", re.compile(r"Open Software License", re.IGNORECASE), "strong"),
]

_DIFF_FILE_RE = re.compile(r"^diff --git a/(.+?) b/")
_SPDX_SPLIT_RE = re.compile(r"[ \t]{1,10}(?:AND|OR|WITH)[ \t]{1,10}", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Internal data type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _LicenseHit:
    """A detected copyleft license indicator in a single diff line.

    Attributes:
        license_id: SPDX identifier or prose label (e.g. "GPL-3.0", "LGPL").
        copyleft_strength: "strong" or "weak".
        file_path: File in which the indicator was found.
        line: The raw added diff line content (leading ``+`` stripped).
    """

    license_id: str
    copyleft_strength: str
    file_path: str
    line: str


# ---------------------------------------------------------------------------
# Internal scanner
# ---------------------------------------------------------------------------


def _check_spdx_line(
    content: str,
    current_file: str,
    seen: set[str],
    hits: list[_LicenseHit],
) -> bool:
    """Check an added line for SPDX license identifiers.

    Returns True if the line contained an SPDX identifier (so prose
    scanning can be skipped), False otherwise.
    """
    spdx_match = _SPDX_RE.search(content)
    if not spdx_match:
        return False
    spdx_expr = spdx_match.group(1).strip()
    for component in _SPDX_SPLIT_RE.split(spdx_expr):
        component = component.strip()
        if component in _SPDX_LICENSE_DB:
            _name, strength = _SPDX_LICENSE_DB[component]
            key = f"{current_file}:{component}"
            if key not in seen:
                seen.add(key)
                hits.append(
                    _LicenseHit(
                        license_id=component,
                        copyleft_strength=strength,
                        file_path=current_file,
                        line=content.strip(),
                    )
                )
    return True


def _check_prose_line(
    content: str,
    current_file: str,
    seen: set[str],
    hits: list[_LicenseHit],
) -> None:
    """Check an added line against prose license header patterns."""
    for label, pattern, strength in _PROSE_PATTERNS:
        if pattern.search(content):
            key = f"{current_file}:{label}"
            if key not in seen:
                seen.add(key)
                hits.append(
                    _LicenseHit(
                        license_id=label,
                        copyleft_strength=strength,
                        file_path=current_file,
                        line=content.strip(),
                    )
                )
            break  # first matching prose pattern wins for this line


def _scan_diff_for_licenses(diff: str) -> list[_LicenseHit]:
    """Extract copyleft license indicators from *added* lines in a git diff.

    Removal lines (``-``) are ignored — a project removing a license header
    has no new obligations.  Deduplication prevents one multi-line boilerplate
    block from generating dozens of hits.

    Args:
        diff: Raw ``git diff`` output.

    Returns:
        Deduplicated list of :class:`_LicenseHit` from added lines.
    """
    hits: list[_LicenseHit] = []
    current_file = ""
    seen: set[str] = set()  # dedup key: "{file}:{license_id}"

    for line in diff.splitlines():
        file_match = _DIFF_FILE_RE.match(line)
        if file_match:
            current_file = file_match.group(1)
            continue

        # Only added lines; skip ``+++`` header lines
        if not line.startswith("+") or line.startswith("+++"):
            continue

        content = line[1:]  # strip leading ``+``

        if not _check_spdx_line(content, current_file, seen, hits):
            _check_prose_line(content, current_file, seen, hits)

    return hits


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def check_license_obligations(diff: str) -> list[PermissionDecision]:
    """Scan a diff for copyleft license obligations in added lines.

    Severity rules:

    - **Strong copyleft** (GPL/AGPL/OSL): hard block.
      Code under these licenses infects the entire derivative work.
      Merge must not proceed until the code is removed or replaced.
    - **Weak copyleft** (LGPL/MPL/EUPL/CDDL): soft flag.
      Obligations exist (attribution, source distribution)
      but the codebase is not fully infected.  Human review is required.
    - **No copyleft detected**: passes.

    Args:
        diff: Raw ``git diff`` output.

    Returns:
        A single-element list containing the ``"license_obligations"``
        :class:`~bernstein.core.policy_engine.PermissionDecision`.
    """
    hits = _scan_diff_for_licenses(diff)

    if not hits:
        return [PermissionDecision(type=DecisionType.ALLOW, reason="No copyleft license obligations detected")]

    strong = [h for h in hits if h.copyleft_strength == "strong"]
    weak = [h for h in hits if h.copyleft_strength == "weak"]
    affected_files = sorted({h.file_path for h in hits if h.file_path})

    if strong:
        ids = ", ".join(sorted({h.license_id for h in strong}))
        return [
            PermissionDecision(
                type=DecisionType.DENY,
                reason=(
                    f"Strong copyleft license(s) detected — merge BLOCKED: {ids}. "
                    "GPL/AGPL-licensed code infects the entire codebase. "
                    "Remove or replace before merging."
                ),
                bypass_immune=True,
                files=tuple(affected_files),
            )
        ]

    ids = ", ".join(sorted({h.license_id for h in weak}))
    return [
        PermissionDecision(
            type=DecisionType.ASK,
            reason=(
                f"Weak copyleft license(s) detected — review required: {ids}. "
                "LGPL/MPL code may be linked but distribution obligations apply."
            ),
            files=tuple(affected_files),
        )
    ]
