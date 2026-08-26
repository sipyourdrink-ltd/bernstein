A task can now declare a `blob` artifact output: the canonical form is the raw
bytes and the digest is their content hash, so a deliverable that is not text,
JSONL or a JSON object can still be anchored by a signed receipt. The
text-shaped acceptance criteria (`schema_valid`, `criteria_match`) are rejected
at parse time for this kind, naming the criterion.

The artifact-kind lists behind the plan schema and `bernstein task add
--artifact-kind` are now derived from the kind enum instead of copied, so a new
kind reaches every declaration surface at once.
