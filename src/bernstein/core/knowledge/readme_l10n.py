"""README translation drift gate (issue #3425).

Splits the English ``README.md`` into sections, binds every translated
``README.<ietf-tag>.md`` section to a content hash of the English section
it mirrors, and verifies the bindings, code-block fidelity, and
verbatim header/footer on demand.

Design notes
------------

- **Section model.** A section starts at a ``###`` heading and runs to
  the next ``###`` heading. Everything before the first ``###`` is the
  ``(header)`` section; everything after the last ``###`` is the
  ``(footer)`` section. The header carries the logo block, badges, and
  the language links line; the footer carries the license line and the
  author block. Both are shared verbatim across translations (issue
  #3425: "Badges, the logo block, and the license line are shared
  verbatim"), so the gate compares them for equality instead of binding
  them to a hash. Prose sections in between are translatable and are
  bound to a hash of the English source they mirror.
- **Binding format.** Each translated section carries, directly under
  its translated heading, an HTML comment line::

      <!-- l10n: en="install in 30 seconds" hash="sha256:3f9a1c2b..." -->

  HTML comments do not render on GitHub, so the binding is machine
  readable and diffable without being visible noise.
- **Hash stability.** The hash input is normalised before hashing:
  trailing whitespace is stripped per line, blank lines are dropped, and
  lines are joined with ``\\n``. This absorbs prettier/editor noise
  (trailing whitespace, blank-line reflow) so the gate does not cry wolf
  on every formatting pass, while still catching any real content change
  to the English source.
- **Code blocks are never translated.** For every prose section, the
  gate extracts the fenced code blocks from the English section and the
  translated section and compares them (normalised). Any difference -
  a translated command name, a translated flag, a reformatted path - is
  a failure naming the language and the section.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from pathlib import Path

_HEADING_RE = re.compile(r"^###\s+(.+?)\s*$")
_FENCE_RE = re.compile(r"^```(\S*)\s*$", re.MULTILINE)
_BINDING_RE = re.compile(r'<!--\s*l10n:\s*en="([^"]+)"\s*hash="(sha256:[0-9a-f]+)"\s*-->')

# Public alias: the CLI ``sync`` command rewrites binding lines and needs
# the same pattern.
BINDING_RE = _BINDING_RE

HEADER_SECTION = "(header)"
FOOTER_SECTION = "(footer)"

_HASH_PREFIX = "sha256:"
_HASH_LEN = 12  # hex chars; 48 bits of collision resistance is ample here


@dataclass(frozen=True)
class Section:
    """One section of the English README."""

    heading: str  # heading text without the '### ' prefix
    body: str  # raw section body (everything after the heading line)


def split_sections(text: str) -> list[Section]:
    """Split README text into (header, prose..., footer) sections.

    The header is everything before the first ``###`` heading; each
    ``###`` heading opens a section that runs to the next heading. The
    footer is the tail after the last heading: it starts at the first
    standalone ``---`` line after the last heading (the conventional
    footer separator) and runs to the end of the file. If the last
    heading's section has no trailing separator, the section body runs
    to EOF and the footer is empty.
    """
    lines = text.splitlines(keepends=True)
    heading_idx: list[int] = []
    for i, line in enumerate(lines):
        if _HEADING_RE.match(line):
            heading_idx.append(i)

    if not heading_idx:
        return [Section(HEADER_SECTION, text)]

    footer_start = _footer_start(lines, heading_idx[-1])

    sections: list[Section] = []

    header_text = "".join(lines[: heading_idx[0]])
    sections.append(Section(HEADER_SECTION, header_text))

    for n, idx in enumerate(heading_idx):
        start = idx
        end = heading_idx[n + 1] if n + 1 < len(heading_idx) else footer_start
        match = _HEADING_RE.match(lines[idx])
        assert match is not None
        heading = match.group(1).strip()
        body = "".join(lines[start + 1 : end])
        sections.append(Section(heading, body))

    footer_text = "".join(lines[footer_start:])
    sections.append(Section(FOOTER_SECTION, footer_text))

    return sections


def _footer_start(lines: list[str], last_heading_idx: int) -> int:
    """Index of the footer separator: the first standalone ``---`` line
    after the last heading, outside any fenced code block."""
    in_fence = False
    for i in range(last_heading_idx + 1, len(lines)):
        if _FENCE_RE.match(lines[i]):
            in_fence = not in_fence
            continue
        if not in_fence and lines[i].strip() == "---":
            return i
    return len(lines)


def normalize(text: str) -> str:
    """Normalise markdown for stable hashing.

    Strips trailing whitespace per line, drops blank lines, joins with
    ``\\n``. Tolerant of prettier/editor reflow; sensitive to any real
    content change.
    """
    out: list[str] = []
    for line in text.splitlines():
        line = line.rstrip()
        if line.strip():
            out.append(line)
    return "\n".join(out)


def section_hash(section: Section) -> str:
    """Content hash of one English section, normalised."""
    digest = hashlib.sha256(normalize(section.body).encode("utf-8")).hexdigest()
    return f"{_HASH_PREFIX}{digest[:_HASH_LEN]}"


def extract_code_blocks(section: Section) -> list[str]:
    """Fenced code blocks of a section, normalised, in source order."""
    blocks: list[str] = []
    start = None
    for lineno, line in enumerate(section.body.splitlines()):
        if _FENCE_RE.match(line):
            if start is None:
                start = lineno + 1
            else:
                body = "\n".join(section.body.splitlines()[start:lineno])
                blocks.append(normalize(body))
                start = None
    return blocks


def parse_bindings(text: str) -> dict[str, str]:
    """Map English section name -> bound hash from a translated file."""
    return {en: h for en, h in _BINDING_RE.findall(text)}


def paragraph_count(body: str) -> int:
    """Number of paragraph-level blocks in a section body.

    Blocks are runs of non-blank lines separated by blank lines. A fenced
    code block counts as a single block regardless of internal blank
    lines. l10n binding comments are gate metadata, not content, and are
    ignored. Used to compare structure between an English section and the
    translation that mirrors it: hashes bind content, but a re-synced
    binding cannot tell whether a paragraph added to the English source
    ever reached the translation - the block count can.
    """
    blocks = 0
    in_block = False
    in_fence = False
    for line in body.splitlines():
        if _FENCE_RE.match(line):
            # A fence always opens a block of its own, even when it
            # follows prose with no blank line between them, and closing
            # it ends that block so following prose opens a new one.
            if not in_fence:
                blocks += 1
                in_fence = True
                in_block = True
            else:
                in_fence = False
                in_block = False
            continue
        if in_fence:
            continue
        if not line.strip():
            in_block = False
            continue
        if _BINDING_RE.search(line):
            continue
        if not in_block:
            blocks += 1
            in_block = True
    return blocks


# ---------------------------------------------------------------------------
# Verify
# ---------------------------------------------------------------------------


@dataclass
class VerifyResult:
    """Outcome of verifying one language file."""

    lang: str
    errors: list[str] = field(default_factory=list[str])

    @property
    def ok(self) -> bool:
        return not self.errors


def verify_language(source_sections: list[Section], lang: str, translated_text: str) -> VerifyResult:
    """Verify one translated README against the English source sections."""
    result = VerifyResult(lang=lang)
    bindings = parse_bindings(translated_text)
    by_heading = {s.heading: s for s in source_sections}
    prose = [s for s in source_sections if s.heading not in (HEADER_SECTION, FOOTER_SECTION)]

    # 1. Binding drift: every prose section must be bound to the current
    #    hash of its English source.
    for section in prose:
        expected = section_hash(section)
        bound = bindings.get(section.heading)
        if bound is None:
            result.errors.append(f'section "{section.heading}" has no l10n binding; run `bernstein readme-l10n sync`')
        elif bound != expected:
            result.errors.append(
                f'section "{section.heading}" is stale: translation binds '
                f"{bound}, English source now hashes to {expected}; run "
                "`bernstein readme-l10n sync`"
            )

    # 2. Code-block fidelity: fenced code in a translated section must be
    #    byte-identical (normalised) to the English section it mirrors.
    for section in prose:
        en_blocks = extract_code_blocks(section)
        trans_section = _find_translated_section(translated_text, section.heading)
        if trans_section is None:
            # Missing translated section is already covered by the binding
            # check above; do not double-report.
            continue
        tr_blocks = extract_code_blocks(trans_section)
        if len(en_blocks) != len(tr_blocks):
            result.errors.append(
                f'section "{section.heading}" has {len(tr_blocks)} code '
                f"block(s) but the English section has {len(en_blocks)}; "
                "code blocks must never be translated or removed"
            )
        else:
            for i, (en, tr) in enumerate(zip(en_blocks, tr_blocks, strict=True)):
                if en != tr:
                    result.errors.append(
                        f'section "{section.heading}" code block {i + 1} '
                        "was translated or altered; commands, flags, paths "
                        "and subcommands must stay verbatim"
                    )

    # 3. Binding placement: a duplicated or orphaned binding lets a
    #    section resolve to the wrong span, so parity below cannot be
    #    trusted until placement is unambiguous.
    result.errors.extend(binding_placement_errors(translated_text))

    # 4. Paragraph parity: a translated section must carry the same number
    #    of paragraph-level blocks as the English section it mirrors. The
    #    hash binding pins the English content, but `sync` re-binds after
    #    an English edit without proving the translation followed - a
    #    paragraph added to the English source can otherwise go missing
    #    from every translation while the gate stays green.
    for section in prose:
        trans_section = _find_translated_section(translated_text, section.heading)
        if trans_section is None:
            continue
        en_blocks_n = paragraph_count(section.body)
        tr_blocks_n = paragraph_count(trans_section.body)
        if en_blocks_n != tr_blocks_n:
            result.errors.append(
                f'section "{section.heading}" has {tr_blocks_n} paragraph '
                f"block(s) but the English section has {en_blocks_n}; "
                "translate the missing or extra paragraph(s) so the "
                "structures match"
            )

    # 5. Header/footer verbatim: logo, badges, language links line and the
    #    license/footer block are shared verbatim. The translated file
    #    mirrors the English structure, so its own header (before the
    #    first heading) and footer (after the last '---') map directly.
    trans_sections = split_sections(translated_text)
    trans_by_role = {
        trans_sections[0].heading: trans_sections[0],
        trans_sections[-1].heading: trans_sections[-1],
    }
    for name in (HEADER_SECTION, FOOTER_SECTION):
        en_section = by_heading[name]
        trans_section = trans_by_role.get(name)
        if trans_section is None:
            result.errors.append(f"{name} block is missing from the translation")
            continue
        if normalize(trans_section.body) != normalize(en_section.body):
            result.errors.append(
                f"{name} block must be shared verbatim with the English README (logo, badges, license line)"
            )

    return result


def _find_translated_section(text: str, en_heading: str) -> Section | None:
    """Locate the translated section that mirrors an English heading.

    The translated heading text differs (it is translated prose), so the
    section is located via its l10n binding comment: the first binding
    for ``en_heading`` sits directly under the translated heading, and
    the section runs to the next ``###`` heading (or l10n binding).
    """
    owned = _owned_sections(text)
    sections = owned.get(en_heading)
    if sections is None or len(sections) != 1:
        # Absent, or bound more than once: ambiguous placement is reported
        # by ``binding_placement_errors`` rather than silently resolved to
        # whichever copy happens to come first.
        return None
    return sections[0]


def _owned_sections(text: str) -> dict[str, list[Section]]:
    """Map English section name -> the translated sections bound to it.

    A binding is owned by the heading it sits under: the *first* binding
    inside a heading's span, per the documented placement (directly under
    the translated heading). The section body runs from that binding to
    the next heading, or to the footer separator for the last one -
    mirroring ``split_sections`` so the translated footer is never read
    as part of the last prose section. Resolving by owning heading is
    what keeps a stray or duplicated binding elsewhere in the file from
    redefining a section's boundaries.
    """
    lines = text.splitlines(keepends=True)
    heading_idx = [i for i, line in enumerate(lines) if _HEADING_RE.match(line)]
    if not heading_idx:
        return {}
    footer_start = _footer_start(lines, heading_idx[-1])

    owned: dict[str, list[Section]] = {}
    for n, idx in enumerate(heading_idx):
        end = heading_idx[n + 1] if n + 1 < len(heading_idx) else footer_start
        for j in range(idx + 1, min(end, len(lines))):
            m = _BINDING_RE.search(lines[j])
            if m is None:
                continue
            en = m.group(1)
            owned.setdefault(en, []).append(Section(en, "".join(lines[j + 1 : end])))
            break  # only the first binding under a heading owns it
    return owned


def binding_placement_errors(text: str) -> list[str]:
    """Report bindings a reader would misread as pinning a section.

    Two shapes are rejected: the same English section bound under more
    than one translated heading (which of them mirrors it?), and a
    binding that sits under no heading at all (before the first heading
    or below the footer separator), where it pins nothing. Both let a
    translated paragraph go missing while the hash bindings still
    reconcile, so they are failures rather than warnings.
    """
    errors: list[str] = []
    owned = _owned_sections(text)
    for en, sections in sorted(owned.items()):
        if len(sections) > 1:
            errors.append(f'section "{en}" is bound by {len(sections)} translated headings; exactly one must mirror it')

    lines = text.splitlines(keepends=True)
    heading_idx = [i for i, line in enumerate(lines) if _HEADING_RE.match(line)]
    first_heading = heading_idx[0] if heading_idx else len(lines)
    footer_start = _footer_start(lines, heading_idx[-1]) if heading_idx else len(lines)
    for i, line in enumerate(lines):
        m = _BINDING_RE.search(line)
        if m is None:
            continue
        if i < first_heading or i >= footer_start:
            errors.append(
                f'binding for section "{m.group(1)}" sits outside every '
                "translated heading; move it directly under the heading it mirrors"
            )
    return errors


_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def _read_pyproject(pyproject: Path) -> dict[str, object] | None:
    """Parse ``pyproject.toml``; ``None`` means there is no file to read.

    A file that exists but cannot be read or parsed is a configuration
    error, not an absent configuration. Returning an empty result there
    would let a stray tab in the TOML disable the drift gate while the
    run still exits 0 - the gate would report SKIP on a repo whose
    translations are silently rotting.
    """
    import tomllib

    try:
        text = pyproject.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ValueError(f"cannot read {pyproject.name}: {exc}") from exc
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"{pyproject.name} is not valid TOML: {exc}") from exc


def load_config(pyproject: Path) -> list[str]:
    """Read the configured language set from ``[tool.bernstein.readme-l10n]``.

    Missing config means no languages are verified (empty list). A
    malformed ``languages`` entry is a hard error so a typo cannot
    silently disable the gate.
    """
    return _languages_from(_read_pyproject(pyproject))


def _languages_from(data: dict[str, object] | None) -> list[str]:
    """Derive the language set from an already-parsed ``pyproject.toml``."""
    if data is None:
        return []

    tool_raw: object = data.get("tool")
    if not isinstance(tool_raw, dict):
        return []
    tool = cast(dict[str, object], tool_raw)

    bern_raw: object = tool.get("bernstein")
    if not isinstance(bern_raw, dict):
        return []
    bernstein = cast(dict[str, object], bern_raw)

    sec_raw: object = bernstein.get("readme-l10n")
    if not isinstance(sec_raw, dict):
        return []
    section = cast(dict[str, object], sec_raw)

    langs_raw: object = section.get("languages")
    if not isinstance(langs_raw, list):
        raise ValueError("[tool.bernstein.readme-l10n] languages must be a non-empty list of IETF tags")
    langs = cast(list[object], langs_raw)
    strs = [x for x in langs if isinstance(x, str)]
    if not strs or len(strs) != len(langs):
        raise ValueError("[tool.bernstein.readme-l10n] languages must be a non-empty list of IETF tags")
    return strs


def load_owners(pyproject: Path) -> dict[str, str]:
    """Read the per-language owner map from ``[tool.bernstein.readme-l10n.owners]``.

    Maps an IETF tag to the handle of whoever keeps that translation
    current. Missing config, or a missing ``owners`` table, means no
    language has a recorded owner - the gate still fails on drift, it
    just cannot say who to ask. A malformed entry is a hard error for
    the same reason a malformed ``languages`` entry is: a typo must not
    quietly turn a language into one nobody is named for.
    """
    return _owners_from(_read_pyproject(pyproject))


def _owners_from(data: dict[str, object] | None) -> dict[str, str]:
    """Derive the owner map from an already-parsed ``pyproject.toml``."""
    if data is None:
        return {}

    section: object = data.get("tool")
    for key in ("bernstein", "readme-l10n", "owners"):
        if not isinstance(section, dict):
            return {}
        section = cast(dict[str, object], section).get(key)
    if section is None:
        return {}
    if not isinstance(section, dict):
        raise ValueError('[tool.bernstein.readme-l10n.owners] must be a table of ietf-tag = "handle"')
    owners = cast(dict[str, object], section)
    bad = sorted(tag for tag, handle in owners.items() if not isinstance(handle, str) or not handle.strip())
    if bad:
        raise ValueError(
            "[tool.bernstein.readme-l10n.owners] entries must be non-empty strings; bad entries: " + ", ".join(bad)
        )
    # The handle is echoed into the verify output, which CI logs and
    # humans read. A newline or an escape sequence in it would let the
    # config forge lines in that report, so control bytes are refused
    # rather than stripped: a handle that needs them is a typo. The
    # refused set spans C1 as well as C0, because U+009B is a
    # single-character CSI that a terminal reading 8-bit controls will
    # act on exactly as it acts on the two-byte ESC form.
    forged = sorted(tag for tag, handle in owners.items() if _CONTROL_CHARS.search(cast(str, handle)))
    if forged:
        raise ValueError(
            "[tool.bernstein.readme-l10n.owners] handles may not contain control characters; bad entries: "
            + ", ".join(forged)
        )
    return {tag: cast(str, handle).strip() for tag, handle in owners.items()}


def load_settings(pyproject: Path) -> tuple[list[str], dict[str, str]]:
    """Read languages and owners from a single parse of ``pyproject.toml``.

    ``verify`` needs both. Reading the file twice would let an edit
    landing between the two reads pair the languages of one revision
    with the owners of another - the report would then name an owner
    for a language that revision does not configure, or omit one it
    does. One parse, both values, so the two can never disagree.
    """
    data = _read_pyproject(pyproject)
    return _languages_from(data), _owners_from(data)
