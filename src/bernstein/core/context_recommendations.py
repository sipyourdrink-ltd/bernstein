"""Project-specific recommendations injected into agent prompts."""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, ClassVar, cast

import yaml

if TYPE_CHECKING:
    from pathlib import Path


@dataclass(frozen=True)
class Recommendation:
    """A project-specific recommendation for agents."""

    id: str
    category: str
    text: str
    severity: str
    applies_to_roles: list[str]
    source: str


class RecommendationEngine:
    """Build and serve context recommendations for agent prompts."""

    _SEVERITY_ORDER: ClassVar[dict[str, int]] = {"critical": 0, "important": 1, "suggestion": 2}

    def __init__(self, workdir: Path) -> None:
        self._workdir = workdir
        self._recommendations: list[Recommendation] = []
        self._built = False

    def build(self) -> None:
        """Collect recommendations from all supported sources."""
        self._recommendations = []
        self._load_from_claude_md()
        self._load_from_project_md()
        self._load_from_failure_history()
        self._load_from_recommendations_file()
        self._recommendations = self._dedupe(self._recommendations)
        self._built = True

    def for_role(self, role: str) -> list[Recommendation]:
        """Return recommendations applicable to ``role``."""
        if not self._built:
            self.build()
        applicable = [
            rec for rec in self._recommendations if not rec.applies_to_roles or role in set(rec.applies_to_roles)
        ]
        return sorted(
            applicable,
            key=lambda rec: (self._SEVERITY_ORDER.get(rec.severity, 3), rec.category, rec.id),
        )

    def all_recommendations(self) -> list[Recommendation]:
        """Return all built recommendations in deterministic order."""
        if not self._built:
            self.build()
        return list(self._recommendations)

    def render_for_prompt(self, role: str, max_chars: int = 2000) -> str:
        """Render recommendations as a prompt section."""
        recommendations = self.for_role(role)
        if not recommendations:
            return ""

        grouped: dict[str, list[str]] = {"critical": [], "important": [], "suggestion": []}
        for rec in recommendations:
            grouped.setdefault(rec.severity, []).append(f"- {rec.text}")

        sections: list[str] = ["## Project recommendations"]
        if grouped["critical"]:
            sections.append("")
            sections.append("**CRITICAL:**")
            sections.extend(grouped["critical"])
        if grouped["important"]:
            sections.append("")
            sections.append("**IMPORTANT:**")
            sections.extend(grouped["important"])
        if grouped["suggestion"]:
            sections.append("")
            sections.append("**SUGGESTIONS:**")
            sections.extend(grouped["suggestion"])

        rendered = "\n".join(sections).strip()
        if len(rendered) <= max_chars:
            return rendered

        trimmed: list[str] = ["## Project recommendations"]
        for rec in recommendations:
            candidate = "\n".join([*trimmed, f"- {rec.severity.upper()}: {rec.text}"]).strip()
            if len(candidate) > max_chars:
                break
            trimmed.append(f"- {rec.severity.upper()}: {rec.text}")
        return "\n".join(trimmed).strip()

    def record_hits(self, role: str, recommendations: list[Recommendation]) -> None:
        """Record which recommendations were injected into an agent prompt."""
        if not recommendations:
            return
        hits_path = self._workdir / ".sdd" / "metrics" / "recommendation_hits.jsonl"
        hits_path.parent.mkdir(parents=True, exist_ok=True)
        with hits_path.open("a", encoding="utf-8") as handle:
            for rec in recommendations:
                payload = {
                    "timestamp": datetime.now(UTC).isoformat(),
                    "role": role,
                    "recommendation_id": rec.id,
                    "category": rec.category,
                    "source": rec.source,
                }
                handle.write(json.dumps(payload, sort_keys=True) + "\n")

    def load_hit_counts(self) -> dict[str, int]:
        """Aggregate recommendation injection counts by ID."""
        hits_path = self._workdir / ".sdd" / "metrics" / "recommendation_hits.jsonl"
        if not hits_path.exists():
            return {}
        counts: Counter[str] = Counter()
        for raw_line in hits_path.read_text(encoding="utf-8").splitlines():
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                event = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            recommendation_id = str(event.get("recommendation_id", "")).strip()
            if recommendation_id:
                counts[recommendation_id] += 1
        return dict(counts)

    def ensure_seed_file(self) -> None:
        """Create a recommendations YAML file when none exists yet."""
        path = self._workdir / ".sdd" / "recommendations.yaml"
        if path.exists():
            return
        self.build()
        payload = {
            "recommendations": [
                {
                    "id": rec.id,
                    "category": rec.category,
                    "severity": rec.severity,
                    "text": rec.text,
                    "applies_to": rec.applies_to_roles,
                }
                for rec in self._recommendations[:10]
            ]
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")

    def _load_from_claude_md(self) -> None:
        """Parse ``CLAUDE.md`` for imperative project recommendations."""
        path = self._workdir / "CLAUDE.md"
        if not path.exists():
            return
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            stripped = line.strip().lstrip("-*").strip()
            if not stripped:
                continue
            lowered = stripped.lower()
            if not any(token in lowered for token in ("never", "always", "do not", "run ", "use ", "`")):
                continue
            self._recommendations.append(
                Recommendation(
                    id=self._make_id("claude", stripped),
                    category=self._category_for_text(stripped),
                    text=stripped,
                    severity=self._severity_for_text(stripped),
                    applies_to_roles=[],
                    source="claude_md",
                )
            )

    def _load_from_project_md(self) -> None:
        """Parse ``.sdd/project.md`` for project-specific conventions."""
        path = self._workdir / ".sdd" / "project.md"
        if not path.exists():
            return
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            stripped = line.strip().lstrip("-*").strip()
            if not stripped or len(stripped) < 8:
                continue
            lowered = stripped.lower()
            if "`" not in stripped and not any(token in lowered for token in ("always", "never", "use", "run")):
                continue
            self._recommendations.append(
                Recommendation(
                    id=self._make_id("project", stripped),
                    category=self._category_for_text(stripped),
                    text=stripped,
                    severity=self._severity_for_text(stripped, default="important"),
                    applies_to_roles=[],
                    source="project_md",
                )
            )

    def _load_from_failure_history(self) -> None:
        """Generate preventive recommendations from repeated gate failures."""
        path = self._workdir / ".sdd" / "metrics" / "quality_gates.jsonl"
        if not path.exists():
            return
        blocked_by_gate: Counter[str] = Counter()
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                event = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if str(event.get("result", "")) == "blocked":
                blocked_by_gate[str(event.get("gate", "unknown"))] += 1

        for gate, count in blocked_by_gate.items():
            if count < 3:
                continue
            text = self._failure_history_text(gate)
            if not text:
                continue
            self._recommendations.append(
                Recommendation(
                    id=f"failure-history-{gate}",
                    category="testing" if gate in {"tests", "coverage_delta"} else "tool_usage",
                    text=text,
                    severity="important",
                    applies_to_roles=[],
                    source="failure_history",
                )
            )

    def _load_from_recommendations_file(self) -> None:
        """Load explicit recommendations from ``.sdd/recommendations.yaml``."""
        path = self._workdir / ".sdd" / "recommendations.yaml"
        if not path.exists():
            return
        raw_data: object = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(raw_data, dict):
            return
        raw_mapping = cast("dict[str, Any]", raw_data)
        raw_items = raw_mapping.get("recommendations", [])
        if not isinstance(raw_items, list):
            return
        raw_items_list = cast("list[object]", raw_items)
        for raw_item in raw_items_list:
            if not isinstance(raw_item, dict):
                continue
            item = cast("dict[str, Any]", raw_item)
            text = str(item.get("text", "")).strip()
            if not text:
                continue
            applies_to_raw = item.get("applies_to", item.get("applies_to_roles", []))
            applies_to = (
                [str(role) for role in cast("list[object]", applies_to_raw)] if isinstance(applies_to_raw, list) else []
            )
            self._recommendations.append(
                Recommendation(
                    id=str(item.get("id", self._make_id("yaml", text))),
                    category=str(item.get("category", self._category_for_text(text))),
                    text=text,
                    severity=str(item.get("severity", self._severity_for_text(text, default="important"))),
                    applies_to_roles=applies_to,
                    source="manager",
                )
            )

    def _dedupe(self, recommendations: list[Recommendation]) -> list[Recommendation]:
        """Deduplicate recommendations by normalized text."""
        deduped: dict[str, Recommendation] = {}
        for rec in recommendations:
            key = re.sub(r"\s+", " ", rec.text.strip().lower())
            current = deduped.get(key)
            if current is None or self._SEVERITY_ORDER.get(rec.severity, 3) < self._SEVERITY_ORDER.get(
                current.severity,
                3,
            ):
                deduped[key] = rec
        return list(deduped.values())

    def _make_id(self, prefix: str, text: str) -> str:
        """Generate a stable recommendation identifier."""
        slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
        return f"{prefix}-{slug[:48] or 'recommendation'}"

    def _severity_for_text(self, text: str, *, default: str = "suggestion") -> str:
        """Infer recommendation severity from imperative wording."""
        lowered = text.lower()
        if lowered.startswith("never") or lowered.startswith("do not") or "never " in lowered:
            return "critical"
        if lowered.startswith("always") or lowered.startswith("run ") or lowered.startswith("use "):
            return "important"
        return default

    def _category_for_text(self, text: str) -> str:
        """Infer a category from recommendation text."""
        lowered = text.lower()
        if any(token in lowered for token in ("pytest", "test", "coverage")):
            return "testing"
        if any(token in lowered for token in ("git", "branch", "commit", "merge")):
            return "git"
        if any(token in lowered for token in ("uv run", "python", "ruff", "pyright", "tool")):
            return "tool_usage"
        if any(token in lowered for token in ("perf", "latency", "slow", "cache")):
            return "performance"
        if any(token in lowered for token in ("never", "do not", "secret", "unsafe", "permission")):
            return "safety"
        return "coding"

    def _failure_history_text(self, gate: str) -> str:
        """Return a canned recommendation for repeated gate failures."""
        mapping = {
            "lint": "Recent lint failures suggest running `uv run ruff check` before completing the task.",
            "type_check": "Recent type-check failures suggest running `uv run pyright` before completing the task.",
            "tests": (
                "Recent test failures suggest running targeted `uv run pytest ... -x -q` before completing the task."
            ),
            "coverage_delta": (
                "Recent coverage regressions suggest running the coverage command locally before completion."
            ),
        }
        return mapping.get(gate, "")
