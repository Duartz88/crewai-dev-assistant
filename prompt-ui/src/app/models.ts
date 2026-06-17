export interface SessionRequest {
  num: number;
  request: string;
  status: string;
  elapsed: number | null;
  output?: string;
}

export interface Session {
  project_path: string;
  branch: string | null;
  requests: SessionRequest[];
  started_at: string | null;
}

export interface Branch {
  name: string;
  current: boolean;
}

export interface PlanIssue {
  file: string;
  line: string;
  description: string;
  snippet: string;
  severity: string;
}

export interface PlanChange {
  path: string;
  action: string;
  reason: string;
  location: string;
}

export interface ArchitecturePlanData {
  files_read: string[];
  issues: PlanIssue[];
  changes: PlanChange[];
  endpoints_verified: string[];
  plan: string;
}

// ── SSE message discriminated union ──────────────────────────────────────────
// Each variant carries exactly the fields the backend emits for that type.
// Using a union means TypeScript will narrow correctly inside switch/if blocks.

export type SseMessage =
  | { type: 'session_state';   session: Session }
  | { type: 'session_start';   project: string; branch: string; started_at: string; languages: string[]; requests: SessionRequest[] }
  | { type: 'output';          text: string }
  | { type: 'request_start';   num: number; request: string; agent: string; agent_label: string }
  | { type: 'request_done';    num: number; status: string; elapsed: number; requests: SessionRequest[] }
  | { type: 'context_updated'; from_agent: string | null; agent_label: string | null }
  | { type: 'lm_error';        text: string }
  | { type: 'tool_call';       tool: string; n?: number }
  | { type: 'tool_input';      tool: string; input: string }
  | { type: 'tool_result';     tool: string; output: string }
  | { type: 'tool_done';       tool: string; secs: number; n: number }
  | { type: 'plan_ready';      plan: ArchitecturePlanData; num: number }
  | { type: 'plan_rejected' }
  | { type: 'input_needed';    prompt: string; context: string }
  | { type: 'input_done' }
  | { type: 'fix_done' }
  | { type: 'scanning';       text: string }
  | { type: 'scanning_done'; text: string }
  | { type: 'plan_countdown'; remaining: number }
  | { type: 'session_stats'; stats: Record<string, { count: number; total_secs: number }> }
  | { type: 'ping' };
