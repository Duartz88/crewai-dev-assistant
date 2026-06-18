import { Component, OnInit, OnDestroy, signal, computed } from '@angular/core';
import { CommonModule } from '@angular/common';
import { SseService } from '../../services/sse';
import { ApiService } from '../../services/api';
import { Session, SessionRequest, SseMessage, ArchitecturePlanData } from '../../models';
import { SetupModal } from '../setup-modal/setup-modal';
import { PromptModal } from '../prompt-modal/prompt-modal';
import { CommitModal } from '../commit-modal/commit-modal';
import { BranchModal } from '../branch-modal/branch-modal';
import { OutputPane } from '../output-pane/output-pane';
import { Sidebar } from '../sidebar/sidebar';
import { PlanReviewModal } from '../plan-review-modal/plan-review-modal';
import { SettingsModal } from '../settings-modal/settings-modal';

export interface OutputLine { text: string; cls: string; html?: string; }

@Component({
  selector: 'app-shell',
  imports: [CommonModule, SetupModal, PromptModal, CommitModal, BranchModal, OutputPane, Sidebar, PlanReviewModal, SettingsModal],
  templateUrl: './shell.html',
  styleUrl: './shell.scss',
})
export class Shell implements OnInit, OnDestroy {
  session = signal<Session>({ project_path: '', branch: null, requests: [], started_at: null });
  running = signal(false);
  lmOk = signal<boolean | null>(null);
  lmModel = signal<string | null>(null);
  lines = signal<OutputLine[]>([]);
  sidebarWidth = signal(260);
  selectedAgent = signal<string>('full-flow');
  contextFrom = signal<string | null>(null);
  elapsedSec = signal(0);
  elapsedLabel = computed(() => {
    const s = this.elapsedSec();
    if (s < 60) return `${s}s`;
    const m = Math.floor(s / 60), sec = s % 60;
    if (s < 3600) return `${m}m ${sec}s`;
    return `${Math.floor(s / 3600)}h ${String(Math.floor((s % 3600) / 60)).padStart(2, '0')}m`;
  });
  languages = signal<string[]>([]);

  // ── Sidebar resize ──────────────────────────────────────────────────────────
  private _resizing = false;
  private _startX = 0;
  private _startW = 0;

  private _onResizeMove = (e: MouseEvent) => {
    if (!this._resizing) return;
    const w = Math.max(160, Math.min(480, this._startW + (e.clientX - this._startX)));
    this.sidebarWidth.set(w);
  };
  private _onResizeUp = () => { this._resizing = false; };

  onResizeStart(e: MouseEvent) {
    this._resizing = true;
    this._startX = e.clientX;
    this._startW = this.sidebarWidth();
    e.preventDefault();
  }

  showSetup       = signal(true);
  setupError      = signal('');
  showPrompt      = signal(false);
  showCommit      = signal(false);
  showBranch      = signal(false);
  showPlanReview  = signal(false);
  showSettings    = signal(false);
  planData        = signal<ArchitecturePlanData | null>(null);
  planCountdown   = signal(600);
  showConfirmNewSession  = signal(false);
  showConfirmRollback    = signal(false);
  lastStats = signal<Record<string, { count: number; total_secs: number }> | null>(null);

  promptData = signal<{ prompt: string; context: string }>({ prompt: '', context: '' });
  fixFeedback = '';

  // tracks where in lines[] the current request's output begins
  private _reqStartLine = 0;
  private _runTimer: ReturnType<typeof setInterval> | null = null;
  private _pendingToolName: string | null = null;
  private _pendingToolN = 0;         // sequential number carried from tool_call event
  private _lastToolCardIdx = -1;     // index in lines[] of the most recent tool card
  private _scanningLineIdx  = -1;    // index of the current scanning status line

  // ── Markdown streaming state ────────────────────────────────────────────────
  private _md = {
    state: 'normal' as 'normal' | 'code' | 'antes' | 'depois',
    lang: '',
    lines: [] as string[],
    antesCode: null as { lang: string; text: string } | null,
    nextIs: null as 'antes' | 'depois' | null,
  };

