// Shared types for the Artifacts panel (#2553). Mirror the response models
// served by ``GET /api/v1/tasks/{task_id}/artifacts`` (a list of
// ``TaskArtifactContentResponse``) and ``GET /api/v1/tasks/{task_id}/progress``
// (``TaskProgressResponse``). Every artifact is a content-addressed, journal-
// anchored record; ``verified`` reflects re-checking the stored blob hash
// against the journal row, so a tampered blob renders as tampered, not content.

export type ArtifactType = 'report' | 'table' | 'link';

/** Decoded content of a posted artifact, discriminated by ``type``. */
export interface ReportContent {
  type: 'report';
  body: string;
}

export interface TableContent {
  type: 'table';
  columns: string[];
  rows: string[][];
}

export interface LinkContent {
  type: 'link';
  url: string;
  kind: string;
}

export type ArtifactContent = ReportContent | TableContent | LinkContent;

/** One posted artifact version with its chain anchors and verification state. */
export interface ArtifactView {
  task_id: string;
  key: string;
  artifact_type: ArtifactType;
  content_hash: string;
  version: number;
  prev_version_hash: string;
  spine_entry_hash: string;
  journal_index: number;
  journal_event_hash: string;
  link_kind: string;
  size: number;
  verified: boolean;
  verify_reason: string;
  content: ArtifactContent | null;
}

/** The chain-computed progress vector: a projection of journaled work. */
export interface ProgressVector {
  task_id: string;
  schema_version: number;
  checkpoints: number;
  diffs_captured: number;
  gate_attempts: number;
  evidence_declared: number;
  evidence_passed: number;
  ledger_phase: string;
  ledger_attempts: number;
  terminal: boolean;
  earned_steps: number;
  phase_ordinal: number;
  vector_hash: string;
}
