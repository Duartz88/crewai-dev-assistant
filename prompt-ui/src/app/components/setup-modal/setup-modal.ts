import { Component, input, output, signal, OnInit } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';

@Component({
  selector: 'app-setup-modal',
  imports: [CommonModule, FormsModule],
  templateUrl: './setup-modal.html',
  styleUrl: './setup-modal.scss',
})
export class SetupModal implements OnInit {
  initialPath = input('');
  cancelable  = input(false);
  errorMsg    = input('');

  start  = output<string>();
  cancel = output<void>();

  path = signal('');

  ngOnInit() {
    if (this.initialPath()) this.path.set(this.initialPath());
  }

  submit() {
    const p = this.path().trim();
    if (p) this.start.emit(p);
  }
}
