"""Recipe manifest schema - parameterised workflows for operators.

A recipe is a thin wrapper around :class:`WorkflowSpec` that adds an
operator-facing ``params`` block.  Each parameter declares a name, type
hint, default value, required flag, and a one-line help string.  When the
operator launches a recipe with ``--param key=value`` arguments the CLI
validates the inputs, applies defaults, and renders all ``{param}``
placeholders in prompts and commands before handing a finished
:class:`WorkflowSpec` to the existing :class:`WorkflowRunner`.

Example manifest::

    name: bump-dependency
    description: "Upgrade a Python dependency and fix test breakage."
    version: "1.0.0"
    params:
      - name: package
        type: string
        required: true
        help: "Distribution name on PyPI (e.g. 'httpx')."
      - name: version
        type: string
        required: true
        help: "Target version specifier (e.g. '0.27.0')."
      - name: run_tests
        type: bool
        default: true
        help: "Run pytest after the bump."
    nodes:
      - id: bump
        agent: backend
        prompt: "Upgrade {package} to {version} in pyproject.toml. Goal: {goal}"
      - id: tests
        depends_on: [bump]
        command: "pytest -x"

The recipe layer is intentionally additive: a manifest without a
``params`` block parses as a recipe with zero params and behaves exactly
like a vanilla workflow.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from bernstein.core.tasks.param_contract import ParamType as ParamType
from bernstein.core.tasks.param_contract import (
    coerce_value as _shared_coerce_value,
)
from bernstein.core.workflows.workflow_spec import (
    WorkflowSpec,
    WorkflowSpecError,
    load_workflow_spec_from_text,
)

# ---------------------------------------------------------------------------
# Parameter declaration
# ---------------------------------------------------------------------------

# Parameter names mirror node id constraints - slug-shaped so they round
# trip through YAML keys and CLI flags without escaping.
_PARAM_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")

# The scalar type vocabulary is owned by
# :mod:`bernstein.core.tasks.param_contract` and re-exported above so recipes
# and schedules (#2545) share one declaration.  Kept small on purpose: recipes
# are operator-facing, not a general-purpose templating engine.


class RecipeParam(BaseModel):
    """One operator-facing recipe parameter.

    Attributes:
        name: Slug-shaped parameter name.  Referenced as ``{name}`` in
            prompt / command bodies.
        type: One of ``string``, ``int``, ``float``, ``bool``.
        default: Optional default value applied when the operator omits
            the parameter at launch.  Stored as the declared type.
        required: When ``True``, missing values produce a clean exit-1
            instead of falling back to ``default`` / empty string.
        help: One-line operator-facing description.
        choices: Optional whitelist for ``string`` params.  Values
            outside the list are rejected at validation time.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=64)
    type: ParamType = "string"
    default: str | int | float | bool | None = None
    required: bool = False
    help: str = Field(default="", max_length=256)
    choices: list[str] | None = None

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        """Reject names that are not slug-shaped."""
        if not _PARAM_NAME_PATTERN.match(value):
            raise ValueError(
                f"param name {value!r} must match {_PARAM_NAME_PATTERN.pattern}",
            )
        if value == "goal":
            # ``goal`` is reserved - it's substituted by the WorkflowRunner
            # itself from the CLI ``--goal`` option, not by the recipe
            # layer.  Collisions silently shadow operator input.
            raise ValueError("param name 'goal' is reserved")
        return value

    @model_validator(mode="after")
    def _check_default_matches_type(self) -> RecipeParam:
        """Default value (when present) must coerce; choices stay string-only."""
        if self.choices is not None and self.type != "string":
            raise ValueError(
                f"param {self.name!r} declares choices but type is {self.type!r}; "
                "choices only apply to 'string' params",
            )
        if self.default is None:
            return self
        try:
            _coerce_value(str(self.default), self.type)
        except RecipeParamError as exc:
            raise ValueError(f"default for {self.name!r}: {exc}") from exc
        if self.choices is not None and str(self.default) not in self.choices:
            raise ValueError(
                f"default {self.default!r} for {self.name!r} not in choices {self.choices!r}",
            )
        return self


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class RecipeSpecError(ValueError):
    """Raised when a recipe manifest is malformed or fails schema validation."""


class RecipeParamError(ValueError):
    """Raised when operator-provided parameter values fail validation.

    Distinct from :class:`RecipeSpecError` so the CLI can map manifest
    errors to one exit code (2 - bad recipe on disk) and operator input
    errors to a different one (1 - bad ``--param`` value).
    """


