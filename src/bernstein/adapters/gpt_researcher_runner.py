"""Standalone runner for gpt-researcher, launched by path.

Runs inside the interpreter gpt-researcher is installed in; bernstein is not
importable there, so this imports only the standard library, gpt-researcher,
and :mod:`deep_research_artifact` (loaded by file path). The adapter
(:mod:`bernstein.adapters.gpt_researcher`) sets the gateway environment.

Exit codes: 0 ok, 3 inconclusive (empty report), 1 the agent failed,
2 gpt-researcher is not installed in this interpreter.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import sys
import time
from pathlib import Path
from typing import Any

AGENT = "gpt-researcher"


def _isolate_and_load_artifact() -> Any:
    """Drop this directory from ``sys.path``; load the artifact module by path.

    Python puts a script's own directory first on ``sys.path``. Here that is
    bernstein's adapter directory, which holds a module named after the agent
    package (``gpt_researcher.py``); left in place, ``import gpt_researcher``
    would load bernstein's adapter instead of the agent.
    """
    here = Path(__file__).resolve().parent
    sys.path[:] = [p for p in sys.path if Path(p or ".").resolve() != here]
    spec = importlib.util.spec_from_file_location("deep_research_artifact", here / "deep_research_artifact.py")
    if spec is None or spec.loader is None:
        raise ImportError(f"deep_research_artifact.py missing beside {__file__}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _research(query: str, report_type: str) -> tuple[str, list[str]]:
    from gpt_researcher import GPTResearcher  # type: ignore[import-not-found]

    researcher = GPTResearcher(query=query, report_type=report_type)
    await researcher.conduct_research()
    report = await researcher.write_report()
    urls = set(researcher.get_source_urls() or ())
    urls.update(getattr(researcher, "visited_urls", ()) or ())
    return str(report or ""), sorted(urls)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prompt-file", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--report-type", default="deep")
    args = parser.parse_args(argv)

    artifact = _isolate_and_load_artifact()

    try:
        import gpt_researcher  # noqa: F401
    except ImportError as exc:
        print(f"gpt-researcher is not installed in {sys.executable}: {exc}", file=sys.stderr)
        return 2

    query = args.prompt_file.read_text(encoding="utf-8")
    started = time.time()
    try:
        report, sources = asyncio.run(_research(query, args.report_type))
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
    record = artifact.write_run(
        args.out_dir,
        agent=AGENT,
        report=report,
        sources=sources,
        started_at=started,
        finished_at=time.time(),
        state=artifact.STATE_OK,
    )
    print(f"{AGENT}: {record['state']}, {record['sources_count']} sources, {record['wall_seconds']} s")
    return 0 if record["state"] == artifact.STATE_OK else 3


if __name__ == "__main__":
    raise SystemExit(main())
