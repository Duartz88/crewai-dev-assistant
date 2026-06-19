import { Injectable } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { Session, Branch } from '../models';

@Injectable({ providedIn: 'root' })
export class ApiService {
  constructor(private http: HttpClient) {}

  getStatus() {
    return firstValueFrom(this.http.get<{ session: Session; running: boolean; has_changes: boolean; languages?: string[] }>('/api/status'));
  }

  startSession(projectPath: string) {
    return firstValueFrom(this.http.post<{ ok: boolean; branch: string; error?: string }>('/api/session/start', { project_path: projectPath }));
  }

  clearSession() {
    return firstValueFrom(this.http.post<{ ok: boolean }>('/api/session/clear', {}));
  }

  runAgent(agent: string, request: string) {
    return firstValueFrom(this.http.post<{ ok: boolean; num: number; error?: string }>('/api/request/run-agent', { agent, request }));
  }

  clearContext() {
    return firstValueFrom(this.http.post<{ ok: boolean }>('/api/context/clear', {}));
  }

  getContextStatus() {
    return firstValueFrom(this.http.get<{ has_context: boolean; from_agent: string | null; length: number }>('/api/context/status'));
  }

  cancelRequest() {
    return firstValueFrom(this.http.post<{ ok: boolean; error?: string }>('/api/request/cancel', {}));
  }

  respondInput(response: string) {
    return firstValueFrom(this.http.post<{ ok: boolean }>('/api/input/respond', { response }));
  }

  commit(message: string) {
    return firstValueFrom(this.http.post<{ ok: boolean; branch: string; error?: string }>('/api/session/commit', { message }));
  }

  getBranches() {
    return firstValueFrom(this.http.get<{ branches: Branch[]; current: string | null }>('/api/branches'));
  }

  deleteBranch(name: string, force: boolean) {
    return firstValueFrom(this.http.post<{ ok?: boolean; error?: string; was_session_branch?: boolean }>('/api/branches/delete', { name, force }));
  }

  getLmStatus() {
    return firstValueFrom(this.http.get<{ ok: boolean; error: string | null; model: string | null }>('/api/lm-status'));
  }

  fullFlow(request: string) {
    return firstValueFrom(this.http.post<{ ok: boolean; num: number; error?: string }>('/api/request/full-flow', { request }));
  }

  approvePlan(approvedIndices?: number[], approvedIssueIndices?: number[]) {
    const body: Record<string, number[]> = {};
    if (approvedIndices       != null) body['approved_indices']       = approvedIndices;
    if (approvedIssueIndices  != null) body['approved_issue_indices'] = approvedIssueIndices;
    return firstValueFrom(this.http.post<{ ok: boolean; error?: string }>('/api/plan/approve', body));
  }

  rejectPlan(feedback: string) {
    return firstValueFrom(this.http.post<{ ok: boolean; error?: string }>('/api/plan/reject', { feedback }));
  }

  getSettings() {
    return firstValueFrom(this.http.get<{ lm_base_url: string; lm_api_key: string; model_name: string; tavily_api_key: string }>('/api/settings'));
  }

  saveSettings(data: { lm_base_url: string; lm_api_key: string; model_name: string; tavily_api_key: string }) {
    return firstValueFrom(this.http.post<{ ok: boolean; error?: string }>('/api/settings', data));
  }

  rollbackSession() {
    return firstValueFrom(this.http.post<{ ok: boolean; commit: string; output: string; error?: string }>('/api/session/rollback', {}));
  }

  setContext(num: number) {
    return firstValueFrom(this.http.post<{ ok: boolean; error?: string }>('/api/session/set-context', { num }));
  }
}
