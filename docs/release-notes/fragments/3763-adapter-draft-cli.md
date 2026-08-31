## Drafted adapter profiles get an operator confirmation and a saved record

`bernstein adapters draft` runs a real probe against an installed CLI, drafts a candidate invocation profile from its `--help` capture, and shows the operator the exact argv the draft would invoke before anything is written. A confirmed draft persists as plain YAML - the invocation, a contract preview, and the evidence byte range each field traces back to - so a later step can read back what was accepted (#3763).
