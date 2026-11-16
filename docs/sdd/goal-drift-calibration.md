# Recorded intent calibration boundary

Goal-drift trajectory payloads can include existing calibration-log records under
`intent_calibration_records`. Parsing uses `CalibrationRecord` validation and
retains only consumed fields (`ts`, kind, policy identifier, probability, outcome).
The receipt carries that projection; scoring reconstructs the report from it.
Arbitrary metadata and decision IDs do not enter this new receipt field.

The additive curve section uses a unique nonempty `policy_path` as the recorded
version identifier and existing `compute_report` output as quality evidence. It
adds no fitting, evaluator invocation, current-log access or wall-clock reads.
Zero/missing/malformed/mixed-version evidence is unavailable. Soft metadata never
participates in hard drift measurements or threshold decisions.
