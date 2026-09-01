"""GPT Researcher synthesiser adapter for the research modality (issue #2524).

This module provides a synthesiser callable that drives the upstream GPT
Researcher runtime (https://github.com/assafelovic/gpt-researcher, Apache-2.0)
as a plug-in for the research worker's ``synthesise`` injection point.

The adapter ensures that the upstream runtime never performs its own retrieval:
all fetching happens through the worker's :class:`ResearchActivity` and the
worker passes only the fetched, content-addressed pages to the synthesiser. This
keeps the deterministic contract of :mod:`bernstein.core.orchestration.research_worker`
and ensures that the origin of every piece of evidence is verifiable against
the content store.

The synthesiser callable's signature matches what :class:`ResearchWorker`
expects::

    Callable[[str, tuple[FetchedSource, ...]], Sequence[ClaimDraft]]

where the first argument is the research ``query`` and the second is the
ordered tuple of :class:`FetchedSource` objects the worker fetched.

An :class:`UnavailableError` named after the package (``GptResearcherUnavailableError``)
is raised when the ``gpt-researcher`` package is not installed, following the
pattern used by :class:`~bernstein.core.orchestration.browser_driver.BrowserDriverUnavailable`.

The subprocess environment is filtered through
:meth:`bernstein.adapters.env_isolation.build_filtered_env` to allow only the
provider environment variables this integration needs (e.g. ``OPENAI_API_KEY`` for
GPT Researcher's default backend). No unrelated env keys leak.
"""

from __future__ import annotations

import json
import subprocess
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence
    from typing import Any

    from bernstein.core.orchestration.research_worker import (
        ClaimDraft,
        FetchedSource,
        SpanRef,
    )

__all__ = [
    "GptResearcherSynthesiser",
    "GptResearcherUnavailableError",
]


class GptResearcherUnavailableError(RuntimeError):
    """Raised when ``gpt-researcher`` is not installed.

    This error follows the pattern used by
    :class:`~bernstein.core.orchestration.browser_driver.BrowserDriverUnavailable`
    and names the exact package to install (``gpt-researcher``).

    The error is raised at import time for this module, so attempting to use
    the synthesiser when the dependency is missing results in a clear error
    message.
    """

    def __init__(self) -> None:
        super().__init__(
            "GPT Researcher synthesiser requires the optional extra "
            "'gpt-researcher' (package 'gpt-researcher'); "
            "install it and retry"
        )


class GptResearcherSynthesiser:
    """Synthesiser adapter that drives GPT Researcher as a subprocess.

    The synthesiser operates in server mode, consuming the research query and
    fetched sources from standard input as JSON, then writing the synthesized
    research report as JSON to standard output. This approach avoids the
    upstream runtime's own retrieval logic entirely, respecting the contract
    enforced by :class:`~bernstein.core.orchestration.research_worker.ResearchWorker`.

    The subprocess environment is filtered using the orchestrator's
    environment isolation system, restricting the subprocess to only the
    environment variables it needs (provider API keys).

    Args:
        extra_keys: Additional environment variable names to include in the
            subprocess environment (defaults to ``["OPENAI_API_KEY"]``). This
            allows overriding the default provider.
    """

    def __init__(self, *, extra_keys: list[str] | None = None) -> None:
        # Lazy import to allow tests to run without the optional dependency.
        try:
            import gpt_researcher  # type: ignore[import-not-found]  # noqa: F401
        except ImportError:
            raise GptResearcherUnavailableError() from None

        from bernstein.adapters.env_isolation import build_filtered_env

        self._extra_keys = extra_keys if extra_keys is not None else ["OPENAI_API_KEY"]
        self._build_env = build_filtered_env(extra_keys=self._extra_keys)

    def _run_synthesiser_sync(
        self,
        query: str,
        fetched: tuple[FetchedSource, ...],
    ) -> Sequence[ClaimDraft]:
        """Execute GPT Researcher as a subprocess to synthesize claims.

        This synchronous method prepares a JSON payload containing the query and
        fetched sources (source_ref, content), invokes the GPT Researcher server,
        and parses the response into ``ClaimDraft`` objects. This is the
        synchronous version expected by :class:`~bernstein.core.orchestration.research_worker.ResearchWorker`.

        Args:
            query: The research question.
            fetched: The sources already fetched by the research worker.

        Returns:
            A sequence of ``ClaimDraft`` objects synthesized by GPT Researcher.
        """
        # Prepare the input JSON that GPT Researcher expects via stdin.
        input_data = {
            "query": query,
            "sources": [
                {
                    "source_ref": src.source_ref,
                    "content_hash": src.content_hash,
                    "content": src.content.decode("utf-8", errors="replace"),
                }
                for src in fetched
            ],
        }

        # Launch GPT Researcher server process.
        process = subprocess.Popen(
            ["python", "-m", "gpt_researcher.server", "--stdin"],
            env=self._build_env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        # Communicate with the process and wait for output.
        stdout_bytes, stderr_bytes = process.communicate(json.dumps(input_data).encode("utf-8"))
        if process.returncode != 0:
            stderr = stderr_bytes.decode("utf-8", errors="replace")
            raise RuntimeError(f"GPT Researcher synthesiser failed (exit {process.returncode}): {stderr}")

        report = json.loads(stdout_bytes.decode("utf-8", errors="replace"))
        return self._parse_report_to_claims(report)

    @staticmethod
    def _parse_report_to_claims(report: dict[str, Any]) -> Sequence[ClaimDraft]:
        """Convert GPT Researcher's JSON report into ``ClaimDraft`` objects."""
        from bernstein.core.orchestration.research_worker import ClaimDraft, SpanRef

        claim_drafts: list[ClaimDraft] = []

        for idx, claim_data in enumerate(report.get("claims", [])):
            statement = claim_data.get("claim", "")
            spans: list[SpanRef] = []

            for citation in claim_data.get("supports", []):
                quote = citation.get("content", "")
                source_ref = citation.get("source", "")
                spans.append(SpanRef(source_ref=source_ref, quote=quote))

            claim_drafts.append(ClaimDraft(statement=statement, spans=tuple(spans), claim_id=f"c{idx + 1}"))

        return claim_drafts

    def __call__(
        self,
        query: str,
        fetched: tuple[FetchedSource, ...],
    ) -> Sequence[ClaimDraft]:
        """Synthesiser callable matching :class:`~bernstein.core.orchestration.research_worker.ResearchWorker`.

        This method is the entry point used by the research worker when it calls
        its injected ``synthesise`` function.

        Args:
            query: The research question.
            fetched: The sources already fetched by the research worker.

        Returns:
            A sequence of ``ClaimDraft`` objects.
        """
        return self._run_synthesiser_sync(query, fetched)
