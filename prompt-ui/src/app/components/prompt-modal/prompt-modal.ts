import { Component, input, output, signal, computed, AfterViewInit, ElementRef, ViewChild } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { CommonModule } from '@angular/common';

@Component({
  selector: 'app-prompt-modal',
  imports: [CommonModule, FormsModule],
  templateUrl: './prompt-modal.html',
  styleUrl: './prompt-modal.scss',
})
export class PromptModal implements AfterViewInit {
  data = input.required<{ prompt: string; context: string }>();
  respond = output<string>();

  reply = signal('');
  isYesNo = computed(() => /\(s\/n\)/i.test(this.data().prompt) || !this.data().prompt.trim());

  @ViewChild('inp') inp!: ElementRef<HTMLInputElement>;
  @ViewChild('ctx') ctx!: ElementRef<HTMLDivElement>;

  ngAfterViewInit() {
    setTimeout(() => {
      this.inp?.nativeElement.focus();
      const el = this.ctx?.nativeElement;
      if (el) el.scrollTop = el.scrollHeight;
    }, 50);
  }

  send(value?: string) {
    this.respond.emit(value !== undefined ? value : this.reply());
  }
}
