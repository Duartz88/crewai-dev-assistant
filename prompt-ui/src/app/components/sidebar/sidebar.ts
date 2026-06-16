import { Component, input, computed, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { Session, SessionRequest } from '../../models';

@Component({
  selector: 'app-sidebar',
  imports: [CommonModule],
  templateUrl: './sidebar.html',
  styleUrl: './sidebar.scss',
})
export class Sidebar {
  session = input.required<Session>();

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

  select(r: SessionRequest) {
    this.selectedNum.set(this.selectedNum() === r.num ? null : r.num);
  }

  async copyOutput() {
    const out = this.selectedRequest()?.output;
    if (out) await navigator.clipboard.writeText(out);
  }
}
