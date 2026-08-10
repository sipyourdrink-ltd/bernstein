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
            if not in_fence and not in_block:
                blocks += 1
                in_block = True
            in_fence = not in_fence
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

    # 3. Paragraph parity: a translated section must carry the same number
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

    # 4. Header/footer verbatim: logo, badges, language links line and the
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
    lines = text.splitlines(keepends=True)
    heading_idx = [i for i, line in enumerate(lines) if _HEADING_RE.match(line)]
    # The last section stops where the footer starts, mirroring
    # ``split_sections`` - otherwise the translated footer is read as part
    # of the last prose section and its blocks are miscounted.
    footer_start = _footer_start(lines, heading_idx[-1]) if heading_idx else len(lines)
    # Find the binding line for this English heading.
    for i, line in enumerate(lines):
        m = _BINDING_RE.search(line)
        if m and m.group(1) == en_heading:
            start = i + 1
            end = footer_start
            for j in range(i + 1, min(len(lines), footer_start)):
                if _HEADING_RE.match(lines[j]):
                    end = j
                    break
                if _BINDING_RE.search(lines[j]) and j > i:
                    end = j
                    break
            return Section(en_heading, "".join(lines[start:end]))
    return None


def load_config(pyproject: Path) -> list[str]:
    """Read the configured language set from ``[tool.bernstein.readme-l10n]``.

    Missing config means no languages are verified (empty list). A
    malformed ``languages`` entry is a hard error so a typo cannot
    silently disable the gate.
    """
    import tomllib

    try:
        data: dict[str, object] = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
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
