## `trace export` journals the model namespace when no provider resolves

An endpoint-routed worker journaled `model_provider` as `null`, so `bernstein
trace export` refused the run: the record requires a provider and the exporter
will not invent one. The spawn journal now derives the provider from the
operator-configured model identifier when it carries a namespace - for
`omnilab/fleet-hard` it journals `model_provider` as `omnilab` and keeps
`model_id` as `omnilab/fleet-hard`. A bare identifier or an empty namespace is
not a provider, and a resolved provider always wins, so a run that cannot say
who served the model still refuses rather than gaining a made-up vendor name.
