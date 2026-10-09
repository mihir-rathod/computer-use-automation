export type Role = "viewer" | "operator" | "supervisor" | "admin";
export const ROLE_RANK: Record<Role, number> = { viewer: 0, operator: 1, supervisor: 2, admin: 3 };
export const atLeast = (role: Role | undefined, min: Role) => !!role && ROLE_RANK[role] >= ROLE_RANK[min];

export interface Me { name: string; role: Role }

export interface SchemaProp { type?: string | string[]; enum?: string[]; pattern?: string; description?: string; minLength?: number; maxLength?: number; minimum?: number; maximum?: number }
export interface InputSchema { properties: Record<string, SchemaProp>; required: string[]; at_least_one_of?: string[] }

export interface Capability {
  capability_id: string; name: string; description: string; version: string; versions: string[]; app: string;
  risk: { level: "read_only" | "state_changing"; has_irreversible_step: boolean; irreversible_steps: string[]; approval_required: string | null; approve_at: "run" | "step" | null; caps: { max_param: Record<string, number>; max_commits_per_day: number | null } | null };
  login: string | null; reviewed: boolean; has_canary: boolean;
  last_canary: { ok: boolean; at: string; detail: string } | null;
  input_schema: InputSchema; output_schema: { properties: Record<string, SchemaProp>; required: string[] };
  provenance?: { discovered_by: string; reviewed: boolean; note?: string; approved_by?: string; change_note?: string; parent_version?: string; commit_approvals: { step_id: string; approver: string; mode: string; at: string }[] };
  history?: { action: string; version: string; from: string | null; at: string; by: string; reason: string }[];
  steps?: { step_id: string; action: string; description: string; risk: string; idempotent: boolean; optional_input: string | null; expected: string | null; locators: Locator[]; output: string | null }[];
  success_when?: string | null; business_outcomes?: { when: string | null; outcome: string }[]; recoverable?: { when: string | null; action: string; attempts: number }[];
}

export interface Target { name: string; app: string; base_url: string; sandbox: boolean; signs_in_as: string }

export interface Approval { id: number; run_id: string; tier: string; requested_by: string; requested_at: string; decision: string | null; decided_by: string | null; decided_at: string | null; reason: string | null }
export interface RunView {
  id: string; capability_id: string; version: string | null; status: string; requested_by: string; target: string | null; idempotency_key: string | null;
  committed: boolean; commit_step: string | null; error_code: string | null; created_at: string; started_at: string | null; finished_at: string | null;
  resolution: string | null; params: Record<string, unknown>; evidence: string | null; has_trace: boolean; pace_ms: number; show_window: boolean;
  paused?: { reason: string; step_id: string | null; since: string; stops_in_s: number | null; being_helped: boolean } | null;
  awaiting_approval?: AwaitingApproval | null;
  approvals?: Approval[]; result?: RunResult | null; repairs?: { id: string; status: string; step_id: string; confident: boolean }[];
}
/** A run stopped just before an irreversible step, waiting for someone to approve or reject it. */
export interface AwaitingApproval { tier: string; requested_by: string; step_id: string | null; description: string | null; since: string; stops_in_s: number | null }
export interface ApprovalState {
  awaiting: AwaitingApproval | null; page_moved?: boolean; capability_id?: string; params?: Record<string, unknown>; url: string | null; title?: string | null;
  fields: { name: string; value: string | null }[]; page_text?: string | null; has_screenshot?: boolean; screenshot_token?: number;
  elements?: EscalationElement[]; last_action?: { ok: boolean; error: string | null; kind: string } | null;
}
export interface RunResult {
  status: string; outputs: Record<string, unknown> | null; business_outcome: string | null; error: { code: string | null; message: string; step_id: string | null } | null;
  steps_completed: string[]; steps_skipped: string[]; escalated: boolean; recovered: boolean; committed: boolean; deduplicated: boolean; approval_tier: string | null; repair_proposal_id: string | null;
}
export interface TimelineStep { n: number; step_id: string; action: string; description: string; risk: string; optional_input: string | null; status: "ok" | "failed" | "skipped" | "not_run"; expected: string | null; screenshot: string | null; actual?: string; error_code?: string }
export interface SignOn { status: string; capability_id: string | null; step_id: string | null; screenshot: string | null; actual: string | null }
export interface Timeline { sign_on: SignOn | null; steps: TimelineStep[]; pauses: { type: string; at: number; reason: string | null }[]; screenshots: string[]; success_expectation: string | null; event_count: number }

