# Deep-research adapters

Two adapters run deep-research agents: agents that search, open and read a
large number of sources and write a report. Their unit of work is that report,
not a commit.

| Adapter | Agent | Runs |
|---|---|---|
| `gpt_researcher` | gpt-researcher, `deep` report mode | the installed `gpt_researcher` package |
| `tongyi_deepresearch` | Tongyi DeepResearch ReAct agent | an operator-provided checkout (`inference/`) |

## The run artifact

Each run writes three files to `.sdd/<agent>/<session>/` in the task's
working directory:

| File | Content |
|---|---|
| `report.md` | the report, as the agent produced it |
| `sources.json` | every distinct source URL the agent opened, sorted |
| `run.json` | agent, timing, terminal state, source count, and the sha256 of the two files above |

`run.json` binds the report to its source list. Reading a run back
(`bernstein.adapters.deep_research.load_research_run`) recomputes both
digests, so a report or source list edited after the run reads as
`tampered`, not `ok`. An empty report is `inconclusive`, and a run that
never wrote a record is `driver_failure`. The source count and wall time
come from the record, so two agents can be compared on the same question
from their artifacts alone.

## Models go through one gateway, one key per agent

Every model call goes to an OpenAI-compatible endpoint. Each agent has its
own key, so its usage can be limited and revoked on its own:

| Variable | Meaning |
|---|---|
| `BERNSTEIN_<AGENT>_OPENAI_BASE_URL` | the endpoint (required) |
| `BERNSTEIN_<AGENT>_OPENAI_API_KEY` | this agent's key (required) |
| `BERNSTEIN_<AGENT>_MODEL` | the model (required) |
| `BERNSTEIN_<AGENT>_PYTHON` | the interpreter the agent is installed in (default `python3`) |

`<AGENT>` is `GPT_RESEARCHER` or `TONGYI_DEEPRESEARCH`. A spawn with a
missing variable fails before anything starts, and the error names the
variables, never their values. One agent's key is never read for another.

gpt-researcher uses the model for all three of its roles (fast, smart,
strategic). `BERNSTEIN_GPT_RESEARCHER_EMBEDDING` names an embedding model on
the same endpoint. Its retriever settings (`RETRIEVER`, `TAVILY_API_KEY`,
...) and depth knobs (`DEEP_RESEARCH_BREADTH`, `DEEP_RESEARCH_DEPTH`, ...)
are passed through unchanged.

Tongyi DeepResearch also needs `BERNSTEIN_TONGYI_DEEPRESEARCH_HOME`, the
checkout. Its planner and its page summariser both use the endpoint
(`BERNSTEIN_TONGYI_DEEPRESEARCH_SUMMARY_MODEL` picks a different summary
model). Its own tool settings (`SERPER_KEY_ID`, `JINA_API_KEYS`,
`SANDBOX_FUSION_ENDPOINT`, `MAX_LLM_CALL_PER_RUN`) are passed through
unchanged.

Its search, scholar and visit tools call `google.serper.dev` and `r.jina.ai`
directly. Two variables send them elsewhere, for example to a gateway that
meters and logs web access:

| Variable | Effect |
|---|---|
| `BERNSTEIN_TONGYI_DEEPRESEARCH_SERPER_BASE_URL` | Serper requests go to this base, path appended (`https://gw/v1` → `https://gw/v1/search`, `/v1/scholar`) |
| `BERNSTEIN_TONGYI_DEEPRESEARCH_JINA_BASE_URL` | reader requests go to `<base>/<page url>` instead of `https://r.jina.ai/<page url>` |

Only the address changes: headers (`SERPER_KEY_ID` as `X-API-KEY`,
`JINA_API_KEYS` as a bearer token), request bodies and response parsing stay
the agent's own, so the endpoint must speak the Serper and Jina Reader
protocols. A value that is not an absolute http(s) URL fails the run with
exit 2.

## How the agent runs

The adapter launches a runner script shipped with bernstein
(`gpt_researcher_runner.py`, `tongyi_deepresearch_runner.py`) under the
agent's own interpreter. The runner imports nothing from bernstein, so the
agent's dependencies stay out of bernstein's environment and bernstein's
stay out of the agent's.

The Tongyi runner makes two changes to the agent at run time, without
editing the checkout:

- The agent is written for a local inference server. Its client is bound to
  the gateway endpoint and key instead.
- It counts context with the served model's local tokenizer. A gateway model
  has no local tokenizer, so the count is estimated at four characters per
  token, which keeps the agent's context-limit fallback working.
- With the tool base URLs above set, its search, scholar and visit tools are
  pointed at them.

Isolation is the spawner's concern, as for every adapter. A container
backend with the gVisor runtime (`--runtime runsc`) and outbound-only
networking fits these agents: they need the network to read sources, and
nothing on the host.

## Failure modes

| Symptom | Cause |
|---|---|
| `set BERNSTEIN_<AGENT>_...` at spawn | a gateway variable or the checkout path is unset |
| runner exit 2 | the agent is not installed in `BERNSTEIN_<AGENT>_PYTHON`, the checkout has no `inference/react_agent.py`, or a Tongyi tool base URL is not http(s) |
| state `driver_failure`, `detail` set | the agent raised; `detail` holds the exception |
| state `inconclusive` | empty report, or Tongyi stopped without an answer (`detail` says why) |
| state `tampered` | `report.md` or `sources.json` changed after the run |
