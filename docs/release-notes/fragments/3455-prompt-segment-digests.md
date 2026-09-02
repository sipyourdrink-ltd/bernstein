## `bernstein context segment-prompt` digests the prompt blocks the orchestrator authors

The text an agent acts on is assembled from four blocks the orchestrator writes
itself — role instructions, the task brief, the coordination-mailbox section and
the resume-state prefix — and none of them were content-addressed. Prompt
assembly now digests each block into a named `sha256:` segment plus one digest
over the ordered segment list, so a run that diverges can name *which* block
changed instead of only that assembly produced different bytes; an empty block
still records a segment, so the segment count never depends on which sections
happened to render. A new offline subcommand, `bernstein context segment-prompt`
(with `--role-file`, `--task-file`, `--mailbox-file`, `--resume-file` and
`--json`), reads the blocks from files and prints their digests. Nothing is
anchored in the run record, journal or audit chain yet (#3455).
