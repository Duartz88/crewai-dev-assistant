import { Component, output, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';

@Component({
  selector: 'app-commit-modal',
  imports: [FormsModule],
  templateUrl: './commit-modal.html',
  styleUrl: './commit-modal.scss',
})
export class CommitModal {
  commit = output<string>();
  cancel = output<void>();
  message = signal('');

  doCommit() { this.commit.emit(this.message()); this.message.set(''); }
}
