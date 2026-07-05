"""Template renderer for role system prompts.

Loads Markdown templates from templates/roles/{role}/system_prompt.md,
substitutes {{PLACEHOLDER}} variables, and handles {{#IF VAR}}...{{/IF}}
and {{#IF_NOT VAR}}...{{/IF_NOT}} conditional blocks.

Nested conditionals are NOT supported in v1.
"""

from __future__ import annotations

import logging
import re
import shlex
import subprocess
from pathlib import Path

from bernstein import _BUNDLED_TEMPLATES_DIR  # type: ignore[reportPrivateUsage]

logger = logging.getLogger(__name__)


class TemplateError(Exception):
    """Raised when template rendering fails (missing file, bad syntax, etc.)."""


# Default templates directory - works both from source tree and after pip install.
_DEFAULT_TEMPLATES_DIR = _BUNDLED_TEMPLATES_DIR / "roles"

# Regex patterns - compiled once at module level.
_IF_BLOCK_RE = re.compile(r"\{\{#IF\s+(\w+)\}\}(.*?)\{\{/IF\}\}", re.DOTALL)
_IF_NOT_BLOCK_RE = re.compile(r"\{\{#IF_NOT\s+(\w+)\}\}(.*?)\{\{/IF_NOT\}\}", re.DOTALL)
_PLACEHOLDER_RE = re.compile(r"\{\{(\w+)\}\}")
_SHELL_CMD_RE = re.compile(r"!" + "`" + r"([^`]+)" + "`")
_INCLUDE_RE = re.compile(r"\{\{INCLUDE\s+([\w-]+)\}\}")

# Directory name for shared template partials, looked up next to the
# template and one level up (e.g. templates/roles/_includes/ for a
# template at templates/roles/backend/task_prompt.md).
_INCLUDES_DIRNAME = "_includes"


def _resolve_conditionals(template: str, context: dict[str, str]) -> str:
    """Expand or strip conditional blocks based on context truthiness.

    Args:
        template: Raw template string containing conditional blocks.
        context: Variable mapping. A key is truthy if present and non-empty.

    Returns:
        Template with conditional blocks resolved.
    """

    def _replace_if(match: re.Match[str]) -> str:
        var = match.group(1)
        body = match.group(2)
        return body if context.get(var) else ""

    def _replace_if_not(match: re.Match[str]) -> str:
        var = match.group(1)
        body = match.group(2)
        return body if not context.get(var) else ""

    result = _IF_BLOCK_RE.sub(_replace_if, template)
    result = _IF_NOT_BLOCK_RE.sub(_replace_if_not, result)
    return result


def _execute_shell_commands(template: str) -> str:
    """Execute shell command tokens and substitute their stdout (T678).

    The ``!`command` syntax embeds command output into the template.
    Commands are executed with a 10-second timeout.

    Args:
        template: Template with ``!`command` tokens.

    Returns:
        Template with command output substituted.
    """

    def _run_cmd(match: re.Match[str]) -> str:
        cmd = match.group(1).strip()
        try:
            result = subprocess.run(
                shlex.split(cmd),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
            )
            if result.returncode != 0:
                logger.warning(
                    "Shell command in template exited %d: %s",
                    result.returncode,
                    result.stderr[:200],
                )
                return f"[shell command failed: {cmd}]"
            return result.stdout.strip()
        except subprocess.TimeoutExpired:
            logger.warning("Shell command in template timed out: %s", cmd)
            return f"[shell command timed out: {cmd}]"
        except Exception as exc:
            logger.warning("Shell command in template failed: %s: %s", cmd, exc)
            return f"[shell command error: {cmd}]"

    return _SHELL_CMD_RE.sub(_run_cmd, template)


def _resolve_includes(template: str, template_path: Path) -> str:
    """Expand ``{{INCLUDE name}}`` directives from shared partial files.

    Partials live in an ``_includes/`` directory next to the template's
    directory or one level up, so all roles can share one instruction
    block (e.g. ``templates/roles/_includes/completion_contract.md``).
    Expansion is a single pass - partials cannot include other partials.

    Args:
        template: Raw template string.
        template_path: Path of the template being rendered, used to
            locate the ``_includes`` directory.

    Returns:
        Template with include directives replaced by partial contents.

    Raises:
        TemplateError: When a referenced partial does not exist or
            cannot be read. Failing fast beats silently dropping an
            instruction block from a worker prompt.
    """

    def _replace(match: re.Match[str]) -> str:
        name = match.group(1)
        candidates = (
            template_path.parent / _INCLUDES_DIRNAME / f"{name}.md",
            template_path.parent.parent / _INCLUDES_DIRNAME / f"{name}.md",
        )
        for candidate in candidates:
            if candidate.is_file():
                try:
                    return candidate.read_text(encoding="utf-8")
                except OSError as exc:
                    raise TemplateError(f"Cannot read include {candidate}: {exc}") from exc
        raise TemplateError(f"Include '{name}' not found near {template_path} (looked in {_INCLUDES_DIRNAME}/)")

    return _INCLUDE_RE.sub(_replace, template)


