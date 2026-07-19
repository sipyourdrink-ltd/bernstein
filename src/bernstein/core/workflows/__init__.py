"""Declarative YAML workflow manifests.

Companion to ``bernstein run -g <goal>``: a declarative way to run a
DAG of agent / command / loop nodes through the existing
:class:`bernstein.core.spawner.AgentSpawner` rather than introducing a
parallel spawn path.

Public surface:

* :class:`WorkflowSpec` / :class:`WorkflowNode` / :class:`LoopSpec` -
  Pydantic v2 schema for the YAML manifest (see
  :mod:`bernstein.core.workflows.workflow_spec`).
* :class:`WorkflowRunner` - DAG executor (see
  :mod:`bernstein.core.workflows.workflow_runner`).
"""

from __future__ import annotations

from bernstein.core.workflows.recipe_spec import (
    RecipeParam,
    RecipeParamError,
    RecipeSpec,
    RecipeSpecError,
    discover_recipes,
    load_recipe_spec,
    load_recipe_spec_from_text,
    parse_param_overrides,
    resolve_recipe,
)
from bernstein.core.workflows.workflow_runner import (
    NodeExecution,
    NodeStatus,
    WorkflowExecution,
    WorkflowRunError,
    WorkflowRunner,
)
from bernstein.core.workflows.workflow_spec import (
    LoopSpec,
    WorkflowNode,
    WorkflowSpec,
    WorkflowSpecError,
    discover_workflows,
    load_workflow_spec,
    load_workflow_spec_from_text,
)

__all__ = [
    "LoopSpec",
    "NodeExecution",
    "NodeStatus",
    "RecipeParam",
    "RecipeParamError",
    "RecipeSpec",
    "RecipeSpecError",
    "WorkflowExecution",
    "WorkflowNode",
    "WorkflowRunError",
    "WorkflowRunner",
    "WorkflowSpec",
    "WorkflowSpecError",
    "discover_recipes",
    "discover_workflows",
    "load_recipe_spec",
    "load_recipe_spec_from_text",
    "load_workflow_spec",
    "load_workflow_spec_from_text",
    "parse_param_overrides",
    "resolve_recipe",
]
