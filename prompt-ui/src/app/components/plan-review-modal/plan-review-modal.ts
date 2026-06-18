import { Component, computed, input, OnInit, output, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { ArchitecturePlanData, PlanIssue } from '../../models';

type Tab = 'plan' | 'issues' | 'changes';

export interface PlanApproval {
  changeIndices: number[];       // sorted list of approved change indices
  issueIndices:  number[] | null; // sorted list of approved issue indices; null = all
}

@Component({
  selector: 'app-plan-review-modal',
  imports: [CommonModule, FormsModule],
  templateUrl: './plan-review-modal.html',
  styleUrl: './plan-review-modal.scss',
})
export class PlanReviewModal implements OnInit {
  plan      = input.required<ArchitecturePlanData>();
  countdown = input<number>(600);
  approve   = output<PlanApproval>();
  reject    = output<string>();

  tab          = signal<Tab>('plan');
  showFeedback = signal(false);
  feedback     = signal('');

  // Granular approval: set of change indices the user has checked (all checked by default)
  approvedSet = signal<Set<number>>(new Set());

  // Issue selection: set of issue indices the user wants the developer to fix
  approvedIssueSet = signal<Set<number>>(new Set());

  countdownLabel = computed(() => {
    const s = this.countdown();
    if (s <= 0) return 'Expirado';
    const m = Math.floor(s / 60), sec = s % 60;
    return m > 0 ? `${m}m ${String(sec).padStart(2, '0')}s` : `${sec}s`;
  });

  ngOnInit() {
    this.approvedSet.set(new Set(this.plan().changes.map((_, i) => i)));
    this.approvedIssueSet.set(new Set(this.plan().issues.map((_, i) => i)));
  }

  // ── Changes ───────────────────────────────────────────────────────────────
  toggleChange(i: number) {
    this.approvedSet.update(s => {
      const next = new Set(s);
      next.has(i) ? next.delete(i) : next.add(i);
      return next;
    });
  }

  isChangeApproved(i: number) { return this.approvedSet().has(i); }

  hasNoChanges  = computed(() => this.plan().changes.length === 0);
  noneSelected  = computed(() => this.plan().changes.length > 0 && this.approvedSet().size === 0);
  approveLabel  = computed(() => (this.hasNoChanges() || this.noneSelected()) ? 'Concluir' : 'Implementar');
  approveIcon   = computed(() => (this.hasNoChanges() || this.noneSelected()) ? 'check_circle' : 'rocket_launch');

  toggleAllChanges() {
    const all = new Set(this.plan().changes.map((_, i) => i));
    this.approvedSet.update(s => s.size === all.size ? new Set() : all);
  }

  // ── Issues ────────────────────────────────────────────────────────────────
  toggleIssue(i: number) {
    this.approvedIssueSet.update(s => {
      const next = new Set(s);
      next.has(i) ? next.delete(i) : next.add(i);
      return next;
    });
  }

  isIssueApproved(i: number) { return this.approvedIssueSet().has(i); }

  toggleAllIssues() {
    const all = new Set(this.plan().issues.map((_, i) => i));
    this.approvedIssueSet.update(s => s.size === all.size ? new Set() : all);
  }

  // ── Approve / Reject ──────────────────────────────────────────────────────
  doApprove() {
    const changeIndices = [...this.approvedSet()].sort((a, b) => a - b);
    const totalIssues   = this.plan().issues.length;
    const selectedIssues = [...this.approvedIssueSet()].sort((a, b) => a - b);
    // Send null when all issues are selected (back-compat: backend passes them all through)
    const issueIndices = selectedIssues.length === totalIssues ? null : selectedIssues;
    this.approve.emit({ changeIndices, issueIndices });
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
