import { Component, computed, input, output, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { Session, SessionRequest } from '../../models';

@Component({
  selector: 'app-sidebar',
  imports: [CommonModule],
  templateUrl: './sidebar.html',
  styleUrl: './sidebar.scss',
})
export class Sidebar {
  session    = input.required<Session>();
  lastStats  = input<Record<string, { count: number; total_secs: number }> | null>(null);
  useContext = output<number>();

  projectName = computed(() => {
    const p = this.session().project_path;
    return p ? p.split(/[\\/]/).pop()! : '—';
  });

  selectedNum = signal<number | null>(null);

  selectedRequest = computed<SessionRequest | null>(() => {
    const num = this.selectedNum();
    if (num === null) return null;
    return this.session().requests.find(r => r.num === num) ?? null;
  });

  statEntries = computed(() => {
    const s = this.lastStats();
    if (!s) return [];
    return Object.entries(s)
      .sort((a, b) => b[1].total_secs - a[1].total_secs)
      .map(([name, v]) => ({ name, ...v }));
  });

  totalSecs = computed(() =>
    this.statEntries().reduce((acc, e) => acc + e.total_secs, 0)
  );

  select(r: SessionRequest) {
    this.selectedNum.set(this.selectedNum() === r.num ? null : r.num);
  }

  setContext() {
    const num = this.selectedRequest()?.num;
    if (num != null) this.useContext.emit(num);
  }

  async copyOutput() {
    const out = this.selectedRequest()?.output;
    if (out) await navigator.clipboard.writeText(out);
  }

  async copyRequest() {
    const req = this.selectedRequest()?.request;
    if (req) await navigator.clipboard.writeText(req);
  }
}
