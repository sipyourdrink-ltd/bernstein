## Gate-evasion corpus and benchmark suite (`gate-evasion-v1`)

Every way an agent change previously fooled or evaded a quality gate
is now captured as an offline test fixture under
`src/bernstein/eval/cases/gate_evasion/<class>/`. The new `gate-evasion-v1`
benchmark suite dynamically discovers all evasion cases from their
`manifest.json` definitions, evaluates changes against assigned quality
gates, tracks catch rate, reports missed classes and responsible gates,
and produces verifiable, signed `SubmissionBundle` artifacts (#5448).
