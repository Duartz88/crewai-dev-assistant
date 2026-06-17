import { Component, OnInit, output, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { ApiService } from '../../services/api';

@Component({
  selector: 'app-settings-modal',
  imports: [CommonModule, FormsModule],
  templateUrl: './settings-modal.html',
  styleUrl: './settings-modal.scss',
})
export class SettingsModal implements OnInit {
  close = output<void>();

  lmBaseUrl  = signal('');
  lmApiKey   = signal('');
  modelName  = signal('');
  saving     = signal(false);
  saved      = signal(false);
  error      = signal('');

  constructor(private api: ApiService) {}

  async ngOnInit() {
    try {
      const s = await this.api.getSettings();
      this.lmBaseUrl.set(s.lm_base_url ?? '');
      this.lmApiKey.set(s.lm_api_key ?? '');
      this.modelName.set(s.model_name ?? '');
    } catch { /* keep defaults */ }
  }

  async save() {
    this.saving.set(true);
    this.error.set('');
    try {
      await this.api.saveSettings({
        lm_base_url: this.lmBaseUrl(),
        lm_api_key:  this.lmApiKey(),
        model_name:  this.modelName(),
      });
      this.saved.set(true);
      setTimeout(() => { this.saved.set(false); this.close.emit(); }, 800);
    } catch (e: any) {
      this.error.set(e?.error?.error ?? 'Erro ao guardar definições.');
    } finally {
      this.saving.set(false);
    }
  }
}
