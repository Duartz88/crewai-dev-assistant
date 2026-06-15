import { Component, input, output, OnInit, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { ApiService } from '../../services/api';
import { Branch, Session } from '../../models';

const PROTECTED = /^(main|master|develop|dev)$/i;

@Component({
  selector: 'app-branch-modal',
  imports: [CommonModule],
  templateUrl: './branch-modal.html',
  styleUrl: './branch-modal.scss',
})
export class BranchModal implements OnInit {
  session = input.required<Session>();
  close = output<void>();
  branchDeleted = output<boolean>();

  branches = signal<Branch[]>([]);
  loading = signal(true);

  constructor(private api: ApiService) {}

  ngOnInit() { this.load(); }

  async load() {
    this.loading.set(true);
    const data = await this.api.getBranches();
    const sorted = [...data.branches].sort((a, b) => {
      if (a.current) return -1; if (b.current) return 1;
      return a.name.localeCompare(b.name);
    });
    this.branches.set(sorted);
    this.loading.set(false);
  }

  isProtected(b: Branch) { return b.current || PROTECTED.test(b.name); }

  async deleteBranch(branch: Branch) {
    if (!confirm(`Apagar branch "${branch.name}"?`)) return;

    let data = await this.api.deleteBranch(branch.name, false);

    if (data['error'] === 'unmerged') {
      if (!confirm(`"${branch.name}" tem commits não merged. Forçar apagar?`)) return;
      data = await this.api.deleteBranch(branch.name, true);
    }

    if (data['ok']) {
      this.branches.update(bs => bs.filter(b => b.name !== branch.name));
      this.branchDeleted.emit(!!data['was_session_branch']);
    } else {
      alert('Erro: ' + (data['error'] || 'Falha desconhecida'));
    }
  }
}
