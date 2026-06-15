import { Component, input, output, signal, OnInit } from '@angular/core';
import { FormsModule } from '@angular/forms';

@Component({
  selector: 'app-setup-modal',
  imports: [FormsModule],
  templateUrl: './setup-modal.html',
})
export class SetupModal implements OnInit {
  initialPath = input('');
  cancelable  = input(false);

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
