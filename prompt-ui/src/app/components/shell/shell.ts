import { Component, OnInit, OnDestroy, signal, computed } from '@angular/core';
import { CommonModule } from '@angular/common';
import { SseService } from '../../services/sse';
import { ApiService } from '../../services/api';
import { Session, SessionRequest, SseMessage } from '../../models';
import { SetupModal } from '../setup-modal/setup-modal';
import { PromptModal } from '../prompt-modal/prompt-modal';
import { CommitModal } from '../commit-modal/commit-modal';
import { BranchModal } from '../branch-modal/branch-modal';
import { OutputPane } from '../output-pane/output-pane';
import { Sidebar } from '../sidebar/sidebar';

export interface OutputLine { text: string; cls: string; html?: string; }

@Component({
  selector: 'app-shell',
  imports: [CommonModule, SetupModal, PromptModal, CommitModal, BranchModal, OutputPane, Sidebar],
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
  selectedAgent = signal<string>('architect');
  contextFrom = signal<string | null>(null);

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

  showSetup  = signal(true);
  showPrompt = signal(false);
  showCommit = signal(false);
  showBranch = signal(false);

  promptData = signal<{ prompt: string; context: string }>({ prompt: '', context: '' });
  fixFeedback = '';

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

  private _renderDiff(antes: { lang: string; text: string }, depois: { lang: string; text: string }): string {
    const lang = antes.lang || depois.lang;
    const label = lang && lang !== 'text' ? `<span class="code-lang">${lang}</span>` : '';
    return `<div class="md-diff">
      <div class="diff-pane diff-antes">
        <div class="diff-label">ANTES${label}</div>
        <pre><code>${this._esc(antes.text)}</code></pre>
      </div>
      <div class="diff-pane diff-depois">
        <div class="diff-label">DEPOIS${label}</div>
        <pre><code>${this._esc(depois.text)}</code></pre>
      </div>
    </div>`;
  }

  private _toolIcon(tool: string): string {
    const icons: Record<string, string> = {
      read_file: 'draft', list_project_structure: 'folder_open',
      write_file: 'edit', diff_write_file: 'edit',
      git_log: 'history', read_project_memory: 'memory',
      write_project_memory: 'memory', validate_python_syntax: 'check_circle',
      validate_typescript: 'check_circle', run_tests: 'play_circle',
    };
    return icons[tool] || 'build';
  }

  private _addHtml(html: string, cls = '') {
    this.lines.update(ls => [...ls, { text: '', cls, html }]);
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
    switch (msg['type']) {
      case 'session_state': {
        const s = msg['session'] as Session;
        if (s?.project_path) { this.session.set(s); this.showSetup.set(false); }
        break;
      }
      case 'session_start':
        this.session.update(s => ({ ...s, project_path: msg['project'] as string, branch: msg['branch'] as string }));
        this.addLine(`\nSessão iniciada — branch: ${msg['branch']}`, 'head');
        break;
      case 'output':
        this.appendOutput(msg['text'] as string);
        break;
      case 'request_start': {
        this.running.set(true);
        this._md = { state: 'normal', lang: '', lines: [], antesCode: null, nextIs: null };
        const agentLabel = (msg['agent_label'] as string) || '';
        this.addLine(`\n${'─'.repeat(60)}\n${agentLabel} — Pedido #${msg['num']}: ${msg['request']}`, 'head');
        break;
      }
      case 'request_done': {
        this.running.set(false);
        const status = msg['status'] as string;
        const done = status === 'done';
        const cancelled = status === 'cancelled';
        const label = done ? 'Concluído' : cancelled ? 'Cancelado' : 'Falhou';
        const cls = done ? 'ok' : cancelled ? 'warn' : 'err';
        this.addLine(`\n${label} em ${msg['elapsed']}s`, cls);
        this.session.update(s => ({ ...s, requests: msg['requests'] as SessionRequest[] }));
        break;
      }
      case 'context_updated':
        this.contextFrom.set(msg['from_agent'] as string | null);
        break;
      case 'lm_error':
        this.running.set(false);
        this.lmOk.set(false);
        this.addLine(`\nLM Studio: ${msg['text']}`, 'err');
        break;
      case 'tool_call':
        this._addHtml(
          `<div class="tool-card">
            <span class="icon sm">${this._toolIcon(msg['tool'] as string)}</span>
            <span class="tool-name">${this._esc(msg['tool'] as string)}</span>
          </div>`, 'tool-card-line');
        break;
      case 'tool_input':
        this._addHtml(
          `<div class="tool-input">${this._esc((msg['input'] as string) || '')}</div>`,
          'tool-input-line');
        break;
      case 'tool_result':
        break; // conteúdo já flui via output event
      case 'fix_done':
        this.running.set(false);
        break;
      case 'input_needed':
        this.promptData.set({ prompt: msg['prompt'] as string, context: msg['context'] as string });
        this.showPrompt.set(true);
        break;
      case 'input_done':
        this.showPrompt.set(false);
        break;
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
    if (l.includes('erro') || l.includes('error') || l.includes('failed') || l.includes('falhou')) return 'err';
    if (l.includes('warn') || l.includes('aviso') || l.includes('aguarda')) return 'warn';
    if (t.startsWith('-') || t.startsWith('*')) return 'list';
    return '';
  }

  async onStartSession(projectPath: string) {
    const data = await this.api.startSession(projectPath);
    if (data.error) { alert(data.error); return; }
    this.lines.set([]);
    this.contextFrom.set(null);
    this.showSetup.set(false);
    this.addLine(`Projeto: ${projectPath}`, 'ok');
    if (data.branch) this.addLine(`Branch: ${data.branch}`, 'dim');
  }

  async onSubmitRequest(request: string) {
    const lm = await this.api.getLmStatus().catch(() => ({ ok: false, error: 'Sem resposta do backend', model: null }));
    this.lmOk.set(lm.ok);
    this.lmModel.set(lm.model ?? null);
    if (!lm.ok) {
      this.addLine(`LM Studio: ${lm.error}`, 'err');
      return;
    }
    const data = await this.api.runAgent(this.selectedAgent(), request);
    if (data.error) this.addLine('Erro: ' + data.error, 'err');
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

  async onNewSession() {
    if (!confirm('Iniciar nova sessão? O histórico atual será apagado.')) return;
    await this.api.clearSession();
    this.lines.set([]);
    this.session.set({ project_path: '', branch: null, requests: [], started_at: null });
    this.showSetup.set(true);
  }

  clearOutput() { this.lines.set([]); }
}