# ---------------------------------------------------------------------------
# Coercion
# ---------------------------------------------------------------------------


def _coerce_value(raw: str, type_: ParamType) -> str | int | float | bool:
    """Coerce a raw CLI string into the parameter's declared scalar type.

    Delegates to the shared
    :func:`bernstein.core.tasks.param_contract.coerce_value` (the extracted
    vocabulary) and re-raises coercion failures as :class:`RecipeParamError` so
    the recipe layer's exit-code mapping and messages stay byte-identical.

    Args:
        raw: Raw string value as it appeared on the command line.
        type_: Declared parameter type.

    Returns:
        The coerced Python value.

    Raises:
        RecipeParamError: When ``raw`` does not parse as the declared
            type.  Bool accepts the usual truthy / falsy spellings.
    """
    try:
        return _shared_coerce_value(raw, type_)
    except ValueError as exc:
        raise RecipeParamError(str(exc)) from exc


# ---------------------------------------------------------------------------
# Recipe spec
# ---------------------------------------------------------------------------


class RecipeSpec(BaseModel):
    """Top-level recipe manifest model.

    A recipe is a workflow plus a typed ``params`` block.  The recipe
    body is exactly the same shape as :class:`WorkflowSpec` so the same
    runner can execute it once parameters are substituted.

    Attributes:
        name: Slug-shaped recipe name.  Also the file stem on disk.
        description: One-line human-readable description.
        version: Semver-ish version string.
        params: Operator-facing parameters.  Order is preserved for
            display purposes (``recipes show`` renders the table in
            declaration order).
        nodes: Raw node mappings forwarded to :class:`WorkflowSpec`
            after parameter substitution.  Kept as ``list[dict]`` here
            so unresolved ``{param}`` placeholders don't trip the
            workflow validator at recipe-load time.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=64)
    description: str = Field(min_length=1, max_length=512)
    version: str = Field(default="1.0.0")
    params: list[RecipeParam] = Field(default_factory=list)
    nodes: list[dict[str, Any]] = Field(min_length=1)
    # Optional #2546 registration blocks. A manifest without them parses and
    # behaves exactly as before (zero schedules, zero triggers, empty pool).
    # They carry the registration surface (schedule kinds, triggers, sandbox
    # pool, pins) folded into the content-addressed recipe hash.
    schedules: list[dict[str, Any]] = Field(default_factory=list)
    triggers: list[dict[str, Any]] = Field(default_factory=list)
    sandbox_pool: str = Field(default="", max_length=128)
    pins: dict[str, Any] = Field(default_factory=dict)

    @field_validator("params")
    @classmethod
    def _check_unique_param_names(cls, value: list[RecipeParam]) -> list[RecipeParam]:
        """Param names must be unique within a recipe."""
        seen: set[str] = set()
        for param in value:
            if param.name in seen:
                raise ValueError(f"duplicate param name {param.name!r}")
            seen.add(param.name)
        return value

    def param_by_name(self, name: str) -> RecipeParam:
        """Return the param with ``name`` or raise :class:`KeyError`."""
        for param in self.params:
            if param.name == name:
                return param
        raise KeyError(name)

    def resolve_params(self, overrides: dict[str, str]) -> dict[str, str | int | float | bool]:
        """Apply defaults to ``overrides`` and validate the result.

        Args:
            overrides: Raw operator-supplied values keyed by param name.
                Values arrive as strings from the CLI; this method
                coerces them to the declared type.

        Returns:
            Mapping from parameter name to coerced value.  Includes
            defaulted entries for params the operator omitted.

        Raises:
            RecipeParamError: When required params are missing, unknown
                param names appear in ``overrides``, choices are
                violated, or a value fails coercion.
        """
        declared = {p.name for p in self.params}
        unknown = sorted(set(overrides) - declared)
        if unknown:
            raise RecipeParamError(
                f"unknown param(s): {', '.join(unknown)}; declared: {sorted(declared) or '(none)'}",
            )
        resolved: dict[str, str | int | float | bool] = {}
        missing: list[str] = []
        for param in self.params:
            if param.name in overrides:
                value = _coerce_value(overrides[param.name], param.type)
                if param.choices is not None and value not in param.choices:
                    raise RecipeParamError(
                        f"{param.name}={value!r} not in choices {param.choices!r}",
                    )
                resolved[param.name] = value
                continue
            if param.default is not None:
                resolved[param.name] = param.default
                continue
            if param.required:
                missing.append(param.name)
        if missing:
            raise RecipeParamError(
                f"missing required param(s): {', '.join(missing)}",
            )
        return resolved

    def to_workflow_spec(
        self,
        *,
        param_values: dict[str, str | int | float | bool] | None = None,
    ) -> WorkflowSpec:
        """Render the recipe into a runnable :class:`WorkflowSpec`.

        Args:
            param_values: Result of :meth:`resolve_params`.  When
                ``None``, all params must have defaults - otherwise
                :class:`RecipeParamError` is raised through the resolver.

        Returns:
            A validated :class:`WorkflowSpec` with all ``{param}``
            placeholders substituted in agent prompts and command
            bodies.  The ``{goal}`` placeholder is left intact so the
            runner can still substitute the operator-supplied goal.

        Raises:
            RecipeSpecError: When the rendered workflow body fails the
                downstream :class:`WorkflowSpec` validator (e.g. cyclic
                ``depends_on`` graph that was not detectable from raw
                node dicts at recipe-load time).
        """
        if param_values is None:
            param_values = self.resolve_params({})

        rendered_nodes = [_substitute_node(node, param_values) for node in self.nodes]
        body: dict[str, Any] = {
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "nodes": rendered_nodes,
        }
        try:
            return WorkflowSpec.model_validate(body)
        except ValidationError as exc:
            raise RecipeSpecError(
                f"recipe {self.name!r} produced an invalid workflow: {exc}",
            ) from exc
        except WorkflowSpecError as exc:  # pragma: no cover - defensive
            raise RecipeSpecError(str(exc)) from exc


def _substitute_node(node: dict[str, Any], values: dict[str, str | int | float | bool]) -> dict[str, Any]:
    """Return a copy of ``node`` with ``{param}`` placeholders rendered.

    Substitution targets the two free-text fields (``prompt`` and
    ``command``) plus the loop predicate.  Other fields pass through
    unchanged - substituting into ``id`` or ``depends_on`` would let a
    recipe rewire its own DAG at launch, which the spec deliberately
    forbids.
    """
    out: dict[str, Any] = node.copy()
    for key in ("prompt", "command"):
        raw = out.get(key)
        if isinstance(raw, str):
            out[key] = _format_template(raw, values)
    loop = out.get("loop")
    if isinstance(loop, dict):
        # ``isinstance`` narrows to ``dict[Unknown, Unknown]`` under
        # strict pyright; cast to a typed shape before mutating.
        loop_dict: dict[str, Any] = dict(loop)  # type: ignore[arg-type]
        until_raw = loop_dict.get("until")
        if isinstance(until_raw, str):
            loop_dict["until"] = _format_template(until_raw, values)
            out["loop"] = loop_dict
    return out


def _format_template(template: str, values: dict[str, str | int | float | bool]) -> str:
    """Substitute ``{name}`` placeholders that match declared params.

    Keeps unrelated placeholders (notably ``{goal}``) intact so the
    workflow runner can substitute them downstream.  Implemented with a
    regex sweep instead of :meth:`str.format_map` because the latter
    would choke on any brace pair that doesn't match a known key.
    """
    if not values:
        return template
    pattern = re.compile(r"\{([a-z][a-z0-9_]*)\}")

    def _replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key in values:
            return str(values[key])
        return match.group(0)

    return pattern.sub(_replace, template)


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------


def load_recipe_spec_from_text(text: str) -> RecipeSpec:
    """Parse ``text`` as a YAML recipe manifest.

    Args:
        text: Raw manifest body.

    Returns:
        A validated :class:`RecipeSpec`.  Note: the node DAG is not
        validated here - placeholders may still be unresolved, so the
        downstream :class:`WorkflowSpec` validator runs only inside
        :meth:`RecipeSpec.to_workflow_spec`.

    Raises:
        RecipeSpecError: On YAML parse failure or schema violation.
    """
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise RecipeSpecError(f"malformed YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise RecipeSpecError("recipe manifest must be a mapping at the top level")
    try:
        return RecipeSpec.model_validate(data)
    except ValidationError as exc:
        raise RecipeSpecError(str(exc)) from exc


def load_recipe_spec(path: Path) -> RecipeSpec:
    """Load a recipe manifest from disk."""
    if not path.is_file():
        raise RecipeSpecError(f"recipe manifest not found: {path}")
    return load_recipe_spec_from_text(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def _bundled_recipes_dir() -> Path:
    """Return the bundled stock-recipes directory.

    Resolves the wheel-installed ``_default_templates/recipes`` or the
    dev-checkout ``templates/recipes``.
    """
    from bernstein import _BUNDLED_TEMPLATES_DIR

    return _BUNDLED_TEMPLATES_DIR / "recipes"


def _user_recipes_dirs(workdir: Path | None = None) -> list[Path]:
    """Return user-installed recipe directories, in lookup order.

    Project-local ``<workdir>/.bernstein/recipes/`` wins over
    ``~/.bernstein/recipes/`` so a checked-in recipe shadows a
    home-directory copy.
    """
    candidates: list[Path] = []
    if workdir is not None:
        candidates.append(workdir / ".bernstein" / "recipes")
    candidates.append(Path.home() / ".bernstein" / "recipes")
    return candidates


def discover_recipes(
    *,
    workdir: Path | None = None,
    include_bundled: bool = True,
    include_user: bool = True,
) -> Iterator[tuple[str, Path]]:
    """Yield ``(name, path)`` pairs for every reachable recipe manifest.

    Lookup order matches :func:`discover_workflows`: project-local,
    user-home, then bundled.  Names are deduplicated across sources -
    the first occurrence wins.
    """
    seen: set[str] = set()
    dirs: list[Path] = []
    if include_user:
        dirs.extend(_user_recipes_dirs(workdir))
    if include_bundled:
        dirs.append(_bundled_recipes_dir())
    for directory in dirs:
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.yaml")) + sorted(directory.glob("*.yml")):
            name = path.stem
            if name in seen:
                continue
            seen.add(name)
            yield name, path


def resolve_recipe(
    name_or_path: str,
    *,
    workdir: Path | None = None,
) -> tuple[Path, RecipeSpec]:
    """Resolve a name or filesystem path to a loaded :class:`RecipeSpec`.

    Args:
        name_or_path: Either a path on disk or a bare recipe name.
        workdir: Project root for project-local discovery.

    Returns:
        ``(path, spec)`` for the resolved manifest.

    Raises:
        RecipeSpecError: When the recipe can't be located or fails
            validation.
    """
    candidate = Path(name_or_path)
    if candidate.suffix in {".yaml", ".yml"} or candidate.exists():
        return candidate.resolve(), load_recipe_spec(candidate)
    for name, path in discover_recipes(workdir=workdir):
        if name == name_or_path:
            return path.resolve(), load_recipe_spec(path)
    raise RecipeSpecError(
        f"recipe {name_or_path!r} not found; "
        "pass a path or place a YAML in templates/recipes/, "
        ".bernstein/recipes/, or ~/.bernstein/recipes/",
    )


# ---------------------------------------------------------------------------
# CLI helpers
# ---------------------------------------------------------------------------


def parse_param_overrides(raw: tuple[str, ...] | list[str]) -> dict[str, str]:
    """Parse ``--param key=value`` arguments into a raw string mapping.

    Args:
        raw: Sequence of ``key=value`` strings as collected by Click.

    Returns:
        Dict from key to raw string value.

    Raises:
        RecipeParamError: When an entry lacks an ``=`` separator or
            specifies the same key twice.
    """
    out: dict[str, str] = {}
    for entry in raw:
        if "=" not in entry:
            raise RecipeParamError(
                f"invalid --param {entry!r}; expected key=value",
            )
        key, _, value = entry.partition("=")
        key = key.strip()
        if not key:
            raise RecipeParamError(
                f"invalid --param {entry!r}; missing key",
            )
        if key in out:
            raise RecipeParamError(
                f"duplicate --param {key!r}",
            )
        out[key] = value
    return out


# Re-export the workflow loader for downstream consumers that want a
# uniform import surface.  Keeps test modules from reaching into the
# workflow_spec module directly.
__all__ = [
    "RecipeParam",
    "RecipeParamError",
    "RecipeSpec",
    "RecipeSpecError",
    "discover_recipes",
    "load_recipe_spec",
    "load_recipe_spec_from_text",
    "load_workflow_spec_from_text",
    "parse_param_overrides",
    "resolve_recipe",
]
