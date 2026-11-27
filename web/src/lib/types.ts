// Mirrors contracts/models.py and api/schemas.py. Keep in sync when contracts change.

export type FlagValue = boolean | number | string;

export type Domain = "opspilot" | "vaultguard";

export interface FlagConfig {
  values: Record<string, FlagValue>;
  source: "captured" | "baseline" | "override";
  segment_overrides: Record<string, Record<string, FlagValue>>;
}

export interface Identity {
  identifier: string;
  traits: Record<string, FlagValue>;
}

export interface FlagChange {
  flag: string;
  old: FlagValue | null;
  new: FlagValue | null;
}

export type TestOutcome = "PASS" | "FAIL" | "INCONCLUSIVE";
export type TestPhase =
  | "reproduce"
  | "search"
  | "escalate"
  | "confirm"
  | "validate"
  | "post_write";

export interface TestResult {
  phase: TestPhase;
  config: FlagConfig;
  changes: FlagChange[];
  runs: number;
  failures: number;
  outcome: TestOutcome;
  trace_ids: string[];
  fresh: boolean;
  /** Distinct behaviours among the runs; 1 means every run was identical. */
  distinct_behaviours?: number | null;
}

export type WriteTool =
  | "create_segment_override"
  | "update_feature_state"
  | "delete_feature_segment";

export interface WriteOp {
  tool: WriteTool;
  project: string;
  environment: string;
  feature: string;
  segment: string | null;
  value: FlagValue | null;
  enabled: boolean | null;
}

export type RepairKind = "segment_override" | "surgical" | "global";

export interface RepairCandidate {
  kind: RepairKind;
  ops: WriteOp[];
  validation: TestResult | null;
  heldout_passes: number | null;
  heldout_runs: number | null;
  baseline_heldout_passes: number | null;
  cost_kept: number | null;
  violations: number | null;
}

export type InvestigationStatus =
  | "capturing"
  | "reproducing"
  | "searching"
  | "validating"
  | "proposed"
  | "approved"
  | "applied"
  | "verified"
  | "stale"
  | "expired"
  | "failed"
  | "rejected"
  | "inconclusive"
  | "not_reproducible";

export interface Timestamps {
  alert: string;
  first_suspect: string | null;
  proposal: string | null;
  approved: string | null;
  verified: string | null;
}

export interface Investigation {
  id: string;
  incident_id: string;
  domain: Domain;
  mode: "guided" | "plain";
  status: InvestigationStatus;
  segment: string | null;
  suspects: FlagChange[];
  steps: TestResult[];
  culprit_set: FlagChange[];
  candidates: RepairCandidate[];
  timestamps: Timestamps;
  runs_used: number;
  usd_used: number;
  budget_runs: number;
  budget_usd: number;
  proposal_id: string | null;
}

export type ProposalState =
  | "proposed"
  | "approved"
  | "applied"
  | "verified"
  | "stale"
  | "expired"
  | "failed"
  | "rejected";

export interface ReadBack {
  identity: Identity;
  values: Record<string, FlagValue>;
  from_default_handler: boolean;
}

export interface Proposal {
  id: string;
  investigation_id: string;
  candidate: RepairCandidate;
  payload_hash: string;
  fingerprint: string;
  explanation: string;
  state: ProposalState;
  created_at: string;
  expires_at: string;
  readback: ReadBack[];
}

/** One MCP call exactly as the gate will send it to Flagsmith. */
export interface McpCall {
  tool: string;
  arguments: Record<string, unknown>;
}

export interface InvestigationView {
  investigation: Investigation;
  proposal: Proposal | null;
  calls: McpCall[];
  rollback_of: string | null;
  active_ms: number;
}

export interface AuditEntry {
  ts: string;
  actor: string;
  action: "read" | "write" | "refused" | "approve" | "reject";
  tool: string | null;
  args: Record<string, unknown>;
  result: string | null;
  proposal_id: string | null;
}

export interface SessionResponse {
  token: string;
  approver: string;
  expires_at: string;
}
