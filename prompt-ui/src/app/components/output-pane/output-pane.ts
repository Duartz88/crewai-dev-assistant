import { Component, input, output, signal, ElementRef, ViewChild, AfterViewChecked } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { OutputLine } from '../shell/shell';

type AgentId = 'architect' | 'developer' | 'reviewer' | 'dev+review' | 'arch+review' | 'full-flow';

const AGENTS: { id: AgentId; label: string; icon: string }[] = [
  { id: 'full-flow',   label: 'Fluxo Completo', icon: 'auto_mode'     },
  { id: 'architect',   label: 'Arquitecto',     icon: 'architecture'  },
  { id: 'developer',   label: 'Developer',      icon: 'code'          },
  { id: 'reviewer',    label: 'Reviewer',       icon: 'fact_check'    },
  { id: 'dev+review',  label: 'Dev+Review',     icon: 'merge'         },
  { id: 'arch+review', label: 'Arch+Review',    icon: 'manage_search' },
];

const AGENT_LABELS: Record<string, string> = {
  'full-flow': 'Fluxo Completo',
  architect: 'Arquitecto', developer: 'Developer',
  reviewer: 'Reviewer', 'dev+review': 'Dev+Review', 'arch+review': 'Arch+Review',
};

// Classes that represent discrete events on the timeline and get a dot.
// Regular agent output text (no cls or unrecognised cls) gets no dot.
const DOT_CLASSES = new Set([
  'head', 'section-head', 'sub-head',
  'agent', 'tool-card-line',
  'ok', 'err', 'warn',
  'scanning', 'scanning-done',
]);

@Component({
  selector: 'app-output-pane',
  imports: [CommonModule, FormsModule],
  templateUrl: './output-pane.html',
  styleUrl: './output-pane.scss',
})
export class OutputPane implements AfterViewChecked {
  lines         = input<OutputLine[]>([]);
  running       = input(false);
  selectedAgent = input<string>('architect');
  contextFrom   = input<string | null>(null);

  submit      = output<string>();
  agentChange = output<string>();
  clearCtx    = output<void>();

  readonly agents = AGENTS;

  request     = signal('');
  lastRequest = signal('');
  private autoScroll = true;
  private _prevLineCount = 0;

  @ViewChild('out') outEl!: ElementRef<HTMLDivElement>;

  ngAfterViewChecked() {
    const count = this.lines().length;
    if (count === this._prevLineCount) return;
    this._prevLineCount = count;
    if (this.autoScroll) {
      const el = this.outEl?.nativeElement;
      if (el) el.scrollTop = el.scrollHeight;
    }
  }

  onScroll(el: HTMLDivElement) {
    this.autoScroll = el.scrollHeight - el.scrollTop - el.clientHeight < 50;
  }

  onWheel(e: WheelEvent) {
    if (e.deltaY < 0) this.autoScroll = false;
  }

  onEnter(e: Event) {
    if ((e as KeyboardEvent).shiftKey) return;
    e.preventDefault();
    this.send();
  }

  send() {
    const req = this.request().trim();
    if (!req || this.running()) return;
    this.lastRequest.set(req);
    this.submit.emit(req);
    this.request.set('');
  }

  showDot(line: OutputLine): boolean {
    return DOT_CLASSES.has(line.cls ?? '');
  }

  repeatLast() {
    const last = this.lastRequest();
    if (last) this.request.set(last);
  }

  contextLabel(): string {
    const a = this.contextFrom();
    return a ? `Contexto: ${AGENT_LABELS[a] ?? a}` : '';
  }

  copyAll() {
    const text = this.lines()
      .map(l => l.text || '')
      .filter(t => t.trim())
      .join('\n');
    navigator.clipboard.writeText(text).catch(console.error);
  }

  copySelected() {
    const sel = window.getSelection()?.toString() || '';
    if (sel) navigator.clipboard.writeText(sel).catch(console.error);
  }
}
