## Deep-research adapters honour the network egress policy

`gpt_researcher` and `tongyi_deepresearch` declared no network endpoints, so the
egress check every other adapter runs before spawning was a no-op for them.
Under `--profile airgap` or `--allow-network none` they still launched a runner
whose whole job is calling an LLM gateway and fetching web pages.

A spawn now derives its destinations from the environment it is about to hand
the runner and checks them against the active policy before anything is
written or started: the gateway, the retrievers (gpt-researcher) or the Serper,
Jina and code-sandbox endpoints (Tongyi). A denied destination, a URL without
a readable host, or a retriever or scraper bernstein cannot place refuses the
spawn. The unrestricted default is unchanged. A host allow-list bounds these
services, not the pages the agent fetches from search results; see
`docs/adapters/deep-research.md`.

The spawn also reads its gateway and tool configuration before creating the run
directory, so a configuration error no longer leaves a prompt file behind.

## Deep-research adapters no longer declare the `artifact` output mode

Their runners leave the report in `.sdd/<agent>/<session>/`. Nothing copies it
to a task's declared artifact path, and the artifact-mode workspace is removed
at reap, so a task declaring an artifact kind on these adapters failed
verification and was retried. The two rows now keep the default `git-diff`
mode; publishing the report through the artifact-completion path is not done
here.

## Tongyi runner keeps a finished answer when a tool call is malformed

`visited_urls` raised on a tool call that parsed but had the wrong shape (string
or list `arguments`, a non-list `url`), which discarded an answer the agent had
already produced. Such calls now contribute no sources and the run is recorded.