def _substitute_placeholders(template: str, context: dict[str, str]) -> str:
    """Replace {{VAR}} placeholders with values from context.

    Unknown placeholders (not in context) are left as-is for forward
    compatibility - callers may layer multiple render passes.

    Args:
        template: Template string with placeholder tokens.
        context: Variable mapping.

    Returns:
        Template with known placeholders substituted.
    """

    def _replace(match: re.Match[str]) -> str:
        var = match.group(1)
        if var in context:
            return context[var]
        # Unknown placeholder - leave as-is.
        return match.group(0)

    return _PLACEHOLDER_RE.sub(_replace, template)


def render_template(template_path: Path, context: dict[str, str]) -> str:
    """Load a template file and render it with the given context.

    Processing order:
      1. Expand ``{{INCLUDE name}}`` partials from ``_includes/``.
      2. Resolve {{#IF VAR}}...{{/IF}} and {{#IF_NOT VAR}}...{{/IF_NOT}} blocks.
      3. Execute ``!`command` shell command tokens.
      4. Substitute remaining {{VAR}} placeholders.

    Args:
        template_path: Absolute or relative path to the template file.
        context: Mapping of placeholder names to replacement strings.

    Returns:
        Fully rendered template string.

    Raises:
        FileNotFoundError: If template_path does not exist.
        TemplateError: If the template cannot be read.
    """
    path = Path(template_path)
    if not path.exists():
        raise FileNotFoundError(f"Template not found: {path}")

    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise TemplateError(f"Cannot read template {path}: {exc}") from exc

    rendered = _resolve_includes(raw, path)
    rendered = _resolve_conditionals(rendered, context)
    rendered = _execute_shell_commands(rendered)
    rendered = _substitute_placeholders(rendered, context)
    return rendered


def render_role_prompt(
    role: str,
    context: dict[str, str],
    templates_dir: Path | None = None,
) -> str:
    """Render the system prompt for a given agent role.

    Convenience wrapper around ``render_template`` that locates
    ``templates/roles/{role}/system_prompt.md`` automatically.

    Optionally appends an install-rev fingerprint footer as a markdown
    HTML comment at the very end of the rendered prompt.
    The footer is appended **after** the existing prompt body, so the
    cacheable prefix (everything that drives KV-cache locality across
    spawns of the same role) is byte-identical to what callers got
    before the wiring landed.  When emission is disabled or the token
    is the disabled sentinel, the footer is suppressed entirely - we
    never inject a useless ``<!-- bernstein-rev: 0…0 -->`` line.

    Args:
        role: Role name (e.g. "manager", "backend", "qa").
        context: Placeholder values for the template.
        templates_dir: Override for the roles template directory.
            Defaults to ``<repo>/templates/roles/``.

    Returns:
        Rendered system prompt string.

    Raises:
        FileNotFoundError: If the role template does not exist.
        TemplateError: On read errors.
    """
    base = templates_dir if templates_dir is not None else _DEFAULT_TEMPLATES_DIR
    template_path = base / role / "system_prompt.md"
    rendered = render_template(template_path, context)

    # Lazy import - keeps templates/ importable in stripped test envs and
    # avoids a cycle if identity ever needs templating.  Reading the gate
    # via the module attribute (not the re-export) means tests can flip
    # it with ``monkeypatch.setattr(install_rev, ...)`` and see live
    # behaviour without rebuilding caches at the import boundary.
    from bernstein.core.identity import install_rev as _identity
    from bernstein.core.identity.install_rev import (
        DISABLED_SENTINEL,
        render_md_footer,
    )

    if not _identity.IDENTITY_EMISSION_ENABLED:
        return rendered
    footer = render_md_footer()
    if DISABLED_SENTINEL in footer:
        return rendered
    # Append-only - preserves prefix bytes verbatim for KV-cache reuse.
    sep = "" if rendered.endswith("\n") else "\n"
    return f"{rendered}{sep}{footer}\n"