export interface PendingApproval { run_id: string; capability_id: string; tier: string; requested_by: string; requested_at: string; params: Record<string, unknown>; at_step?: boolean; description?: string; stops_in_s?: number | null }
export interface Candidate { ref: string; role: string; name: string | null; html_name: string | null; score: number; why: Record<string, number> }
export interface Locator { strategy: string; value: string }
export interface RepairProposal {
  id: string; capability_id: string; base_version: string; step_id: string; run_id: string | null; method: string; confident: boolean; reason: string;
  old_target: { semantic_description: string; locators: Locator[] }; new_target: { semantic_description: string; locators: Locator[] } | null;
  candidates: Candidate[]; page_url: string | null; touches_irreversible_step: boolean;
}
export interface RepairRow { id: string; capability_id: string; base_version: string; step_id: string; run_id: string | null; status: string; created_at: string; decided_by: string | null; new_version: string | null; confident: boolean }

export interface CapMetrics { capability_id: string; runs: number; settled: number; statuses: Record<string, number>; success_rate: number | null; failure_rate: number | null; needs_review: number; escalation_rate: number | null; auto_recovered: number; committed: number; latency_s: { p50: number | null; p95: number | null; max: number | null; n: number }; top_error_codes: Record<string, number> }
export interface Metrics { capabilities: CapMetrics[]; totals: { runs: number; pending_approvals: number; pending_repairs: number; failing_canaries: string[] }; browser_pool: Record<string, number> }

export interface ArtifactDiff { capability_id: string; version_a: string; version_b: string; metadata: string[]; schema: string[]; safety: string[]; error_handling: string[]; steps: { kind: string; step: string; summary: string; details: string[] }[] }
export interface PolicyView { escalation: { enabled: boolean; wait_s: number; max_paused: number; approval_wait_s: number; max_awaiting_approval: number }; default_approval: string; discovery: { non_sandbox_read_only: boolean; max_steps: number; timeout_s: number; commit_wait_s: number }; approvers: Record<string, string>; capabilities: Record<string, { approval: string; approve_at: "run" | "step"; caps: { max_param: Record<string, number>; max_commits_per_day: number | null } }>; tracing: { mode: string; non_sandbox: boolean }; evidence: { screenshots: string; non_sandbox: boolean }; risk_keywords: { commit: string[]; domain: string[] }; redaction: { patterns: string[]; field_names: string[] }; approval_tiers: Record<string, string> }
export interface KeyRow { id: number; name: string; role: Role; created_at: string; last_used_at: string | null; revoked_at: string | null }

export interface RepairItemLite { id: string; confident: boolean; touches_irreversible_step: boolean }

export interface Features { show_window: boolean; max_pace_ms: number; watch_presets: { slow: number; step_by_step: number } }

export type DiscoverEffect = "read_only" | "changes_data" | "irreversible";
export interface DiscoverInput { name: string; kind: "text" | "number"; required: boolean; example: string; description?: string | null; pattern?: string | null; choices?: string[] | null }
export interface DiscoveryContract {
  task_name: string; name: string; description: string; target: string; goal: string; start_path: string | null;
  inputs: DiscoverInput[]; outputs: { name: string; description?: string | null }[]; success_text: string | null; success_status: string;
  outcomes: { when_text: string; outcome: string }[]; effect: DiscoverEffect; commit_approval: "auto_sandbox" | "supervisor"; hint?: string | null;
  verify?: { inputs: Record<string, string>; expect_outcome?: string | null; same_as_example?: boolean } | null; retry_on_text?: string | null; timeout_text?: string | null;
}
export interface DiscoverOptions { targets: { name: string; app_id: string; base_url: string; sandbox: boolean; read_only_only: boolean }[]; model_available: boolean; busy: boolean; limits: { max_steps: number; timeout_s: number; commit_wait_s: number } }
export interface DiscoverTurn { n: number; url: string | null; title: string | null; screenshot: string | null; tool: string; summary: string; ok: boolean | null; error: string | null; value: string | null; reasoning: string | null; commit?: { approved: boolean; by: string | null; mode: string } }
export interface DiscoverVerify { ran: boolean; passed?: boolean; status?: string; business_outcome?: string | null; outputs?: Record<string, unknown> | null; error?: string | null; warnings?: string[]; inputs?: Record<string, unknown>; expected?: string }
export interface DiscoverSession {
  id: string; created_by: string; created_at: string; finished_at: string | null; status: string; capability_id: string; version: string | null; target: string; name: string; effect: DiscoverEffect;
  stop_reason: string | null; reasoning: string | null; steps: number | null; error: string | null; retry_of: string | null; verify: DiscoverVerify | null;
  contract?: DiscoveryContract; lint?: { level: string; code: string; message: string; step_id: string | null }[] | null;
  commit_request?: { description: string; url: string; page_title: string; goal: string | null; at: string } | null; turns?: DiscoverTurn[]; draft_url?: string | null;
}

export interface EscalationElement { ref: string; role: string; name: string | null; value: string | null; options: string[] | null; disabled: boolean }
export interface EscalationState {
  paused: RunView["paused"]; goal: string | null; capability_id: string | null; url: string | null; title: string | null; elements: EscalationElement[];
  has_screenshot: boolean; screenshot_token: number; last_action: { ok: boolean; error: string | null; kind: string } | null;
}
