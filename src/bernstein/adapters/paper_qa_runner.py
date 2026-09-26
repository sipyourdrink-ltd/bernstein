"""Standalone runner for PaperQA2, launched by path.

Runs inside the interpreter PaperQA2 is installed in; bernstein is not
importable there, so this imports only the standard library, PaperQA2, and
:mod:`deep_research_artifact` (loaded by file path). The adapter
(:mod:`bernstein.adapters.paper_qa`) sets the gateway environment.

Every model is named ``openai/<model>``, so LiteLLM sends each one to the
gateway in ``OPENAI_API_BASE`` with the key in ``OPENAI_API_KEY``: the answer,
summary and agent models, the parser's enrichment model, and the embedding
model. Paper metadata lookup (Crossref, Semantic Scholar) is off, so the
papers directory and the gateway are the run's only inputs.

The sources recorded are the papers the answer cites, one entry per paper:
its URL or DOI link when PaperQA2 has one, else its citation.

Exit codes: 0 ok, 3 inconclusive (no answer, or PaperQA2 was unsure or cut
short), 1 the agent failed, 2 PaperQA2 is not installed in this interpreter
or the papers directory is missing.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import sys
import time
from pathlib import Path
from typing import Any

AGENT = "paper-qa"
_ANSWERED = "success"
_FAILED = "fail"


def _isolate_and_load_artifact() -> Any:
    """Drop this directory from ``sys.path``; load the artifact module by path.

    Python puts a script's own directory first on ``sys.path``. Here that is
    bernstein's adapter directory; left in place, its modules could shadow the
    agent's imports.
    """
    here = Path(__file__).resolve().parent
    sys.path[:] = [p for p in sys.path if Path(p or ".").resolve() != here]
    spec = importlib.util.spec_from_file_location("deep_research_artifact", here / "deep_research_artifact.py")
    if spec is None or spec.loader is None:
        raise ImportError(f"deep_research_artifact.py missing beside {__file__}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def build_settings(paperqa: Any, papers_dir: Path, model: str, embedding: str) -> Any:
    """PaperQA2 settings with every model on the gateway and metadata lookup off."""
    llm = f"openai/{model}"
    return paperqa.Settings(
        llm=llm,
        summary_llm=llm,
        embedding=f"openai/{embedding}",
        agent={"agent_llm": llm, "index": {"paper_directory": str(papers_dir)}},
        parsing={"use_doc_details": False, "enrichment_llm": llm},
    )


def cited_sources(session: Any) -> list[str]:
    """One entry per paper the answer cites: URL, DOI link, else citation."""
    used = set(getattr(session, "used_contexts", ()) or ())
    sources: set[str] = set()
    for context in getattr(session, "contexts", ()) or ():
        if getattr(context, "id", None) not in used:
            continue
        doc = context.text.doc
        for ref in (getattr(doc, "url", None), getattr(doc, "doi_url", None), getattr(doc, "citation", None)):
            if isinstance(ref, str) and ref.strip():
                sources.add(ref.strip())
                break
    return sorted(sources)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prompt-file", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--papers-dir", type=Path, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--embedding", required=True)
    args = parser.parse_args(argv)

    artifact = _isolate_and_load_artifact()

    if not args.papers_dir.is_dir():
        print(f"no papers directory at {args.papers_dir}", file=sys.stderr)
        return 2
    try:
        import paperqa  # type: ignore[import-not-found]
    except ImportError as exc:
        print(f"PaperQA2 is not installed in {sys.executable}: {exc}", file=sys.stderr)
        return 2

    query = args.prompt_file.read_text(encoding="utf-8")
    started = time.time()
    try:
        settings = build_settings(paperqa, args.papers_dir.resolve(), args.model, args.embedding)
        response = asyncio.run(paperqa.agent_query(query, settings, agent_type=settings.agent.agent_type))
        status = str(response.status)
        if status == _FAILED:
            raise RuntimeError("PaperQA2 reported status 'fail'")
    except Exception as exc:  # the agent's own failure, recorded not raised
        artifact.write_run(
            args.out_dir,
            agent=AGENT,
            report="",
            sources=(),
            started_at=started,
            finished_at=time.time(),
            state=artifact.STATE_DRIVER_FAILURE,
            detail=f"{exc.__class__.__name__}: {exc}"[:500],
        )
        return 1

    session = response.session
    answered = status == _ANSWERED and bool(str(session.answer or "").strip())
    record = artifact.write_run(
        args.out_dir,
        agent=AGENT,
        report=str(session.formatted_answer or "") if answered else "",
        sources=cited_sources(session),
        started_at=started,
        finished_at=time.time(),
        state=artifact.STATE_OK if answered else artifact.STATE_INCONCLUSIVE,
        detail="" if answered else f"PaperQA2 status: {status}",
    )
    print(f"{AGENT}: {record['state']}, {record['sources_count']} sources, {record['wall_seconds']} s")
    return 0 if record["state"] == artifact.STATE_OK else 3


if __name__ == "__main__":
    raise SystemExit(main())
