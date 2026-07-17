"""Correctness under compatibility (#2548).

Covers the acceptance criterion: existing SSE consumers and existing
``triggers.yaml`` configs work unchanged, and the create-task action path
produces identical results for existing configs. Eventing v2 is additive: it
projects the existing taxonomies, it does not replace them.
"""

from __future__ import annotations

from pathlib import Path

from bernstein.core.events.grammar import canonical_label_for_sse
from bernstein.core.orchestration.trigger_manager import load_trigger_configs
from bernstein.core.server.sse_events import SSEEventType


def test_every_sse_type_is_addressable_in_the_grammar() -> None:
    # The projection must address every existing SSE event type without changing
    # its identifier, so existing SSE consumers see no change.
    for member in SSEEventType:
        assert canonical_label_for_sse(member.value) == member.value


def test_existing_triggers_yaml_create_task_path_unchanged(tmp_path: Path) -> None:
    config = tmp_path / "triggers.yaml"
    config.write_text(
        """
triggers:
  - name: on-push
    source: github_push
    enabled: true
    filters:
      branches: ["main"]
    conditions:
      cooldown_seconds: 300
    task:
      title: "Handle push"
      role: backend
      priority: 1
      scope: small
      description_template: "Push to {branch}"
""".lstrip(),
        encoding="utf-8",
    )

    configs = load_trigger_configs(config)
    assert len(configs) == 1
    cfg = configs[0]
    assert cfg.name == "on-push"
    assert cfg.source == "github_push"
    # The create-task template parses exactly as before Eventing v2.
    assert cfg.task.title == "Handle push"
    assert cfg.task.role == "backend"
    assert cfg.task.priority == 1
    assert cfg.task.description_template == "Push to {branch}"
