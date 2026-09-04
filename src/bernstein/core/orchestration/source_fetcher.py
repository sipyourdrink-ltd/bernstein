"""Fetcher interface for research activities: a `fetch(ref) -> bytes` verb (#3112).

:meth:`~bernstein.core.orchestration.research_worker.ResearchWorker.run` takes
``fetch_fn: Callable[[str], bytes]`` and documents it as "the only place the run
touches the outside world" -- every blob it returns is content-addressed at fetch
time. Until this module, no concrete implementation shipped: the only callers in
the tree were tests passing a raw dict-lookup lambda.

This mirrors the shape :class:`~bernstein.core.orchestration.browser_driver.BrowserDriver`
already established for the browser modality: a small protocol the worker never
imports a concrete implementation of, plus a recorded implementation that serves
fixed bytes with no network, so a second fetcher is a new implementation and not
a change to the worker, and the whole research determinism suite can run offline
against a fixed corpus.

Note: :mod:`bernstein.core.devops.trend_scan` already defines an unrelated
``SourceFetcher`` type alias over its own ``SourceSpec``/``RawItem`` types. That
name is a scan-pipeline callable shape with no relation to this protocol; import
this one by module path (``from bernstein.core.orchestration.source_fetcher import
SourceFetcher``) rather than assuming a bare ``SourceFetcher`` import resolves
here.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

__all__ = [
    "RecordedSourceFetcher",
    "SourceFetcher",
    "SourceNotRecorded",
]


class SourceNotRecorded(KeyError):
    """A :class:`RecordedSourceFetcher` was asked to fetch a ref outside its corpus.

    Raised instead of a bare ``KeyError`` so the refusal is typed and names the
    ref that was missing, the way :class:`~bernstein.core.orchestration.browser_driver.BrowserDriverError`
    names what a browser driver could not do.
    """

    def __init__(self, source_ref: str) -> None:
        self.source_ref = source_ref
        super().__init__(f"source {source_ref!r} is not in the recorded corpus")


@runtime_checkable
class SourceFetcher(Protocol):
    """The one-verb surface a research activity fetches sources through.

    Deliberately a single method: :meth:`~bernstein.core.orchestration.research_worker.ResearchWorker.run`
    only ever needs the exact bytes behind a source reference, and every fetcher
    implementation (recorded corpus, live stdlib, a future backend) binds to this
    one verb so the worker never depends on which one is in use.
    """

    def fetch(self, source_ref: str) -> bytes:
        """Return the exact bytes behind *source_ref*.

        Args:
            source_ref: The source reference (URL / label) to fetch.

        Returns:
            The exact bytes fetched, before any decoding or normalisation --
            these are the bytes the worker content-addresses.
        """


class RecordedSourceFetcher:
    """A fetcher over a fixed ``{source_ref: bytes}`` corpus, no network involved.

    This is what makes the research determinism suite runnable offline: a run
    driven against a fixed corpus is a pure function of the query and the corpus,
    so "the same query over the same recorded corpus produces a byte-identical
    report" is a testable property rather than a claim. It is also the offline
    replay path -- a corpus recorded once from a live fetcher replays with no
    network access.

    Args:
        corpus: The fixed ``{source_ref: bytes}`` mapping served. Copied on
            construction so a caller mutating their own dict afterwards cannot
            change what the fetcher serves.
    """

    def __init__(self, corpus: dict[str, bytes]) -> None:
        self._corpus = dict(corpus)

    def fetch(self, source_ref: str) -> bytes:
        """Return the recorded bytes for *source_ref*.

        Raises:
            SourceNotRecorded: When *source_ref* is not in the corpus.
        """
        try:
            return self._corpus[source_ref]
        except KeyError:
            raise SourceNotRecorded(source_ref) from None
