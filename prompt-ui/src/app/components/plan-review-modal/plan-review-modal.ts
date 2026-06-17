import { Component, computed, input, OnInit, output, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { ArchitecturePlanData, PlanIssue } from '../../models';

type Tab = 'plan' | 'issues' | 'changes';

@Component({
  selector: 'app-plan-review-modal',
  imports: [CommonModule, FormsModule],
  templateUrl: './plan-review-modal.html',
  styleUrl: './plan-review-modal.scss',
})
export class PlanReviewModal implements OnInit {
  plan      = input.required<ArchitecturePlanData>();
  countdown = input<number>(600);
  approve   = output<number[]>();
  reject    = output<string>();

  tab          = signal<Tab>('plan');
  showFeedback = signal(false);
  feedback     = signal('');

  // Granular approval: set of change indices the user has checked (all checked by default)
  approvedSet = signal<Set<number>>(new Set());

  // Initialise the set once the plan input is available.
  // We use ngOnInit because inputs resolve before that lifecycle hook.

  countdownLabel = computed(() => {
    const s = this.countdown();
    if (s <= 0) return 'Expirado';
    const m = Math.floor(s / 60), sec = s % 60;
    return m > 0 ? `${m}m ${String(sec).padStart(2, '0')}s` : `${sec}s`;
  });

  ngOnInit() {
    this.approvedSet.set(new Set(this.plan().changes.map((_, i) => i)));
  }

  toggleChange(i: number) {
    this.approvedSet.update(s => {
      const next = new Set(s);
      next.has(i) ? next.delete(i) : next.add(i);
      return next;
    });
  }

  isApproved(i: number) { return this.approvedSet().has(i); }

  hasNoChanges  = computed(() => this.plan().changes.length === 0);
  noneSelected  = computed(() => this.plan().changes.length > 0 && this.approvedSet().size === 0);
  approveLabel  = computed(() => (this.hasNoChanges() || this.noneSelected()) ? 'Concluir' : 'Implementar');
  approveIcon   = computed(() => (this.hasNoChanges() || this.noneSelected()) ? 'check_circle' : 'rocket_launch');

  toggleAll() {
    const all = new Set(this.plan().changes.map((_, i) => i));
    this.approvedSet.update(s => s.size === all.size ? new Set() : all);
  }

  doApprove() {
    const indices = [...this.approvedSet()].sort((a, b) => a - b);
    this.approve.emit(indices);
  }

  doReject() {
    if (this.showFeedback()) {
      this.reject.emit(this.feedback());
    } else {
      this.showFeedback.set(true);
    }
  }

  cancelReject() {
    this.showFeedback.set(false);
    this.feedback.set('');
  }

  severityCls(s: string) {
    return s === 'high' ? 'sev-high' : s === 'low' ? 'sev-low' : 'sev-med';
  }

  snippetLines(issue: PlanIssue) {
    return (issue.snippet || '').split('\n')
      .filter(l => !l.startsWith('-'))
      .map(l => ({
        cls: l.startsWith('+') ? 'ins' : 'ctx',
        text: l,
      }));
  }


}
