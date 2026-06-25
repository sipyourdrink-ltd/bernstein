"""Deterministic opt-in skill auto-routing."""

from __future__ import annotations

import math
import os
import re
from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, cast

import yaml

from bernstein.core.defaults import (
    SKILLS_AUTO_ROUTE_DEFAULT_LIMIT as DEFAULT_ROUTE_LIMIT,
)
from bernstein.core.defaults import (
    SKILLS_AUTO_ROUTE_ENV as ENV_AUTO_ROUTE,
)

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping, Sequence
    from pathlib import Path

_ENABLE_TOKENS = frozenset({"1", "true", "yes", "on"})
_TOKEN_RE: re.Pattern[str] = re.compile(r"[a-z0-9][a-z0-9-]*")


class RoutableTask(Protocol):
    """Task fields used by deterministic skill auto-routing."""

    title: str
    description: str
    owned_files: list[str]
    requires: list[str]


@dataclass(frozen=True)
class SkillRouteCandidate:
    """One deterministic skill routing candidate."""

    template_name: str
    skill_name: str
    score: float


def auto_route_enabled(env: Mapping[str, str] | None = None) -> bool:
    """Return whether opt-in auto-routing is enabled."""
    values = env if env is not None else os.environ
    return values.get(ENV_AUTO_ROUTE, "").strip().lower() in _ENABLE_TOKENS


def select_auto_route_templates(
    skills_source_dir: Path,
    tasks: Sequence[RoutableTask],
    *,
    excluded_templates: Collection[str],
    limit: int = DEFAULT_ROUTE_LIMIT,
) -> tuple[SkillRouteCandidate, ...]:
    """Select extra skill templates using deterministic TF-IDF scoring."""
    if limit < 1 or not tasks or not skills_source_dir.is_dir():
        return ()
    query = _task_query(tasks)
    query_counts = _token_counts(query)
    if not query_counts:
        return ()

    documents = _load_skill_documents(skills_source_dir, excluded_templates=excluded_templates)
    if not documents:
        return ()

    doc_freq: Counter[str] = Counter()
    for document in documents:
        doc_freq.update(document.tokens.keys())

    query_weight = _tfidf_vector(query_counts, doc_freq=doc_freq, corpus_size=len(documents))
    scored: list[SkillRouteCandidate] = []
    for document in documents:
        doc_weight = _tfidf_vector(document.tokens, doc_freq=doc_freq, corpus_size=len(documents))
        score = _cosine(query_weight, doc_weight)
        if score > 0:
            scored.append(
                SkillRouteCandidate(
                    template_name=document.template_name,
                    skill_name=document.skill_name,
                    score=score,
                )
            )

    scored.sort(key=lambda item: (-item.score, item.template_name))
    return tuple(scored[:limit])


@dataclass(frozen=True)
class _SkillDocument:
    template_name: str
    skill_name: str
    tokens: Counter[str]


def _load_skill_documents(
    skills_source_dir: Path,
    *,
    excluded_templates: Collection[str],
) -> tuple[_SkillDocument, ...]:
    documents: list[_SkillDocument] = []
    for path in sorted(skills_source_dir.glob("*.md")):
        if path.name in excluded_templates:
            continue
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            continue
        frontmatter, body = _split_frontmatter(raw)
        skill_name = _frontmatter_name(frontmatter) or path.stem
        parts = [
            path.stem.replace("-", " "),
            skill_name.replace("-", " "),
            _frontmatter_text(frontmatter),
            body,
        ]
        tokens = _token_counts("\n".join(parts))
        if tokens:
            documents.append(_SkillDocument(template_name=path.name, skill_name=skill_name, tokens=tokens))
    return tuple(documents)


def _task_query(tasks: Sequence[RoutableTask]) -> str:
    parts: list[str] = []
    for task in tasks:
        parts.extend([task.title, task.description])
        parts.extend(task.owned_files)
        parts.extend(task.requires)
    return "\n".join(part for part in parts if part)


def _token_counts(text: str) -> Counter[str]:
    return Counter(_TOKEN_RE.findall(text.casefold()))


def _tfidf_vector(counts: Counter[str], *, doc_freq: Counter[str], corpus_size: int) -> dict[str, float]:
    total = sum(counts.values())
    if total <= 0:
        return {}
    vector: dict[str, float] = {}
    for token, count in counts.items():
        df = doc_freq.get(token, 0)
        idf = math.log((1 + corpus_size) / (1 + df)) + 1.0
        vector[token] = (count / total) * idf
    return vector


def _cosine(left: dict[str, float], right: dict[str, float]) -> float:
    if not left or not right:
        return 0.0
    numerator = sum(weight * right.get(token, 0.0) for token, weight in left.items())
    if numerator <= 0:
        return 0.0
    left_norm = math.sqrt(sum(weight * weight for weight in left.values()))
    right_norm = math.sqrt(sum(weight * weight for weight in right.values()))
    if 0 in (left_norm, right_norm):
        return 0.0
    return numerator / (left_norm * right_norm)


def _split_frontmatter(raw: str) -> tuple[dict[str, object], str]:
    lines = raw.splitlines()
    if not lines or lines[0].rstrip() != "---":
        return ({}, raw)
    front: list[str] = []
    for idx in range(1, len(lines)):
        if lines[idx].rstrip() == "---":
            try:
                loaded_obj: object = yaml.safe_load("\n".join(front)) if front else {}
            except yaml.YAMLError:
                return ({}, "\n".join(lines[idx + 1 :]))
            if not isinstance(loaded_obj, dict):
                return ({}, "\n".join(lines[idx + 1 :]))
            typed: dict[str, object] = {}
            for key, value in cast("dict[object, object]", loaded_obj).items():
                if isinstance(key, str):
                    typed[key] = value
            return (typed, "\n".join(lines[idx + 1 :]))
        front.append(lines[idx])
    return ({}, raw)


def _frontmatter_name(frontmatter: dict[str, object]) -> str | None:
    value = frontmatter.get("name")
    return value if isinstance(value, str) and value else None


def _frontmatter_text(frontmatter: dict[str, object]) -> str:
    parts: list[str] = []
    for key in ("name", "description"):
        value = frontmatter.get(key)
        if isinstance(value, str):
            parts.append(value)
    keywords = frontmatter.get("trigger_keywords")
    if isinstance(keywords, list):
        keyword_items = cast("list[object]", keywords)
        parts.extend(item for item in keyword_items if isinstance(item, str))
    return "\n".join(parts)


__all__ = [
    "DEFAULT_ROUTE_LIMIT",
    "ENV_AUTO_ROUTE",
    "SkillRouteCandidate",
    "auto_route_enabled",
    "select_auto_route_templates",
]
