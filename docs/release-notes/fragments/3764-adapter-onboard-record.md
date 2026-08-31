## Onboarded adapters can record and replay a supervised transcript

Adapter onboarding now records a successful smoke invocation as a replayable golden transcript and checks three deterministic held-out invocations against the same CLI. Recorded profiles preserve their exact invocation tokens, while failed smoke or held-out runs remain explicit failures rather than being silently skipped (#3764).
