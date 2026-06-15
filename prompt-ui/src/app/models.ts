export interface SessionRequest {
  num: number;
  request: string;
  status: string;
  elapsed: number | null;
}

export interface Session {
  project_path: string;
  branch: string | null;
  requests: SessionRequest[];
  started_at: string | null;
}

export interface SseMessage {
  type: string;
  [key: string]: unknown;
}

export interface Branch {
  name: string;
  current: boolean;
}
