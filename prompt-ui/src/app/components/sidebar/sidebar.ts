import { Component, input, computed } from '@angular/core';
import { CommonModule } from '@angular/common';
import { Session } from '../../models';

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
}