  private _esc(s: string): string {
    return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  private _renderCode(text: string, lang: string): string {
    const label = lang && lang !== 'text' ? `<span class="code-lang">${lang}</span>` : '';
    return `<div class="md-code">${label}<pre><code>${this._esc(text)}</code></pre></div>`;
  }

  private _renderDiff(_antes: { lang: string; text: string }, depois: { lang: string; text: string }): string {
    const lang = _antes.lang || depois.lang;
    const label = lang && lang !== 'text' ? `<span class="code-lang">${lang}</span>` : '';
    return `<div class="md-code md-resultado">${label}<pre><code>${this._esc(depois.text)}</code></pre></div>`;
  }

  private _toolIcon(tool: string): string {
    const icons: Record<string, string> = {
      read_file: 'draft', list_project_structure: 'folder_open',
      write_file: 'edit', diff_write_file: 'edit', patch_file: 'edit',
      git_log: 'history', read_project_memory: 'memory',
      write_project_memory: 'memory',
      validate_python_syntax: 'check_circle', validate_typescript: 'check_circle',
      validate_powershell: 'check_circle', run_tests: 'play_circle',
      compare_files: 'difference', grep_in_files: 'search', grep_in_project: 'search',
      mark_endpoint_verified: 'verified',
      read_project_state: 'hub',
    };
    return icons[tool] || 'build';
  }

  private _addHtml(html: string, cls = '') {
    this.lines.update(ls => [...ls, { text: '', cls, html }]);
  }

  private _flushPendingTool() {
    if (!this._pendingToolName) return;
    const tool = this._pendingToolName;
    const n    = this._pendingToolN;
    this._pendingToolName = null;
    this._pendingToolN = 0;
    this._addHtml(
      `<div class="tool-card">
        <span class="icon sm">${this._toolIcon(tool)}</span>
        <span class="tool-name">${this._esc(tool)}</span>
        <span class="tool-dur"></span>
      </div>`, 'tool-card-line');
    this._lastToolCardIdx = this.lines().length - 1;
  }

  projectName = computed(() => {
    const p = this.session().project_path;
    return p ? p.split(/[\\/]/).pop()! : '—';
  });

  constructor(private sse: SseService, private api: ApiService) {}

  ngOnInit() {
    this.api.getStatus().then(data => {
      if (data.session.project_path) {
        this.session.set(data.session);
        this.running.set(data.running);
        this.showSetup.set(false);
        this.addLine(`Sessão retomada: ${data.session.project_path}`, 'ok');
        if (data.session.branch) this.addLine(`Branch: ${data.session.branch}`, 'dim');
        if ((data as any).languages?.length) this.languages.set((data as any).languages);
      }
    });

    this.sse.connect('/stream').subscribe({ next: m => this.handle(m) });
    this._pollLmStatus();
    this.api.getContextStatus().then(r => { if (r.has_context) this.contextFrom.set(r.from_agent); }).catch(() => {});
    document.addEventListener('mousemove', this._onResizeMove);
    document.addEventListener('mouseup', this._onResizeUp);
  }

  ngOnDestroy() {
    document.removeEventListener('mousemove', this._onResizeMove);
    document.removeEventListener('mouseup', this._onResizeUp);
    if (this._lmPollTimer) clearInterval(this._lmPollTimer);
    if (this._runTimer) clearInterval(this._runTimer);
  }

  private _lmPollTimer: ReturnType<typeof setInterval> | null = null;

  private _pollLmStatus() {
    const check = () => this.api.getLmStatus()
      .then(r => { this.lmOk.set(r.ok); this.lmModel.set(r.model); })
      .catch(() => { this.lmOk.set(false); this.lmModel.set(null); });
    check();
    // Poll every 10s while running, every 30s otherwise
    this._lmPollTimer = setInterval(() => check(), 10_000);
  }

  private handle(msg: SseMessage) {
    switch (msg.type) {
      case 'session_state': {
        this._flushPendingTool();
        if (msg.session?.project_path) { this.session.set(msg.session); this.showSetup.set(false); }
        break;
      }
      case 'session_start':
        this._flushPendingTool();
        this.session.update(s => ({
          ...s,
          project_path: msg.project,
          branch: msg.branch,
          requests: msg.requests ?? s.requests,
        }));
        if (msg.languages?.length) this.languages.set(msg.languages);
        this.addLine(`\nSessão iniciada — branch: ${msg.branch}`, 'head');
        break;
      case 'output':
        this._flushPendingTool();
        this.appendOutput(msg.text);
        break;
      case 'request_start': {
        this._flushPendingTool();
        this.running.set(true);
        this.elapsedSec.set(0);
        if (this._runTimer) clearInterval(this._runTimer);
        this._runTimer = setInterval(() => this.elapsedSec.update(s => s + 1), 1000);
        this._reqStartLine = this.lines().length;
        this._md = { state: 'normal', lang: '', lines: [], antesCode: null, nextIs: null };
        this.addLine(`\n${'─'.repeat(60)}\n${msg.agent_label} — Pedido #${msg.num}: ${msg.request}`, 'head');
        break;
      }
      case 'request_done': {
        this._flushPendingTool();
        if (this._runTimer) { clearInterval(this._runTimer); this._runTimer = null; }
        this.running.set(false);
        // Always reset markdown state on request end — prevents a half-open code
        // block from bleeding into the next request's output.
        this._md = { state: 'normal', lang: '', lines: [], antesCode: null, nextIs: null };
        const done      = msg.status === 'done';
        const cancelled = msg.status === 'cancelled';
        const label = done ? 'Concluído' : cancelled ? 'Cancelado' : 'Falhou';
        const cls   = done ? 'ok'        : cancelled ? 'warn'      : 'err';
        this.addLine(`\n${label} em ${msg.elapsed}s`, cls);
        const capturedOutput = this.lines()
          .slice(this._reqStartLine)
          .map(l => l.text || '')
          .filter(t => t.trim())
          .join('\n');
        const requests = msg.requests.map(r =>
          r.num === msg.num
            ? { ...r, output: r.output ?? (capturedOutput || undefined) }
            : r
        );
        this.session.update(s => ({ ...s, requests }));
        break;
      }
      case 'context_updated':
        this._flushPendingTool();
        this.contextFrom.set(msg.from_agent);
        break;
      case 'lm_error':
        this._flushPendingTool();
        if (this._runTimer) { clearInterval(this._runTimer); this._runTimer = null; }
        this.running.set(false);
        this.lmOk.set(false);
        this.addLine(`\nLM Studio: ${msg.text}`, 'err');
        break;
      case 'tool_call':
        this._flushPendingTool();
        this._pendingToolName = msg.tool;
        this._pendingToolN    = msg.n ?? 0;
        break;
      case 'tool_input': {
        const pendingTool = this._pendingToolName;
        this._pendingToolName = null;
        this._pendingToolN    = 0;
        if (pendingTool) {
          if (msg.input) {
            this._addHtml(`<div class="tool-path">${this._esc(msg.input)}</div>`, 'tool-path-line');
          }
          this._addHtml(
            `<div class="tool-card">
              <span class="icon sm">${this._toolIcon(pendingTool)}</span>
              <span class="tool-name">${this._esc(pendingTool)}</span>
              <span class="tool-dur"></span>
            </div>`, 'tool-card-line');
          this._lastToolCardIdx = this.lines().length - 1;
        } else if (msg.input) {
          this._addHtml(`<div class="tool-input">${this._esc(msg.input)}</div>`, 'tool-input-line');
        }
        break;
      }
      case 'tool_result':
        this._flushPendingTool();
        break;
      case 'tool_done': {
        const idx = this._lastToolCardIdx;
        if (idx >= 0) {
          this.lines.update(arr => {
            const copy = [...arr];
            const line = copy[idx];
            if (line?.html) {
              copy[idx] = {
                ...line,
                html: line.html.replace(
                  '<span class="tool-dur"></span>',
                  `<span class="tool-dur">#${msg.n} · ${msg.secs}s</span>`
                ),
              };
            }
            return copy;
          });
        }
        break;
      }
      case 'fix_done':
        this._flushPendingTool();
        if (this._runTimer) { clearInterval(this._runTimer); this._runTimer = null; }
        this.running.set(false);
        break;
      case 'plan_ready':
        this._flushPendingTool();
        this.planData.set(msg.plan);
        this.planCountdown.set(600);
        this.showPlanReview.set(true);
        break;
      case 'plan_rejected':
        this._flushPendingTool();
        this.showPlanReview.set(false);
        this.planData.set(null);
        break;
      case 'input_needed':
        this._flushPendingTool();
        this.promptData.set({ prompt: msg.prompt, context: msg.context });
        this.showPrompt.set(true);
        break;
      case 'input_done':
        this._flushPendingTool();
        this.showPrompt.set(false);
        break;
      case 'scanning':
        // Add a pulsing status line; record its index so scanning_done can update it.
        this.lines.update(ls => [...ls, { text: `⟳ ${msg.text}`, cls: 'scanning' }]);
        this._scanningLineIdx = this.lines().length - 1;
        break;
      case 'scanning_done': {
        const si = this._scanningLineIdx;
        if (si >= 0) {
          this.lines.update(arr => {
            const copy = [...arr];
            copy[si] = { ...copy[si], text: `✓ ${msg.text}`, cls: 'scanning-done' };
            return copy;
          });
          this._scanningLineIdx = -1;
        }
        break;
      }
      case 'plan_countdown':
        this.planCountdown.set(msg.remaining);
        break;
      case 'session_stats':
        this.lastStats.set(msg.stats);
        break;
      case 'ping':
        break;
      default: {
        // Exhaustiveness guard — TypeScript will error here if a union variant
        // is added to SseMessage but not handled in this switch.
        const _exhaustive: never = msg;
        console.warn('[SSE] Unhandled message type:', (_exhaustive as SseMessage).type, msg);
        break;
      }
    }
  }

  private appendOutput(text: string) {
    const emojiRe = /[\u{1F300}-\u{1FFFF}]|[\u{2600}-\u{26FF}]|[\u{2700}-\u{27BF}]/gu;
    const clean = text.replace(emojiRe, '');
    const md = this._md;

    clean.split('\n').forEach(raw => {
      const line = raw.trimEnd();

      // ── Inside a code/diff block ─────────────────────────────────────────
      if (md.state !== 'normal') {
        if (line.trim() === '```') {
          const codeText = md.lines.join('\n');
          if (md.state === 'antes') {
            md.antesCode = { lang: md.lang, text: codeText };
            md.state = 'normal';
          } else if (md.state === 'depois' && md.antesCode) {
            this._addHtml(this._renderDiff(md.antesCode, { lang: md.lang, text: codeText }));
            md.antesCode = null;
            md.state = 'normal';
          } else {
            // Regular code block — show if non-trivial
            if (codeText.trim()) this._addHtml(this._renderCode(codeText, md.lang));
            md.state = 'normal';
          }
          md.lines = [];
          md.lang = '';
        } else {
          md.lines.push(line);
        }
        return;
      }

      // ── Code block start ─────────────────────────────────────────────────
      const codeStart = line.trim().match(/^```(\w*)$/);
      if (codeStart) {
        md.lang = codeStart[1] || 'text';
        md.lines = [];
        if (md.nextIs === 'antes')  { md.state = 'antes';  md.nextIs = null; }
        else if (md.nextIs === 'depois') { md.state = 'depois'; md.nextIs = null; }
        else { md.state = 'code'; }
        return;
      }

      // ── ANTES / DEPOIS markers ────────────────────────────────────────────
      if (/^\*\*ANTES:\*\*$/i.test(line.trim()))  { md.nextIs = 'antes';  return; }
      if (/^\*\*DEPOIS:\*\*$/i.test(line.trim())) { md.nextIs = 'depois'; return; }

      // ── Rich panel title: ──── Title ──── (from capture.py cleanup) ───────
      const richH = line.trim().match(/^────+ (.+?) ────+$/);
      if (richH) { this.addLine(richH[1].trim(), 'section-head'); return; }

      // ── Pure separator line ───────────────────────────────────────────────
      if (/^─{8,}$/.test(line.trim())) { return; }

      // ── Markdown headers ──────────────────────────────────────────────────
      const h2 = line.match(/^## (.+)$/);
      if (h2) { this.addLine(h2[1], 'section-head'); return; }
      const h3 = line.match(/^### (.+)$/);
      if (h3) { this.addLine(h3[1], 'sub-head'); return; }

      // ── Regular line ─────────────────────────────────────────────────────
      if (line.trim()) this.addLine(line, this.classify(line));
    });
  }

  private addLine(text: string, cls = '') {
    this.lines.update(ls => [...ls, { text, cls }]);
  }

  private classify(line: string): string {
    const l = line.toLowerCase();
    const t = line.trimStart();
    if (t.startsWith('Thought:') || t.startsWith('thought:')) return 'thought';
    if (l.includes('agent:') || l.includes('arquiteto') || l.includes('programador') || l.includes('revisor')) return 'agent';
    if (l.includes('action:') || l.includes('using tool') || l.includes('→')) return 'tool';
    if (l.includes('aprovado') || l.includes('concluido') || l.includes('sucesso')) return 'ok';
    // Only flag as error when the line IS an error message, not when it merely mentions the word.
    // Matches: lines starting with ❌ / Error: / ERRO: / [ERROR] / Traceback / "error": (JSON)
    if (/^(❌|error:|erro:|erros?:|traceback|\[error\]|exception:|failed:)/i.test(t)
        || /^\s*"error"\s*:/i.test(l)
        || t.startsWith('Falhou') || t.startsWith('failed')) return 'err';
    if (l.includes('warn') || l.includes('aviso') || l.includes('aguarda')) return 'warn';
    if (t.startsWith('-') || t.startsWith('*')) return 'list';
    return '';
  }

  async onStartSession(projectPath: string) {
    try {
      const data = await this.api.startSession(projectPath);
      // Update session immediately — don't wait for SSE which has a race with lines.set([])
      this.session.update(s => ({
        ...s,
        project_path: projectPath,
        branch: data.branch ?? s.branch,
        requests: [],
      }));
      this.lines.set([]);
      this.contextFrom.set(null);
      this.showSetup.set(false);
      this.addLine(`Projeto: ${projectPath}`, 'ok');
      if (data.branch) this.addLine(`Branch: ${data.branch}`, 'dim');
    } catch (err: any) {
      const msg: string = err?.error?.error ?? err?.message ?? 'Erro ao iniciar sessão';
      this.setupError.set(msg);
    }
  }

  async onSubmitRequest(request: string) {
    const lm = await this.api.getLmStatus().catch(() => ({ ok: false, error: 'Sem resposta do backend', model: null }));
    this.lmOk.set(lm.ok);
    this.lmModel.set(lm.model ?? null);
    if (!lm.ok) {
      this.addLine(`LM Studio: ${lm.error}`, 'err');
      return;
    }
    if (this.selectedAgent() === 'full-flow') {
      const data = await this.api.fullFlow(request);
      if (data.error) this.addLine('Erro: ' + data.error, 'err');
      return;
    }
    const data = await this.api.runAgent(this.selectedAgent(), request);
    if (data.error) this.addLine('Erro: ' + data.error, 'err');
  }

  async onApprovePlan(approvedIndices: number[]) {
    this.showPlanReview.set(false);
    await this.api.approvePlan(approvedIndices);
  }

  async onRejectPlan(feedback: string) {
    this.showPlanReview.set(false);
    this.planData.set(null);
    await this.api.rejectPlan(feedback);
  }

  async onRespondInput(value: string) {
    this.showPrompt.set(false);
    await this.api.respondInput(value);
  }

  onAgentSelected(agent: unknown) {
    this.selectedAgent.set(agent as string);
  }

  async stopRequest() {
    try { await this.api.cancelRequest(); } catch { }
  }

  async clearContext() {
    await this.api.clearContext();
  }

  async onCommit(message: string) {
    this.showCommit.set(false);
    const data = await this.api.commit(message);
    if (data.error) this.addLine('Commit falhou: ' + data.error, 'err');
    else this.addLine(`Commit feito na branch ${data.branch}`, 'ok');
  }

  onNewSession() { this.showConfirmNewSession.set(true); }

  onRollback() { this.showConfirmRollback.set(true); }

  async confirmRollback() {
    this.showConfirmRollback.set(false);
    try {
      await this.api.rollbackSession();
    } catch (e: any) {
      this.lines.update(ls => [...ls, { text: `Rollback falhou: ${e?.error?.error ?? e}`, cls: 'err' }]);
    }
  }

  async onSetContext(num: number) {
    try {
      await this.api.setContext(num);
    } catch { /* SSE context_updated will reflect the change */ }
  }

  async confirmNewSession() {
    this.showConfirmNewSession.set(false);
    await this.api.clearSession();
    this.lines.set([]);
    this._md = { state: 'normal', lang: '', lines: [], antesCode: null, nextIs: null };
    this.session.set({ project_path: '', branch: null, requests: [], started_at: null });
    this.showSetup.set(true);
  }

  clearOutput() {
    this.lines.set([]);
    this._md = { state: 'normal', lang: '', lines: [], antesCode: null, nextIs: null };
    this._pendingToolName = null;
  }
}
